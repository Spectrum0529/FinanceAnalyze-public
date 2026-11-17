"""Owner-scoped team presets and immutable run configuration snapshots.

Uses the existing Database migrations/transactions and Pydantic. No SDK,
environment credential lookup, network client, graph or worker is created here.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .auth import require_writer
from .database import install_immutable_guards
from .errors import PlatformError
from .pagination import validate_pagination
from .security import now_text
from .model_capabilities import ModelCapabilities, DEFAULT_MODELS

CONTRACT = 'finance-agent-teams/v1'
ROLES = ('market_analyst', 'social_analyst', 'news_analyst', 'fundamentals_analyst',
         'bull_researcher', 'bear_researcher', 'research_manager', 'trader',
         'risky_analyst', 'safe_analyst', 'neutral_analyst', 'risk_manager')
MIGRATIONS = ("""
CREATE TABLE agent_team_presets(preset_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(user_id),name TEXT NOT NULL,revision INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX agent_team_owner ON agent_team_presets(owner_id,deleted);
CREATE TABLE agent_team_revisions(preset_id TEXT NOT NULL REFERENCES agent_team_presets(preset_id),revision INTEGER NOT NULL,configuration_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(preset_id,revision));
CREATE TABLE agent_team_snapshots(snapshot_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(user_id),preset_id TEXT REFERENCES agent_team_presets(preset_id),preset_revision INTEGER,configuration_json TEXT NOT NULL,resolved_json TEXT NOT NULL,sha256 TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX agent_team_snapshot_owner ON agent_team_snapshots(owner_id);
""","""
CREATE TABLE agent_team_commands(owner_id TEXT NOT NULL REFERENCES users(user_id),idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(owner_id,idempotency_key));
""")


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True,hide_input_in_errors=True)


class ModelChoice(Strict):
    provider: Literal['openai','deepseek','offline','anthropic','gemini','qwen','kimi','glm','openai_compatible','ollama']
    model: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._:/-]*$')
    effort: str | None = Field(default=None, max_length=32)
    secret_ref: str | None = Field(default=None, pattern=r'^(?:env:FA_[A-Z0-9_]{1,80}|credential:FA_[0-9a-f]{64})$')
    endpoint_profile: str | None = Field(default=None,max_length=80,pattern=r'^[A-Za-z0-9_-]{1,80}$')
    native_parameters: dict = Field(default_factory=dict,max_length=8)

    @model_validator(mode='after')
    def references_only(self):
        if (self.provider=='offline' and self.secret_ref is not None
                or self.provider not in {'offline','ollama'} and self.secret_ref is None):
            raise ValueError('SECRET_REFERENCE_REQUIRED')
        return self


class Prompt(Strict):
    system: str = Field(default='', max_length=8192)
    task: str = Field(default='', max_length=8192)
    output: str = Field(default='', max_length=8192)


class RoleConfiguration(Strict):
    model: ModelChoice | None = None
    prompt: Prompt = Field(default_factory=Prompt)
    private_context: str = Field(default='', max_length=8192)
    budgets: 'Budgets | None' = None


class Budgets(Strict):
    history_messages: int = Field(default=32, ge=0, le=128)
    input_tokens: int = Field(default=8192, ge=256, le=65536)
    output_tokens: int = Field(default=1024, ge=64, le=8192)
    input_chars: int = Field(default=32768, ge=256, le=262144)
    context_policy: Literal['reject', 'truncate_oldest'] = 'reject'


class Rounds(Strict):
    debate: int = Field(default=1, ge=1, le=2)
    risk: int = Field(default=1, ge=1, le=2)
    tool: int = Field(default=1, ge=1, le=2)


class Execution(Strict):
    timeout_seconds: int = Field(default=120, ge=1, le=120)
    retry: int = Field(default=0, ge=0, le=2)


class TeamConfiguration(Strict):
    schema_version: Literal[1] = 1
    defaults: ModelChoice = Field(default_factory=lambda: ModelChoice(provider='offline', model='deterministic-v1'))
    roles: dict[str, RoleConfiguration] = Field(default_factory=dict, max_length=12)
    prompt: Prompt = Field(default_factory=Prompt)
    shared_context: str = Field(default='', max_length=32768)
    budgets: Budgets = Field(default_factory=Budgets)
    rounds: Rounds = Field(default_factory=Rounds)
    execution: Execution = Field(default_factory=Execution)
    profile_refs: dict[str, 'ProfileReference'] = Field(default_factory=dict,max_length=13)

    @model_validator(mode='after')
    def known_roles(self):
        if set(self.roles) - set(ROLES) or set(self.profile_refs)-{'default',*ROLES}:
            raise ValueError('UNKNOWN_ROLE')
        return self


class ProfileReference(Strict):
    profile_id: str = Field(min_length=1,max_length=128)
    revision: int = Field(ge=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


RoleConfiguration.model_rebuild()
TeamConfiguration.model_rebuild()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


# Conservative documented subsets; not account availability or paid-call proof.
# New exact model capabilities may be supplied by trusted server composition.
DEFAULT_CATALOG = DEFAULT_MODELS


class TeamProfileService:
    def __init__(self, database, *, catalogue=None, model_profiles=None, capabilities=None):
        self.database = database
        rows = deepcopy(DEFAULT_CATALOG if catalogue is None else catalogue)
        self._catalogue = {(r['provider'], r['model']): r for r in rows}
        self.model_profiles=model_profiles
        self.model_capabilities=capabilities or ModelCapabilities(rows)
        self.database.apply_module_migrations('agent_teams', MIGRATIONS)
        with self.database.transaction() as con:
            install_immutable_guards(con, ('agent_team_revisions', 'agent_team_snapshots','agent_team_commands'))

    def command_receipt(self, principal, key):
        require_writer(principal)
        with closing(self.database.connect()) as con:
            row = con.execute(
                'SELECT response_json FROM agent_team_commands WHERE owner_id=? AND idempotency_key=?',
                (principal.user_id, key),
            ).fetchone()
        if row is None:
            raise PlatformError('TEAM_WRITE_RECEIPT_NOT_FOUND', 404)
        return {'owner_id': principal.user_id, 'idempotency_key': key,
                'response': json.loads(row['response_json'])}

    def _command_read(self,con,principal,key,request):
        if key is None:return None
        if type(key)is not str or not 1<=len(key)<=128:raise PlatformError('IDEMPOTENCY_KEY_REQUIRED',422)
        digest=hashlib.sha256(canonical(request).encode()).hexdigest()
        row=con.execute('SELECT * FROM agent_team_commands WHERE owner_id=? AND idempotency_key=?',(principal.user_id,key)).fetchone()
        if row:
            if row['request_hash']!=digest:raise PlatformError('CONFLICT',409)
            return json.loads(row['response_json'])

    def _command_write(self,con,principal,key,request,response):
        if key is not None:
            con.execute('INSERT INTO agent_team_commands VALUES(?,?,?,?,?)',(principal.user_id,key,hashlib.sha256(canonical(request).encode()).hexdigest(),canonical(response),now_text()))
        return response

    def capabilities(self):
        return {'contract': CONTRACT, 'roles': list(ROLES), **self.model_capabilities.describe(),
                'real_provider_calls': 'NOT_RUN', 'custom_model_without_effort': True}

    def preview(self, configuration, *, principal=None):
        try:
            config = TeamConfiguration.model_validate(configuration).model_dump()
        except (ValidationError, TypeError, ValueError):
            raise PlatformError('INVALID_INPUT', 422) from None
        if len(canonical(config).encode()) > 262144:
            raise PlatformError('INVALID_INPUT', 413)
        profile_origins={}
        for slot,reference in config['profile_refs'].items():
            if principal is None or self.model_profiles is None:raise PlatformError('MODEL_PROFILE_OWNER_REQUIRED',403)
            item=self.model_profiles.revision(principal,reference['profile_id'],reference['revision'])
            if item['sha256']!=reference['sha256']:raise PlatformError('MODEL_PROFILE_HASH_MISMATCH',409)
            profile_origins[slot]=item
        resolved = {}
        real = False
        for role in ROLES:
            override = config['roles'].get(role, RoleConfiguration().model_dump())
            choice = deepcopy(override['model'] or config['defaults'])
            self.model_capabilities.validate(choice)
            execution_target=None
            if principal is not None and self.model_profiles is not None:
                target=None if choice['provider']=='offline'else self.model_profiles.freeze_target(principal,choice)
                self.model_profiles.check_choice_target(principal,choice,target=target)
                if target is not None:
                    execution_target={key:getattr(target,key)for key in ('provider','endpoint_id','base_url','target_sha256','owner_scope')}
                    origin=profile_origins.get(role,profile_origins.get('default'))
                    if origin and origin['configuration']['model']['provider']==choice['provider']and origin['configuration']['model'].get('endpoint_profile')==choice.get('endpoint_profile'):
                        expected=origin.get('execution_target')
                        if expected is None or expected['target_sha256']!=target.target_sha256:raise PlatformError('MODEL_TARGET_CHANGED',409)
            real |= choice['provider'] != 'offline'
            prompt = {key: '\n\n'.join(v for v in (config['prompt'][key], override['prompt'][key]) if v) for key in ('system', 'task', 'output')}
            budgets=override['budgets'] or config['budgets']
            self.model_capabilities.validate_budgets(choice,budgets)
            if sum(map(len, prompt.values())) + len(config['shared_context']) + len(override['private_context']) > budgets['input_chars']:
                raise PlatformError('INPUT_BUDGET_EXCEEDED', 422)
            resolved[role] = {'model': choice, 'prompt': prompt, 'shared_context': config['shared_context'],
                              'private_context': override['private_context'], 'budgets': deepcopy(budgets),'execution_target':execution_target}
        sha = hashlib.sha256(canonical({'configuration': config, 'resolved_roles': resolved}).encode()).hexdigest()
        return {'configuration': config, 'resolved_roles': resolved, 'sha256': sha, 'execution_ready': True,
                'configuration_valid':True,'provider_validation':'NOT_RUN'if real else'OFFLINE',
                'runtime_check_required':real,'reason_codes':['PROVIDER_RUNTIME_CHECK_REQUIRED']if real else[]}

    def _preset(self, con, principal, preset_id):
        row = con.execute('SELECT * FROM agent_team_presets WHERE preset_id=? AND owner_id=? AND deleted=0', (preset_id, principal.user_id)).fetchone()
        if row is None:
            raise PlatformError('NOT_FOUND', 404)
        return dict(row)

    def _record(self, con, row):
        value = con.execute('SELECT configuration_json FROM agent_team_revisions WHERE preset_id=? AND revision=?', (row['preset_id'], row['revision'])).fetchone()
        return {key: row[key] for key in ('preset_id', 'name', 'revision', 'created_at', 'updated_at')} | {'configuration': json.loads(value[0])}

    def get(self, principal, preset_id):
        with closing(self.database.connect()) as con:
            return self._record(con, self._preset(con, principal, preset_id))

    def list(self, principal, limit=50, offset=0):
        validate_pagination(limit, offset)
        with closing(self.database.connect()) as con:
            total = con.execute('SELECT count(*) FROM agent_team_presets WHERE owner_id=? AND deleted=0', (principal.user_id,)).fetchone()[0]
            rows = con.execute('SELECT * FROM agent_team_presets WHERE owner_id=? AND deleted=0 ORDER BY created_at,preset_id LIMIT ? OFFSET ?', (principal.user_id, limit, offset)).fetchall()
            return {'items': [self._record(con, dict(r)) for r in rows], 'total': total, 'limit': limit, 'offset': offset}

    def save(self, principal, name, configuration, *, preset_id=None, expected_revision=None,idempotency_key=None):
        require_writer(principal)
        if type(name) is not str or not name.strip() or len(name) > 80:
            raise PlatformError('INVALID_INPUT', 422)
        config = self.preview(configuration,principal=principal)['configuration']
        now = now_text()
        with self.database.transaction() as con:
            request={'action':'save','preset_id':preset_id,'expected_revision':expected_revision,'name':name,'configuration':config}
            previous=self._command_read(con,principal,idempotency_key,request)
            if previous is not None:return previous
            if preset_id is None:
                preset_id = str(uuid.uuid4())
                revision = 1
                con.execute('INSERT INTO agent_team_presets VALUES (?,?,?,?,0,?,?)', (preset_id, principal.user_id, name, revision, now, now))
            else:
                row = self._preset(con, principal, preset_id)
                if type(expected_revision) is not int or row['revision'] != expected_revision:
                    raise PlatformError('CONFLICT', 409)
                revision = row['revision'] + 1
                con.execute('UPDATE agent_team_presets SET name=?,revision=?,updated_at=? WHERE preset_id=?', (name, revision, now, preset_id))
            con.execute('INSERT INTO agent_team_revisions VALUES (?,?,?,?)', (preset_id, revision, canonical(config), now))
            self.database.audit(principal.user_id, 'agent_team.saved', preset_id, {'revision': revision}, connection=con)
            result=self._record(con, self._preset(con, principal, preset_id))
            return self._command_write(con,principal,idempotency_key,request,result)

    def delete(self, principal, preset_id, expected_revision,*,idempotency_key=None):
        require_writer(principal)
        with self.database.transaction() as con:
            request={'action':'delete','preset_id':preset_id,'expected_revision':expected_revision}
            previous=self._command_read(con,principal,idempotency_key,request)
            if previous is not None:return previous
            row = self._preset(con, principal, preset_id)
            if type(expected_revision) is not int or row['revision'] != expected_revision:
                raise PlatformError('CONFLICT', 409)
            con.execute('UPDATE agent_team_presets SET deleted=1,updated_at=? WHERE preset_id=?', (now_text(), preset_id))
            self.database.audit(principal.user_id, 'agent_team.deleted', preset_id, connection=con)
            return self._command_write(con,principal,idempotency_key,request,{'deleted': True, 'preset_id': preset_id, 'revision': row['revision']})

    def copy(self,principal,preset_id,revision,name,*,idempotency_key):
        require_writer(principal)
        if type(name)is not str or not name.strip() or len(name)>80:raise PlatformError('INVALID_INPUT',422)
        with self.database.transaction() as con:
            request={'action':'copy','preset_id':preset_id,'revision':revision,'name':name}
            previous=self._command_read(con,principal,idempotency_key,request)
            if previous is not None:return previous
            head=self._preset(con,principal,preset_id)
            if type(revision)is not int or revision!=head['revision']:raise PlatformError('CONFLICT',409)
            config=self._record(con,head)['configuration'];target=str(uuid.uuid4());now=now_text()
            con.execute('INSERT INTO agent_team_presets VALUES(?,?,?,?,0,?,?)',(target,principal.user_id,name,1,now,now))
            con.execute('INSERT INTO agent_team_revisions VALUES(?,?,?,?)',(target,1,canonical(config),now))
            self.database.audit(principal.user_id,'agent_team.copied',target,{'source_preset_id':preset_id,'source_revision':revision},connection=con)
            return self._command_write(con,principal,idempotency_key,request,self._record(con,self._preset(con,principal,target)))

    def create_snapshot(self, principal, preset_id=None, revision=None, *, configuration=None,idempotency_key=None):
        require_writer(principal)
        if (preset_id is None) == (configuration is None):
            raise PlatformError('INVALID_INPUT', 422)
        with self.database.transaction() as con:
            request={'action':'snapshot','preset_id':preset_id,'revision':revision,'configuration':configuration}
            previous=self._command_read(con,principal,idempotency_key,request)
            if previous is not None:return previous
            if preset_id is not None:
                self._preset(con, principal, preset_id)
                if type(revision) is not int or revision < 1:
                    raise PlatformError('INVALID_INPUT', 422)
                row = con.execute('SELECT configuration_json FROM agent_team_revisions WHERE preset_id=? AND revision=?', (preset_id, revision)).fetchone()
                if row is None:
                    raise PlatformError('NOT_FOUND', 404)
                configuration = json.loads(row[0])
            elif revision is not None:
                raise PlatformError('INVALID_INPUT', 422)
            preview = self.preview(configuration,principal=principal)
            snapshot_id, now = str(uuid.uuid4()), now_text()
            con.execute('INSERT INTO agent_team_snapshots VALUES (?,?,?,?,?,?,?,?)', (snapshot_id, principal.user_id, preset_id, revision,
                        canonical(preview['configuration']), canonical(preview['resolved_roles']), preview['sha256'], now))
            self.database.audit(principal.user_id, 'agent_team.snapshot', snapshot_id, {'sha256': preview['sha256']}, connection=con)
            # Build the same immutable projection inside this transaction; do
            # not recursively open a second writer or cache a future result.
            result={'snapshot_id':snapshot_id,'owner_id':principal.user_id,'preset_id':preset_id,'preset_revision':revision,
                    'sha256':preview['sha256'],'created_at':now,'configuration':preview['configuration'],'resolved_roles':preview['resolved_roles']}
            return self._command_write(con,principal,idempotency_key,request,result)

    def get_snapshot(self, principal, snapshot_id):
        with closing(self.database.connect()) as con:
            row = con.execute('SELECT * FROM agent_team_snapshots WHERE snapshot_id=? AND owner_id=?', (snapshot_id, principal.user_id)).fetchone()
        if row is None:
            raise PlatformError('NOT_FOUND', 404)
        result = {key: row[key] for key in ('snapshot_id', 'owner_id', 'preset_id', 'preset_revision', 'sha256', 'created_at')}
        result.update(configuration=json.loads(row['configuration_json']), resolved_roles=json.loads(row['resolved_json']))
        actual = hashlib.sha256(canonical({key: result[key] for key in ('configuration', 'resolved_roles')}).encode()).hexdigest()
        if actual != result['sha256']:
            raise PlatformError('SNAPSHOT_HASH_MISMATCH', 409)
        return result

    def freeze_model_reference(self, principal, snapshot_id, versions):
        """Call before jobs.submit, outside its write transaction/resolver."""
        snapshot = self.get_snapshot(principal, snapshot_id)
        return versions.freeze(principal, 'agent-team:' + snapshot_id, '1', 'model',
                               {'team_snapshot_id': snapshot_id, 'team_sha256': snapshot['sha256']})

    def snapshot_from_reference(self, principal, reference, versions):
        """Read the exact RunSpec model on execute/retry/resume, never a preset."""
        value = versions.get(principal, reference)
        if type(value) is not dict or set(value) != {'team_snapshot_id', 'team_sha256'}:
            raise PlatformError('TEAM_MODEL_BINDING_INVALID', 409)
        snapshot = self.get_snapshot(principal, value['team_snapshot_id'])
        if snapshot['sha256'] != value['team_sha256']:
            raise PlatformError('SNAPSHOT_HASH_MISMATCH', 409)
        return snapshot

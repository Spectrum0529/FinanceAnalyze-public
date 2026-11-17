"""Independent editable profile heads and append-only configuration revisions."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import uuid

from pydantic import Field, ValidationError
from .auth import require_writer
from .database import install_immutable_guards
from .errors import PlatformError
from .pagination import validate_pagination
from .security import now_text
from .team_profiles import Strict,ModelChoice,Prompt,Budgets,TeamConfiguration,ROLES,canonical
from .model_capabilities import ModelCapabilities

MIGRATIONS=("""
CREATE TABLE model_profile_heads(profile_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(user_id),name TEXT NOT NULL,revision INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX model_profile_owner ON model_profile_heads(owner_id,deleted,created_at);
CREATE TABLE model_profile_revisions(profile_id TEXT NOT NULL REFERENCES model_profile_heads(profile_id),revision INTEGER NOT NULL,configuration_json TEXT NOT NULL,sha256 TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(profile_id,revision));
CREATE TABLE model_profile_commands(owner_id TEXT NOT NULL REFERENCES users(user_id),idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(owner_id,idempotency_key));
""","""
ALTER TABLE model_profile_revisions ADD COLUMN target_json TEXT;
ALTER TABLE model_profile_revisions ADD COLUMN hash_schema TEXT NOT NULL DEFAULT 'configuration-v1';
""")


class ProfileConfiguration(Strict):
    schema_version: int=Field(default=1,ge=1,le=1)
    model: ModelChoice
    prompt: Prompt=Field(default_factory=Prompt)
    context: str=Field(default='',max_length=8192)
    budgets: Budgets=Field(default_factory=Budgets)


def configuration_hash(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def profile_hash(config,target):return configuration_hash({'schema':2,'configuration':config,'execution_target':target})


class ModelProfileService:
    def __init__(self,database,*,capabilities=None,credential_metadata=None,target_freezer=None,reference_validator=None):
        self.database=database;self.registry=capabilities or ModelCapabilities()
        self.credential_metadata=credential_metadata
        self.target_freezer=target_freezer
        self.reference_validator=reference_validator
        database.apply_module_migrations('model_library',MIGRATIONS)
        with database.transaction() as con:
            install_immutable_guards(con,('model_profile_revisions','model_profile_commands'))

    def capabilities(self):
        return {'contract':'finance-model-library/v1',**self.registry.describe(),
                'reference_pattern':'env:FA_… 或 credential:FA_<64位小写摘要>',
                'single_assistant_and_role_team_are_distinct':True}

    def freeze_target(self,principal,choice):
        self.registry.validate(choice)
        if self.target_freezer is None:raise PlatformError('CREDENTIAL_BINDING_SERVICE_UNAVAILABLE',409)
        return self.target_freezer(principal.user_id,choice['provider'],choice.get('endpoint_profile'))

    def check_choice_target(self,principal,choice,*,target=None):
        self.registry.validate(choice)
        if choice.get('secret_ref')is None:return target
        if target is None:target=self.freeze_target(principal,choice)
        self.registry.validate_target(choice,owner_id=principal.user_id,metadata_reader=self.credential_metadata,target=target,reference_validator=self.reference_validator)
        return target

    def _profile_target(self,principal,choice):
        if choice['provider']=='offline':return None
        target=self.freeze_target(principal,choice)
        self.check_choice_target(principal,choice,target=target)
        return {key:getattr(target,key)for key in ('provider','endpoint_id','base_url','target_sha256','owner_scope')}

    def validate(self,value):
        try:
            config=ProfileConfiguration.model_validate(value).model_dump()
            if len(canonical(config).encode())>131072:raise ValueError()
        except (ValidationError,TypeError,ValueError):raise PlatformError('INVALID_INPUT',422) from None
        self.registry.validate(config['model'])
        self.registry.validate_budgets(config['model'],config['budgets'])
        if sum(map(len,config['prompt'].values()))+len(config['context'])>config['budgets']['input_chars']:
            raise PlatformError('INPUT_BUDGET_EXCEEDED',422)
        return config

    def _head(self,con,principal,profile_id,*,include_deleted=False):
        row=con.execute('SELECT * FROM model_profile_heads WHERE profile_id=? AND owner_id=?',(profile_id,principal.user_id)).fetchone()
        if row is None or row['deleted'] and not include_deleted:raise PlatformError('NOT_FOUND',404)
        return dict(row)

    def _revision(self,con,head,revision):
        if type(revision)is not int or revision<1:raise PlatformError('INVALID_INPUT',422)
        row=con.execute('SELECT * FROM model_profile_revisions WHERE profile_id=? AND revision=?',(head['profile_id'],revision)).fetchone()
        if row is None:raise PlatformError('NOT_FOUND',404)
        config=json.loads(row['configuration_json'])
        target=None if row['target_json']is None else json.loads(row['target_json'])
        actual=configuration_hash(config)if row['hash_schema']=='configuration-v1'else profile_hash(config,target)
        if row['hash_schema']not in {'configuration-v1','configuration-target-v2'}or actual!=row['sha256']:
            raise PlatformError('MODEL_PROFILE_HASH_MISMATCH',409)
        return {key:head[key] for key in ('profile_id','name','created_at','updated_at')}|{
            'revision':revision,'configuration':config,'sha256':row['sha256'],'deleted':bool(head['deleted']),
            'execution_target':target,'hash_schema':row['hash_schema']}

    def get(self,principal,profile_id):
        with closing(self.database.connect()) as con:
            head=self._head(con,principal,profile_id);return self._revision(con,head,head['revision'])

    def revision(self,principal,profile_id,revision):
        with closing(self.database.connect()) as con:
            return self._revision(con,self._head(con,principal,profile_id,include_deleted=True),revision)

    def prepare_execution_profile(self,principal,profile_id,revision):
        """Trusted planning boundary; freeze target without resolving a secret.

        The caller freezes this whole returned value in its immutable plan.
        A later SDK call must use this exact destination, not re-resolve its ID.
        Ordinary revision reads remain the unchanged stored configuration.
        """
        self.get(principal,profile_id)
        value=self.revision(principal,profile_id,revision);choice=value['configuration']['model']
        if choice['provider']=='offline':return value
        target=self.freeze_target(principal,choice)
        expected=value.get('execution_target')
        if (expected is None or expected['target_sha256']!=target.target_sha256
                or expected['owner_scope']!=target.owner_scope):raise PlatformError('MODEL_TARGET_CHANGED',409)
        self.check_choice_target(principal,choice,target=target)
        return value

    def list(self,principal,limit=50,offset=0):
        validate_pagination(limit,offset)
        with closing(self.database.connect()) as con:
            count=con.execute('SELECT count(*) FROM model_profile_heads WHERE owner_id=? AND deleted=0',(principal.user_id,)).fetchone()[0]
            rows=con.execute('SELECT * FROM model_profile_heads WHERE owner_id=? AND deleted=0 ORDER BY created_at,profile_id LIMIT ? OFFSET ?',(principal.user_id,limit,offset)).fetchall()
            return {'items':[self._revision(con,dict(r),r['revision']) for r in rows],'total':count,'limit':limit,'offset':offset}

    def _command(self,principal,key,request,action):
        require_writer(principal)
        if type(key)is not str or not 1<=len(key)<=128:raise PlatformError('IDEMPOTENCY_KEY_REQUIRED',422)
        digest=configuration_hash(request)
        with self.database.transaction() as con:
            previous=con.execute('SELECT * FROM model_profile_commands WHERE owner_id=? AND idempotency_key=?',(principal.user_id,key)).fetchone()
            if previous:
                if previous['request_hash']!=digest:raise PlatformError('CONFLICT',409)
                return json.loads(previous['response_json'])
            response=action(con)
            con.execute('INSERT INTO model_profile_commands VALUES(?,?,?,?,?)',(principal.user_id,key,digest,canonical(response),now_text()))
            return response

    def save(self,principal,name,configuration,*,idempotency_key,profile_id=None,expected_revision=None):
        if type(name)is not str or not name.strip() or len(name)>80:raise PlatformError('INVALID_INPUT',422)
        config=self.validate(configuration);name=name.strip()
        def apply(con):
            target_binding=self._profile_target(principal,config['model'])
            now=now_text()
            if profile_id is None:
                target=str(uuid.uuid4());revision=1
                con.execute('INSERT INTO model_profile_heads VALUES(?,?,?,?,0,?,?)',(target,principal.user_id,name,revision,now,now))
            else:
                head=self._head(con,principal,profile_id)
                if type(expected_revision)is not int or expected_revision!=head['revision']:raise PlatformError('CONFLICT',409)
                target=profile_id;revision=head['revision']+1
                con.execute('UPDATE model_profile_heads SET name=?,revision=?,updated_at=? WHERE profile_id=?',(name,revision,now,target))
            con.execute('INSERT INTO model_profile_revisions(profile_id,revision,configuration_json,sha256,created_at,target_json,hash_schema) VALUES(?,?,?,?,?,?,?)',
                (target,revision,canonical(config),profile_hash(config,target_binding),now,None if target_binding is None else canonical(target_binding),'configuration-target-v2'))
            self.database.audit(principal.user_id,'model_profile.saved',target,{'revision':revision},connection=con)
            return self._revision(con,self._head(con,principal,target),revision)
        return self._command(principal,idempotency_key,{'action':'save','profile_id':profile_id,'expected_revision':expected_revision,'name':name,'configuration':config},apply)

    def copy(self,principal,profile_id,name,revision,*,idempotency_key):
        if type(name)is not str or not name.strip() or len(name)>80:raise PlatformError('INVALID_INPUT',422)
        name=name.strip()
        def apply(con):
            source=self._revision(con,self._head(con,principal,profile_id),revision)
            target_binding=self._profile_target(principal,source['configuration']['model'])
            if source['configuration']['model']['provider']!='offline'and target_binding!=source.get('execution_target'):
                raise PlatformError('MODEL_TARGET_CHANGED',409)
            target=str(uuid.uuid4());now=now_text();config=source['configuration']
            con.execute('INSERT INTO model_profile_heads VALUES(?,?,?,?,0,?,?)',(target,principal.user_id,name,1,now,now))
            con.execute('INSERT INTO model_profile_revisions(profile_id,revision,configuration_json,sha256,created_at,target_json,hash_schema) VALUES(?,?,?,?,?,?,?)',
                (target,1,canonical(config),profile_hash(config,target_binding),now,None if target_binding is None else canonical(target_binding),'configuration-target-v2'))
            self.database.audit(principal.user_id,'model_profile.copied',target,{'source_profile_id':profile_id,'source_revision':revision},connection=con)
            return self._revision(con,self._head(con,principal,target),1)
        return self._command(principal,idempotency_key,{'action':'copy','profile_id':profile_id,'revision':revision,'name':name},apply)

    def delete(self,principal,profile_id,expected_revision,*,idempotency_key):
        def apply(con):
            head=self._head(con,principal,profile_id)
            if type(expected_revision)is not int or expected_revision!=head['revision']:raise PlatformError('CONFLICT',409)
            con.execute('UPDATE model_profile_heads SET deleted=1,updated_at=? WHERE profile_id=?',(now_text(),profile_id))
            self.database.audit(principal.user_id,'model_profile.deleted',profile_id,connection=con)
            return {'profile_id':profile_id,'revision':head['revision'],'deleted':True,'historical_references_retained':True}
        return self._command(principal,idempotency_key,{'action':'delete','profile_id':profile_id,'expected_revision':expected_revision},apply)

    def compose_team(self,principal,selections,*,configuration=None,mode):
        require_writer(principal)
        if mode!='role_team' or type(selections)is not dict or not selections or set(selections)-{'default',*ROLES}:
            raise PlatformError('INVALID_INPUT',422)
        try:config=TeamConfiguration.model_validate(configuration or {}).model_dump()
        except (ValidationError,TypeError,ValueError):raise PlatformError('INVALID_INPUT',422) from None
        refs={}
        for slot,ref in selections.items():
            if type(ref)is not dict or set(ref)!={'profile_id','revision'}:raise PlatformError('INVALID_INPUT',422)
            self.get(principal,ref['profile_id'])
            source=self.prepare_execution_profile(principal,ref['profile_id'],ref['revision']);profile=source['configuration']
            refs[slot]={**ref,'sha256':source['sha256']}
            if slot=='default':
                config['defaults']=deepcopy(profile['model']);config['budgets']=deepcopy(profile['budgets'])
                for key in profile['prompt']:config['prompt'][key]='\n\n'.join(v for v in (profile['prompt'][key],config['prompt'][key]) if v)
                config['shared_context']='\n\n'.join(v for v in (profile['context'],config['shared_context']) if v)
            else:
                row=config['roles'].setdefault(slot,{'model':None,'prompt':{'system':'','task':'','output':''},'private_context':'','budgets':None})
                row['model']=deepcopy(profile['model']);row['budgets']=deepcopy(profile['budgets'])
                row['private_context']='\n\n'.join(v for v in (profile['context'],row['private_context']) if v)
                for key in profile['prompt']:row['prompt'][key]='\n\n'.join(v for v in (profile['prompt'][key],row['prompt'][key]) if v)
        config['profile_refs']=refs
        return {'mode':'role_team','configuration':config,'profile_refs':refs,'role_count':len(ROLES),
                'notice':'这是多角色团队模式；只选一个默认模型时，该模型将承担多个角色。单助手任务请在开始研究中明确选择单助手模式。'}

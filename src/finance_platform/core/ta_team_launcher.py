"""Per-run immutable team binding over the existing native product supervisor."""
from dataclasses import dataclass, replace
from pathlib import Path
from .errors import PlatformError
from .local_personal import LOCAL_OWNER
from .ta_team_admission import offline_choice


def fail(code='TA_TEAM_PROFILE_BINDING_REQUIRED'):
    raise PlatformError(code)


@dataclass(frozen=True)
class TeamNativeLauncher:
    runtime: object
    template: object
    resume_store: object

    def _original(self):
        from .ta_product import TANativeProductLauncher
        from .ta_product_binding import PersonalTeamBinding
        if type(self.template) is not PersonalTeamBinding:
            fail('TA_TEAM_SOURCE_BINDING_REQUIRED')
        # Reuse the manifest returned by this complete verification, rather
        # than rereading it or entering the product launcher's verifier again.
        sources = self.template.verify()[2]
        rows = sources['rows']
        names = {row['path'].replace('\\', '/').rsplit('/', 1)[-1] for row in rows}
        if not {'team_models.py', 'team_execution.py', 'team_ports.py', 'ta_team_admission.py', 'server_model_ports.py'} <= names:
            fail('TA_TEAM_SOURCE_BINDING_REQUIRED')
        return TANativeProductLauncher(self.template, self.resume_store)

    def describe_verified(self):
        """Fresh metadata from exactly one full template/source verification.

        Do not delegate through the product metadata properties: those each
        perform another complete check. No description is stored or reused.
        """
        original = self._original()
        binding = original.binding
        return {
            "release": binding.release,
            "worker_lock_bytes": binding.files['lock'].read_verified(),
            "source_manifest_sha256": binding.source_identity,
            "supports_agent_teams": True,
        }

    @property
    def supports_agent_teams(self):
        self._original()
        return True

    @property
    def worker_lock_bytes(self): return self._original().worker_lock_bytes
    @property
    def source_manifest_sha256(self): return self._original().source_manifest_sha256
    @property
    def release(self): return self._original().release

    def accepts_team_owner(self, principal):
        return self.runtime.access_mode != 'accounts' or principal.user_id == LOCAL_OWNER

    def validate_external_team(self,principal,snapshot):
        from . import ta_external_model
        if not self.accepts_team_owner(principal) or snapshot['owner_id']!=principal.user_id:
            fail('TA_CURRENT_HUMAN_BATCH_AUTHORITY_REQUIRED')
        if snapshot['configuration']['execution']['retry']!=0:
            fail('TA_PROVIDER_RETRY_NOT_METERED')
        broker_source=Path(ta_external_model.__file__).absolute()
        if not any(Path(row["path"]).absolute()==broker_source for row in self.template.verify()[2]["rows"]):
            fail('TA_PROVIDER_SOURCE_BINDING_REQUIRED')
        store=self.runtime.provider_credential_store_factory()
        metered=False
        for item in snapshot['resolved_roles'].values():
            if offline_choice(item['model']) and item.get('execution_target') is None:
                continue
            target=ta_external_model.provider_target(item)
            if store.freeze_target(principal.user_id,"openai",None)!=target:
                fail('TA_PROVIDER_TARGET_CHANGED')
            store.validate_reference_target(principal.user_id,item['model']['secret_ref'],target=target)
            metered=True
        if not metered:
            fail('TA_PROVIDER_PROFILE_OFFLINE')

    def for_profile(self, principal, profile, snapshot, *, provider_approval=None):
        """Research calls this before prepare_attempt; never mutate a singleton."""
        if not self.accepts_team_owner(principal):
            fail('TA_CURRENT_HUMAN_BATCH_AUTHORITY_REQUIRED')
        original = self._original()
        actual = self.runtime.agent_teams.get_snapshot(principal, snapshot.get('snapshot_id'))
        if (actual != snapshot or actual['owner_id'] != principal.user_id
                or profile != {'mode': 'SERVER_PROFILE', 'profile_id': 'team-' + actual['snapshot_id'], 'profile_sha256': actual['sha256']}):
            fail()
        if provider_approval is None:
            for item in actual['resolved_roles'].values():
                if not offline_choice(item['model']) or item.get('execution_target')is not None:
                    fail('TEAM_REAL_PROVIDER_DISABLED')
        else:
            self.validate_external_team(principal,actual)
        bound = replace(self.template, model_profile_sha256=actual['sha256'])
        return (type(original)(bound,self.resume_store) if provider_approval is None else
                type(original)(bound,self.resume_store,self.runtime.provider_credential_store_factory))

    def prepare_attempt(self, *args, **kwargs): fail()
    def execute(self, *args, **kwargs): fail()
    def stage_for_core_success(self, *args, **kwargs): fail()
    def discard_staged(self, *args, **kwargs): fail()
    def checkpoint_status(self, principal, job):
        return self._original().checkpoint_status(principal, job)


def compose_team_launcher(runtime, binding):
    """Trusted server composition supplies a measured immutable source binding."""
    from .ta_product_binding import PersonalTeamBinding
    from .ta_native import load_pure
    from .ta_resume_store import TAResumeStore
    if type(binding) is not PersonalTeamBinding or binding.attempt_base != runtime.root / '.local/platform/native_research':
        fail('TA_TEAM_SOURCE_BINDING_REQUIRED')
    binding.verify()
    workflow = load_pure(binding.files['workflow'], 'finance_platform_team_bound_workflow')
    store = TAResumeStore(runtime.jobs, binding.attempt_base, workflow, binding.source_identity, binding.files['lock'].sha256)
    launcher = TeamNativeLauncher(runtime, binding, store)
    launcher._original()
    return launcher

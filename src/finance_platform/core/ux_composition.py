"""Trusted shared composition for the API and generic product worker."""
from .errors import PlatformError


def install_workflows(runtime, credential_store):
    """Register the existing owner services exactly once, without starting jobs."""
    from finance_platform.operations.provider_credentials import ProviderCredentialStore
    from finance_platform.data.simple_library_jobs import install_library
    from finance_platform.data.simple_library_api import create_router as data_router
    from finance_platform.research.ux_service import ResearchTaskService, register_handlers
    from finance_platform.research.ux_api import create_router as research_router

    if type(credential_store) is not ProviderCredentialStore:
        raise PlatformError("INVALID_INPUT", 422)
    if getattr(runtime, "research_task_service", None) is not None:
        raise PlatformError("INVALID_INPUT", 422)

    def massive_credential(owner, reference, *, provider, endpoint_id):
        # FA014's official SDK factory has this exact fixed destination. The
        # caller cannot choose an endpoint, and a mutable alias is never used.
        if provider != "massive" or endpoint_id is not None:
            raise PlatformError("CREDENTIAL_TARGET_MISMATCH", 409)
        target = credential_store.freeze_target(owner, provider, endpoint_id)
        if target.base_url != "https://api.massive.com":
            raise PlatformError("CREDENTIAL_TARGET_MISMATCH", 409)
        return credential_store.resolve_for_provider(owner, reference, target=target)

    def wrds_credential(owner, reference, *, expected_generation):
        # Keep the optional data adapter out of startup and resolve only after
        # the worker checks the owner and the credential generation.
        from finance_platform.data.wrds import WrdsCredentials
        value = credential_store.resolve_wrds(owner, reference, expected_generation=expected_generation)
        return WrdsCredentials(value.username, value.password)

    runtime.wrds_credential_generation = credential_store.wrds_credential_generation
    runtime.wrds_credential = wrds_credential

    from .openbb_deployment import optional_source_kwargs
    library = install_library(runtime, credential_resolver=massive_credential,
                              **optional_source_kwargs(runtime.root))
    runtime.dataset_library = library
    # ResearchTaskService's trusted resolver handles protected references and
    # legacy env references with its canonical target checks. Do not replace
    # it with the store-only resolver and lose the existing env path.
    research = ResearchTaskService(runtime, datasets=library, model_profiles=runtime.model_profiles, credential_store=credential_store)
    register_handlers(runtime.handlers, runtime, service=research)
    runtime.research_task_service = research
    runtime.routers.append(data_router(runtime, library=library))
    runtime.routers.append(research_router(runtime, service=research))
    from .onboarding_example import create_router as onboarding_router
    runtime.routers.append(onboarding_router(runtime))
    if not runtime.hosted_accounts:
        from .onboarding_migration import create_router as migration_router
        from .onboarding_yahoo_transfer import install as install_yahoo_transfer
        install_yahoo_transfer(runtime)
        runtime.routers.append(migration_router(runtime))
    from .autonomous_deployment import install_autonomous
    install_autonomous(runtime)
    from .goal_composition import install_workflow
    install_workflow(runtime, credential_store)
    return runtime

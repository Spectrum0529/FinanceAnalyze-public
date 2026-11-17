"""Data handlers registered with the shared queue, no parallel task system."""
from predictor.access import AccessPolicy
from .contracts import DataError
from .providers import AlpacaBars, SECEdgar, RestJSON
from .collection import Collector
from .service import DataService


def connector_for(source):
    if source["provider"] == "alpaca":
        return AlpacaBars(source["connection"])
    if source["provider"] == "sec":
        return SECEdgar(source["connection"])
    if source["provider"] == "rest":
        # Server policy cannot be supplied by a URL-import request or source object.
        import os
        import json
        import re
        raw = os.environ.get("FINANCE_PLATFORM_DATA_REST_POLICY")
        if raw is None:
            raise DataError("REST_SERVER_POLICY_REQUIRED")
        try:
            if len(raw) > 4096:
                raise ValueError
            policy = json.loads(raw)
            if set(policy) != {"allowed_hosts", "path_prefixes"} or not isinstance(policy["allowed_hosts"], list) or not 1 <= len(policy["allowed_hosts"]) <= 20 or not isinstance(policy["path_prefixes"], list) or not 1 <= len(policy["path_prefixes"]) <= 20:
                raise ValueError
            if any(not isinstance(h, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", h) for h in policy["allowed_hosts"]):
                raise ValueError
            if any(not isinstance(p, str) or not re.fullmatch(r"/[A-Za-z0-9/_-]{0,100}", p) or ".." in p for p in policy["path_prefixes"]):
                raise ValueError
        except (ValueError, TypeError):
            raise DataError("REST_SERVER_POLICY_INVALID") from None
        return RestJSON(source["connection"], **policy)
    raise DataError("COLLECTION_PROVIDER_UNSUPPORTED")


def _register_legacy_handlers(registry, runtime):
    service = DataService(runtime.database, AccessPolicy(runtime.root))

    def validate(parameters):
        if set(parameters) != {"dataset_id", "parameters", "collection_id", "max_pages"}:
            raise DataError("COLLECTION_JOB_SCHEMA_INVALID")
        from finance_platform.core.contracts import identifier
        identifier(parameters["dataset_id"])
        if parameters["collection_id"] is not None:
            identifier(parameters["collection_id"])
        if type(parameters["max_pages"]) is not int or not 1 <= parameters["max_pages"] <= 10000 or not isinstance(parameters["parameters"], dict):
            raise DataError("COLLECTION_JOB_SCHEMA_INVALID")

    def execute(context, parameters):
        owner_id = context.run.owner_id
        dataset = service.dataset(owner_id, parameters["dataset_id"])
        source = service.source(owner_id, dataset["source_id"], dataset["source_version"])
        collection_id = parameters["collection_id"]
        if collection_id is None:
            from contextlib import closing
            root_run = context.run.run_id
            parent = context.run.parent_run_id
            with closing(runtime.database.connect()) as conn:
                while parent is not None:
                    row = conn.execute("SELECT parent_run_id FROM runs WHERE run_id=? AND owner_id=?", (parent, owner_id)).fetchone()
                    if row is None:
                        raise DataError("COLLECTION_PARENT_MISSING")
                    root_run, parent = parent, row[0]
            collection_id = "collection-" + root_run
        return Collector(service).collect(context, owner_id, dataset["dataset_id"], connector_for(source),
            parameters["parameters"], collection_id=collection_id, max_pages=parameters["max_pages"])

    registry.register("data.collect", execute, validate=validate, recoverable=True)

    def validate_stream(parameters):
        if set(parameters) != {"dataset_id", "symbols", "max_frames"} or type(parameters["max_frames"]) is not int or not 1 <= parameters["max_frames"] <= 10000:
            raise DataError("STREAM_JOB_SCHEMA_INVALID")
        from finance_platform.core.contracts import identifier
        identifier(parameters["dataset_id"])
        from .stream import AlpacaStreamState
        AlpacaStreamState({"endpoint": "https://data.alpaca.markets", "key_env": None, "secret_env": None, "user_agent_env": None, "feed": "iex"}, parameters["symbols"])

    def execute_stream(context, parameters):
        from .stream import stream_capture
        dataset = service.dataset(context.run.owner_id, parameters["dataset_id"])
        source = service.source(context.run.owner_id, dataset["source_id"], dataset["source_version"])
        if source["provider"] != "alpaca":
            raise DataError("STREAM_PROVIDER_UNSUPPORTED")
        return stream_capture(context, service, context.run.owner_id, dataset["dataset_id"], source["connection"],
                              parameters["symbols"], max_frames=parameters["max_frames"])

    # Stream reconnects need explicit backfill. Never auto-recover as if lossless.
    registry.register("data.stream", execute_stream, validate=validate_stream, recoverable=False)


def register_handlers(registry, runtime):
    from .provider_sdk.bootstrap import runtime_components
    from .provider_sdk.jobs import register_handlers as provider_register
    service,runner=runtime_components(runtime)
    provider_register(registry,runtime,service,runner)
    catalog = getattr(runtime, 'qlib_reviewed_catalog', None)
    if catalog is not None:
        from predictor.access import AccessPolicy
        from .qlib_input_bridge import OwnedQlibInputBridge
        from .qlib_export_authorizer import QlibExportAuthorizer, validate_register_parameters
        from finance_platform.research import qlib_workflow
        bridge = OwnedQlibInputBridge(runtime.database, AccessPolicy(runtime.root), catalog)
        adapter = QlibExportAuthorizer(runtime, bridge, qlib_workflow)
        runtime.qlib_export_authorizer = adapter
        registry.register('data.qlib-register', adapter.register_handler,
            validate=validate_register_parameters, recoverable=False, auto_recovery_limit=0)
        if runtime.qlib_input_preparation is None:
            from .qlib_preparation import install_qlib_input_preparation
            from finance_platform.core.qlib_composition import project_qlib_dump_deployment
            deployment = runtime.qlib_dump_deployment
            if deployment is None and runtime.qlib_server_material is not None:
                deployment = project_qlib_dump_deployment(runtime, runtime.qlib_server_material)
            install_qlib_input_preparation(runtime, deployment=deployment)

"""Branches in the existing data.collect handler; zero SDK/collector retries."""
from contextlib import closing
from datetime import date,datetime,timezone
import time
from finance_platform.core.contracts import fingerprint, identifier
from finance_platform.data.contracts import DataError
from .contracts import FAMILY, LIMITS


def register_handlers(registry, runtime, service, runner):
    # Called by the candidate mount in place of the existing data registration.
    from finance_platform.data import jobs as legacy_jobs
    legacy_register=getattr(legacy_jobs,"_register_legacy_handlers",legacy_jobs.register_handlers)
    from finance_platform.core.jobs import HandlerRegistry
    legacy=HandlerRegistry(registry.root)
    legacy_register(legacy,runtime)
    old=legacy.get("data.collect")

    def validate(parameters):
        old.validate(parameters)

    def collect(context, parameters):
        d=service.dataset(context.run.owner_id,parameters["dataset_id"])
        if d.get("data_family")!=FAMILY:return old.handler(context,parameters)
        return execute(context,parameters,False)

    def execute(context, parameters, probe):
        owner=context.run.owner_id
        dataset_id=parameters["dataset_id"]
        d=service.dataset(owner,dataset_id)
        s=service.source(owner,d["source_id"],d["source_version"])
        if probe:
            frozen=parameters["resolved_call"]
            # Revalidate identity/profile against frozen bounds; never use the
            # worker's current date to silently move an already queued probe.
            end=date.fromisoformat(frozen["end"])
            if end>datetime.now(timezone.utc).date():raise DataError("SDK_PROBE_FREEZE_INVALID")
            from .contracts import source_request_parameters
            call=source_request_parameters(s,d,parameters["parameters"],probe=True,today=end)
            if call!=frozen:raise DataError("SDK_PROBE_FREEZE_INVALID")
        else:call=service.resolve_call(owner,dataset_id,parameters["parameters"])
        if parameters.get("max_pages",1)!=1:raise DataError("SDK_UNIT_LIMIT")
        collection_id=parameters.get("collection_id")
        if collection_id is None:
            root_run=context.run.run_id
            parent=context.run.parent_run_id
            with closing(runtime.database.connect()) as con:
                visited=set()
                while parent is not None:
                    if parent in visited:raise DataError("COLLECTION_PARENT_INVALID")
                    visited.add(parent)
                    row=con.execute("SELECT parent_run_id FROM runs WHERE run_id=? AND owner_id=?",(parent,owner)).fetchone()
                    if row is None:raise DataError("COLLECTION_PARENT_MISSING")
                    root_run,parent=parent,row[0]
            collection_id="collection-"+root_run
        identifier(collection_id)
        binding=fingerprint({"dataset_id":dataset_id,"source_hash":s["content_hash"],"call":call,"family":FAMILY})
        context.require_active()
        existing=None if probe else service.existing_unit(owner,dataset_id,collection_id,binding)
        def collected(result,reused):
            return {"data_family":FAMILY,"collection_id":collection_id,"result_ids":[result["result_id"]],"completed_units":1,"observed_rows":result["row_count"],"provider_verified":False,"coverage":"OBSERVED_SAMPLE_ONLY","raw_compatible":False,"reused_committed_unit":reused}
        if existing is not None:return collected(existing,True)
        # Preserve the legacy handler's recovery policy, but an SDK descendant
        # run may only reuse a committed unit, never reset an acquisition budget.
        if context.run.parent_run_id is not None:raise DataError("SDK_RETRY_REQUIRES_NEW_COLLECTION")
        if runner is None:raise DataError("SDK_EXECUTION_NOT_DISPATCHED")
        context.checkpoint(0,{"collection_id":collection_id,"data_family":FAMILY,"unit_ordinal":0})
        try:
            manifest,artifacts,execution=runner.run(context,owner,call,whole_deadline=time.monotonic()+LIMITS["job_seconds"])
            context.require_active()
            if probe:
                from .contracts import checked_json
                rows=checked_json(artifacts["observations.json"])
                # Private runner evidence stays local. A probe never archives or
                # commits observation/data_records/collection pages.
                return {"configuration":"CONFIGURED","dependency":"AVAILABLE","sdk_version":manifest["sdk_ref"]["version"],"public_endpoint_observed":execution["network_class"]=="BOUNDED_PUBLIC_SDK",
                        "fields":"VALIDATED_NONEMPTY","sample_rows":len(rows),"coverage":"OBSERVED_SAMPLE_ONLY","use_basis_state":"USER_DECLARED_PERSONAL_DEVELOPMENT","error_code":None}
            result=service.persist_capture(context,owner,dataset_id,collection_id,call,manifest,artifacts,execution)
            context.checkpoint(1,{"collection_id":collection_id,"result_id":result["result_id"],"row_count":result["row_count"]})
            return collected(result,False)
        except DataError as error:
            from finance_platform.data.collection import Collector
            Collector(service).event(owner,collection_id,"SDK_UNIT_FAILED",error.code)
            raise

    def validate_probe(parameters):
        if type(parameters)is not dict or set(parameters)!={"dataset_id","parameters","collection_id","max_pages","resolved_call"} or parameters["max_pages"]!=1 or type(parameters["resolved_call"])is not dict:
            raise DataError("SDK_PARAMETERS_INVALID")
        identifier(parameters["dataset_id"])
        frozen=parameters['resolved_call']
        from datetime import timedelta
        end=datetime.now(timezone.utc).date()
        if frozen.get('end')!=end.isoformat() or frozen.get('start')!=(end-timedelta(days=7)).isoformat() or frozen.get('max_rows')!=5 or frozen.get('operation')!='probe':
            raise DataError('SDK_PROBE_FREEZE_INVALID')

    # Same registry/JobService/Worker and lease mechanism. No second scheduler.
    registry.register("data.collect",collect,validate=validate,recoverable=old.recoverable,auto_recovery_limit=old.auto_recovery_limit)
    registry.register("data.connection_test",lambda c,p:execute(c,p,True),validate=validate_probe,recoverable=False,auto_recovery_limit=0)
    stream=legacy.get("data.stream")
    registry.register("data.stream",stream.handler,validate=stream.validate,recoverable=stream.recoverable)
    from ..history_jobs import register_history_handlers
    register_history_handlers(registry, runtime, service, runner)

"""Fixed business entry called inside the actual admitted FA013 TA child.

No model class, Graph substitute, process launcher or checkpoint manipulation.
The host supplies a server-selected model port and live private control channel.
"""
from . import tradingagents_workflow as native
from . import native_results
from .debate_tools import DebateReadonlyPorts
from .data import ResearchError

ENTRYPOINT="finance_platform.research.debate_native:execute_native_request"


def execute_native_request(request,attempt_root,*,model_ports,controls,resume=None):
    required={"binding","input_bundle","parent_run_id","source_manifest_sha256","release","native_entrypoint"}
    if type(request)is not dict or set(request)!=required or request["native_entrypoint"]!=ENTRYPOINT:
        raise ResearchError("TA_NATIVE_REQUEST_INVALID")
    if not all(callable(getattr(controls,key,None)) for key in
               ("verify_current_binding","require_active","assert_no_unapproved_effects","preserve_case_failure")):
        raise ResearchError("TA_ACTUAL_NATIVE_CONTROLS_REQUIRED")
    if not callable(getattr(model_ports,"trace_primitive",None)):
        raise ResearchError("TA_ACTUAL_MODEL_TRACE_REQUIRED")
    ports=None
    try:
        controls.verify_current_binding(request);controls.require_active()
        binding=native.binding(request["binding"]);bundle=request["input_bundle"]
        native.require_native_release(binding,request["release"])
        if request["source_manifest_sha256"]!=request["release"]["source_manifest_sha256"]:
            raise ResearchError("TA_NATIVE_SOURCE_BINDING_MISMATCH")
        if native_results.digest(bundle)!=binding["tools_manifest_hash"]:raise ResearchError("TA_NATIVE_TOOL_BINDING_MISMATCH")
        if 'team_snapshot' in bundle:
            configure=getattr(model_ports,'configure_team_snapshot',None)
            if not callable(configure):raise ResearchError('TA_TEAM_MODEL_PORT_REQUIRED')
            configure(bundle['team_snapshot'])
        approval=bundle.get("provider_approval")
        if approval is not None:
            configure=getattr(model_ports,"configure_provider",None)
            exchange=getattr(controls,"invoke_provider",None)
            if not callable(configure) or not callable(exchange) or approval.get("options")!=binding["options"]:
                raise ResearchError("TA_PROVIDER_PORT_BINDING_INVALID")
            configure(approval,exchange)
        ports=DebateReadonlyPorts(bundle,model_ports,active=controls.require_active,
            assert_effects=controls.assert_no_unapproved_effects)
        staged=native.run_native(binding,binding["options"],attempt_root,proof=request["release"],
            ports=ports,active=controls.require_active,resume=resume)
        controls.assert_no_unapproved_effects()
        # The actual native object stays in the child until post-core ACK.
        return staged
    except BaseException as error:
        try:
            trace=model_ports.trace_primitive()
            if type(trace)is not list or len(trace)>4096:raise ResearchError("TA_FAILURE_TRACE_INVALID")
            budget=None if ports is None else getattr(ports,"budget",None)
            spent=None if budget is None else budget.consumed_for_retry()
            controls.preserve_case_failure("SERVER_PROFILE",trace,spent)
        except BaseException:
            # Preserve the original failure even if its evidence channel fails.
            # Host eligibility must reject missing/unproven evidence, never
            # invent a zero budget or treat this as a successful checkpoint.
            BaseException.add_note(error,"TA_FAILURE_EVIDENCE_UNPROVEN")
        raise

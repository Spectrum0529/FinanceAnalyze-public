"""Local closure persistence; early no-effect failures stay in bounded journal.

This is not a historical closure adoption/recovery API. Current helper must
already have closed its worker/transport and checked direct-child emptiness.
"""
from .protocol import require,exact,identifier,sha,digest,encoded,strict_json


def validate_early_closure(binding,runtime,receipt):
    exact(binding,'session_id attempt_id job_id run_id fencing_token')
    for key in('session_id','attempt_id','job_id','run_id'):identifier(binding[key])
    require(type(binding['fencing_token'])is int and binding['fencing_token']>0,'EARLY_CLOSURE_BINDING_INVALID')
    sha(runtime)
    exact(receipt,'binding_sha256 runtime_sha256 helpers_closed containers_closed pipe_eof active_read_counters cleanup_budget')
    require(receipt['binding_sha256']==digest(binding)and receipt['runtime_sha256']==runtime
        and receipt['helpers_closed']is True and receipt['containers_closed']is True
        and type(receipt['pipe_eof'])is bool,'EARLY_CLOSURE_PROOF_INVALID')
    counters=receipt['active_read_counters'];budget=receipt['cleanup_budget']
    exact(counters,'max_age_ms local_checks remote_calls cache_hits remote_failures remote_true_replies typed_stopped_replies invalid_replies invalidations remote_milliseconds')
    require(all(type(v)is int and 0<=v<=2**63-1 for v in counters.values())and counters['max_age_ms']==500,'EARLY_CLOSURE_COUNTERS_INVALID')
    exact(budget,'local_limit_ms remaining_ms wire_updates kernel_enforced')
    require(type(budget['local_limit_ms'])is int and budget['local_limit_ms']==24000
        and type(budget['remaining_ms'])is int and 0<budget['remaining_ms']<=24000
        and type(budget['wire_updates'])is int and 0<=budget['wire_updates']<=2**63-1
        and budget['kernel_enforced']is True,'EARLY_CLOSURE_BUDGET_INVALID')


def persist_local_closure(controller,binding,runtime,receipt,*,cleanup):
    """Called after original physical local closure checks, never before.

    No verified controller/ownership state -> no new file/proof/HELPER_CLOSED.
    Normal plan branch retains original xb+fsync path and finalize ordering.
    """
    cleanup.check()
    require(controller is not None and controller.worker_joined is True
        and controller._transport_close_failed is False,'LOCAL_CLOSURE_CONTROLLER_UNVERIFIED')
    if controller._plan is None:
        require(not controller._owned_requests,'EARLY_CLOSURE_OWNED_EFFECTS_PRESENT')
        return controller.journal.record_unreserved_closure(binding,runtime,receipt,budget=cleanup)
    identifier(binding['attempt_id'])
    closed_path=controller.root/('closure-'+binding['attempt_id']+'.json')
    # The same normal reserved-plan proof is written before finalize/host ACK.
    with closed_path.open('xb')as stream:
        cleanup.check();stream.write(encoded(receipt));stream.flush()
        import os
        os.fsync(stream.fileno());cleanup.check()
    return {'storage':'RESERVED_PLAN_FILE','path':str(closed_path)}

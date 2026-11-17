"""Action scheduling only; economics are owned by the shared trading bridge."""
from finance_platform.core.contracts import parse_instant,fingerprint,payload
from .data import ResearchError


def dispatch_at(record):
    from finance_platform.trading.research_actions import action_due_at
    return action_due_at(record)


def action_records(value):
    if not isinstance(value,dict) or value.get("schema_version")!="research-input-3":
        return ()
    return tuple(value["execution"].get("actions",()))


def apply(boundary,record,now):
    from finance_platform.trading.research_actions import ResearchActionBridge
    from .replay import preflight_action
    preflight_action(boundary.service,boundary.principal,boundary.account_id,record,now)
    if not hasattr(boundary,"action_bridge"):
        boundary.action_bridge=ResearchActionBridge(boundary.service,boundary.principal,boundary.account_id,
             source_id=boundary.sample.source_id,license_id=boundary.sample.license_id,scope=boundary.sample.scope,
             authorize_action=getattr(boundary,"action_authorizer",None))
    receipt=boundary.action_bridge.apply(record,now)
    if receipt["status"] in {"BLOCKED","PENDING_CANCEL"}:
        raise ResearchError(receipt["code"])
    return receipt

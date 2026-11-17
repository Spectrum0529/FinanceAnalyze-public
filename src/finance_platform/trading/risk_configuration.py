"""Immutable risk versions with an active projection; initial account terms remain frozen."""
import json
from datetime import datetime, timezone

from finance_platform.core.contracts import canonical, fingerprint, identifier
from .accounting import TradingError
from .risk import RiskLimits


def ensure_initial(con, account):
    active = con.execute("SELECT version FROM trading_risk_active WHERE account_id=?", (account["account_id"],)).fetchone()
    if active:
        return active["version"]
    limits = RiskLimits.from_dict(json.loads(account["limits_json"]))
    body, sha = canonical(limits), fingerprint(limits)
    con.execute("INSERT INTO trading_risk_versions VALUES(?,?,?,?,?,?)", (account["account_id"], "initial", body, sha, account["owner_id"], datetime.now(timezone.utc).isoformat()))
    con.execute("INSERT INTO trading_risk_active VALUES(?,?)", (account["account_id"], "initial"))
    return "initial"


def policy(con, account, version=None):
    if version is None:
        active = con.execute("SELECT version FROM trading_risk_active WHERE account_id=?", (account["account_id"],)).fetchone()
        version = active["version"] if active else "initial"
    row = con.execute("SELECT * FROM trading_risk_versions WHERE account_id=? AND version=?", (account["account_id"], version)).fetchone()
    if row is None and version == "initial":
        limits = RiskLimits.from_dict(json.loads(account["limits_json"]))
        return limits, {"version": "initial", "content_hash": fingerprint(limits)}
    if row is None:
        raise TradingError("RISK_VERSION_NOT_FOUND")
    return RiskLimits.from_dict(json.loads(row["body"])), {"version": version, "content_hash": row["content_hash"]}


def intent_policy(con, account, client_order_id):
    bound = con.execute("SELECT version FROM trading_intent_risk WHERE client_order_id=?", (client_order_id,)).fetchone()
    return policy(con, account, "initial" if bound is None else bound["version"])[0]

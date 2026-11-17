"""Immutable model-processing frontier, shared by all local trading writers."""
import json
from finance_platform.core.contracts import canonical, fingerprint, instant, parse_instant
from .accounting import TradingError

VERSION = "account-execution-1"


def clock_stamp(at):
    return instant(parse_instant(at) if type(at) is str else at).isoformat(timespec="microseconds")


def read_clock(con, account):
    row = con.execute("SELECT at,body FROM trading_execution_steps WHERE account_id=? ORDER BY sequence DESC LIMIT 1", (account["account_id"],)).fetchone()
    barrier = con.execute("SELECT 1 FROM trading_execution_barriers WHERE account_id=? LIMIT 1", (account["account_id"],)).fetchone()
    seed = con.execute("SELECT body FROM trading_execution_steps WHERE account_id=? AND operation_key='initial'", (account["account_id"],)).fetchone()
    status = "BROKER_EVENT_CLOCK" if account["mode"] == "BROKER_PAPER" else json.loads(seed["body"])["history_status"] if seed else "UNINITIALIZED"
    return {"version": VERSION, "account_id": account["account_id"], "mode": account["mode"],
            "watermark": row["at"] if row else clock_stamp(account["created_at"]), "initial_at": account["created_at"],
            "history_status": "LEGACY_EXECUTION_HISTORY_INVALID" if barrier else status, "blocked": bool(barrier)}


def require_forward(con, account, at):
    clock = read_clock(con, account)
    if account["mode"] == "BROKER_PAPER":
        return clock
    if clock["blocked"]:
        raise TradingError("ACCOUNT_EXECUTION_HISTORY_INVALID")
    if clock_stamp(at) < clock["watermark"]:
        raise TradingError("ACCOUNT_EXECUTION_TIME_REVERSED")
    return clock


def advance(con, account, key, at, body):
    """Caller owns BEGIN IMMEDIATE: side effect and frontier commit or roll back."""
    if account["mode"] == "BROKER_PAPER":
        return
    if not con.in_transaction:
        raise TradingError("EXECUTION_TRANSACTION_REQUIRED")
    encoded = canonical(body)
    old = con.execute("SELECT body FROM trading_execution_steps WHERE account_id=? AND operation_key=?", (account["account_id"], key)).fetchone()
    if old:
        if old["body"] != encoded:
            raise TradingError("EXECUTION_IDENTITY_CONFLICT")
        return
    require_forward(con, account, at)
    con.execute("INSERT INTO trading_execution_steps(account_id,operation_key,at,body) VALUES(?,?,?,?)", (account["account_id"], key, clock_stamp(at), encoded))


def seed(con, account, *, legacy=False):
    if con.execute("SELECT 1 FROM trading_execution_steps WHERE account_id=? LIMIT 1", (account["account_id"],)).fetchone():
        return
    times = [parse_instant(account["created_at"])]
    anomalies = []
    prior = times[0]
    if legacy and account["mode"] == "LOCAL_SIMULATION":
        # Preserve originals. This is an observed lower bound, never preregistration.
        for row in con.execute("SELECT sequence,event_id,at,body FROM trading_ledger WHERE account_id=? ORDER BY sequence", (account["account_id"],)):
            at = parse_instant(row["at"])
            if at < prior:
                anomalies.append({"sequence": row["sequence"], "event_id": row["event_id"], "at": row["at"], "prior_watermark": clock_stamp(prior), "body_hash": fingerprint(json.loads(row["body"]))})
            prior = max(prior, at)
            times.append(at)
        for table, column in (("trading_intents", "created_at"), ("trading_research_actions", "at"), ("trading_pending_actions", "at"), ("trading_action_resolutions", "at")):
            times.extend(parse_instant(r[0]) for r in con.execute(f"SELECT {column} FROM {table} WHERE account_id=?", (account["account_id"],)))
        times.extend(parse_instant(json.loads(r[0])["available_at"]) for r in con.execute("SELECT body FROM trading_market WHERE account_id=?", (account["account_id"],)))
    status = "LEGACY_RECONSTRUCTED_LOWER_BOUND" if legacy else "REGISTERED"
    con.execute("INSERT INTO trading_execution_steps(account_id,operation_key,at,body) VALUES(?,?,?,?)", (account["account_id"], "initial", clock_stamp(max(times)), canonical({"history_status": status, "version": VERSION})))
    if anomalies:
        con.execute("INSERT INTO trading_execution_barriers VALUES(?,?,?)", (account["account_id"], VERSION, canonical({"code": "ACCOUNT_EXECUTION_HISTORY_INVALID", "anomalies": anomalies})))
        con.execute("UPDATE trading_accounts SET frozen=1,freeze_reason='ACCOUNT_EXECUTION_HISTORY_INVALID' WHERE account_id=?", (account["account_id"],))

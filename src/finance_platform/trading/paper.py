"""Non-secret paper configuration and explicit worker operations."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os

from finance_platform.core.contracts import canonical
from .accounting import TradingError
from .alpaca import AlpacaPaperConfig, AlpacaPaperBroker
from .service import stamp


def configure(service, principal, account_id, config: AlpacaPaperConfig):
    if type(config) is not AlpacaPaperConfig or config.account_id != account_id:
        raise TradingError("PAPER_ACCOUNT_MODE_MISMATCH")
    with service.database.transaction() as con:
        account = service._account(con, account_id, principal, True)
        if account["mode"] != "BROKER_PAPER":
            raise TradingError("PAPER_ACCOUNT_MODE_MISMATCH")
        old = con.execute("SELECT body FROM trading_paper_configs WHERE account_id=? ORDER BY sequence DESC LIMIT 1", (account_id,)).fetchone()
        if old:
            if old["body"] != canonical(config):
                raise TradingError("PAPER_ACCOUNT_BINDING_IMMUTABLE")
            return
        con.execute("INSERT INTO trading_paper_configs(account_id,body,at) VALUES(?,?,?)", (account_id, canonical(config), stamp(datetime.now(timezone.utc))))
        service._freeze(con, account_id, "PAPER_CONFIGURATION_RECONCILIATION_REQUIRED")
        service.database.audit(principal.user_id, "trading.paper_configured", account_id, {"mode": "BROKER_PAPER"}, connection=con)


def load_config(service, principal, account_id):
    from finance_platform.core.contracts import RunMode
    with closing(service.database.connect()) as con:
        service._account(con, account_id, principal)
        row = con.execute("SELECT body FROM trading_paper_configs WHERE account_id=? ORDER BY sequence DESC LIMIT 1", (account_id,)).fetchone()
        if row is None:
            raise TradingError("PAPER_NOT_CONFIGURED")
        values = json.loads(row["body"])
        values["mode"] = RunMode(values["mode"])
        return AlpacaPaperConfig(**values)


def status(service, principal, account_id):
    try:
        config = load_config(service, principal, account_id)
    except TradingError as exc:
        if exc.code == "PAPER_NOT_CONFIGURED":
            return {"mode": "BROKER_PAPER", "connection": "NOT_CONFIGURED", "verification": "NOT_RUN", "external_dependency": "WAITING_ACCOUNT", "live": "DISABLED"}
        raise
    configured = bool(os.environ.get(config.key_env) and os.environ.get(config.secret_env))
    return {"mode": "BROKER_PAPER", "connection": "SECRET_REFERENCES_CONFIGURED" if configured else "WAITING_SECRET",
            "key_env": config.key_env, "secret_env": config.secret_env, "masked": "********" if configured else None,
            "verification": "NOT_RUN", "external_dependency": "WAITING_ACCOUNT" if not configured else "WAITING_OBSERVATION",
            "endpoint": "https://paper-api.alpaca.markets", "live": "DISABLED"}

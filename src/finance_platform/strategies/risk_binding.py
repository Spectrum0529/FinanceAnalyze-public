"""Readonly validation of risk declarations before core RunSpec registration."""
from contextlib import closing
from finance_platform.core.contracts import payload
from finance_platform.trading.risk_configuration import policy
from .contracts import StrategyError


def validate_risk_binding(trading, principal, account_id, declaration):
    if not isinstance(declaration,dict) or set(declaration)!={"account_id","version","content_hash","limits"} or declaration["account_id"]!=account_id:
        raise StrategyError("STRATEGY_RISK_BINDING_INVALID")
    with closing(trading.database.connect()) as con:
        account=trading._account(con,account_id,principal)
        limits,reference=policy(con,account,declaration["version"])
    if declaration!={"account_id":account_id,**reference,"limits":payload(limits)}:
        raise StrategyError("STRATEGY_RISK_BINDING_MISMATCH")

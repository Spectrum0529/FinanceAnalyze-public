"""Price-path units and source transform policy, never shareholder total return."""
from decimal import Decimal,localcontext
from finance_platform.core.contracts import parse_instant
from finance_platform.data.provider_sdk.contracts import number
from finance_platform.data.provider_sdk.history_contracts import (HISTORY_PROFILE, NATIVE_INTERFACE, NATIVE_SOURCE_ID,
    NATIVE_PRICE_POLICY as NATIVE_MODE)
from .daily import DailyError

YAHOO_MODES={"YAHOO_NATIVE_SPLIT_ADJUSTED_PRICE_PATH_V1","YAHOO_NATIVE_SPLIT_ADJUSTED_PRICE_ONLY_DIVIDENDS_NOT_CREDITED"}
SINA_MODE="SINA_QFQ_AFFINE_OBSERVED_V1"
QUANTITY_BASIS="ADJUSTED_SIMULATION_UNITS"
COST_SEMANTICS={"version":"financeanalyze-daily-costs-1","quantity_basis":QUANTITY_BASIS,
    "fee_basis":"PER_ADJUSTED_UNIT_WITH_MINIMUM",
    "spread_basis":"LONG_ENTRY_PRICE_MARKUP_RAW_OPEN_EXIT",
    "price_tick_basis":"NO_CENT_TICK_QUANTIZATION_FLOAT64",
    "actions":"NO_ADDITIONAL_SPLIT_DIVIDEND_OR_REINVESTMENT"}
NATIVE_LABEL="AKShare原生前复权（上游四位舍入）"
NATIVE_FLAGS={"SDK_NATIVE_QFQ_FLOAT64_ROUND4","UPSTREAM_FULL_FRAME_FFILL_THEN_BFILL",
    "NO_INDEPENDENT_RAW_ROW_OR_FACTOR_CHAIN_CERTIFICATION",
    "PRICE_PATH_ONLY_NO_DIVIDEND_CREDIT_NO_DOUBLE_SPLIT",
    "VOLUME_UNIT_AND_COMPANY_ACTION_BASIS_UNVERIFIED"}


def native_semantics():
    return {"source_id":NATIVE_SOURCE_ID,"interface":NATIVE_INTERFACE,
        "calculation":"UPSTREAM_PANDAS_FLOAT64_ROUND4_V1",
        "fill":"UPSTREAM_FULL_FRAME_FFILL_THEN_BFILL",
        "quantity_basis":QUANTITY_BASIS,"volume_unit":"UNVERIFIED",
        "volume_adjustment":"UPSTREAM_UNADJUSTED",
        "company_action_quantity_basis":"UNVERIFIED",
        "independent_raw_row_or_factor_chain_proof":False,
        "historical_availability":"NOT_PIT_CERTIFIED"}


def catalog_price_policies():
    """Declared current paths, never actual acquired coverage or old relabelling."""
    return {"yfinance":{"modes":sorted(YAHOO_MODES),"display_name":"拆股调整价格模拟（不含分红）",
                "quantity_basis":QUANTITY_BASIS,"total_return":False},
            "akshare":{"mode":NATIVE_MODE,"display_name":NATIVE_LABEL,
                "description":"上游对完整表前向/后向填充；实际覆盖与缺口未认证，volume单位和公司行为数量未认证。",
                **native_semantics(),"price_unit":"USD_per_adjusted_simulation_unit",
                "total_return":False,"cost_semantics":dict(COST_SEMANTICS),
                "legacy_supported_modes":[SINA_MODE],"legacy_results":"PRESERVE_STORED_POLICY_NO_RECALCULATION"}}


def _native_policy(view,request,policy):
    if request.ticker in {"CIEN","AI"}:raise DailyError("DAILY_KNOWN_FACTOR_QUALITY_RESTRICTION")
    try:
        if (view["source_ref"]["source_id"]!=NATIVE_SOURCE_ID
                or view.get("profile")!=HISTORY_PROFILE
                or view["request_parameters"].get("provider")!="akshare"
                or view["request_parameters"]["interface"]!=NATIVE_INTERFACE
                or view.get("corporate_action_basis") is not None
                or view.get("price_transform") is not None
                or view.get("data_quality") is not None):raise ValueError
        for row in view["rows"]:
            loc=row["sdk_row_locator"]
            flags=row["sdk_transform_flags"]
            if (row["price_policy"]!=NATIVE_MODE or row["quantity_basis"]!=QUANTITY_BASIS
                    or row["price_unit"]!="USD_per_adjusted_simulation_unit"
                    or row["volume_unit"]!="UNVERIFIED" or row["source_reported_volume"]!=row["volume"]
                    or row.get("raw_row_locator") is not None or row.get("factor_locator") is not None
                    or row.get("source_unadjusted_ohlc") is not None
                    or type(loc)is not dict or loc.get("artifact")!="sdk-frame.json"
                    or type(loc.get("row"))is not int or loc["row"]<0
                    or type(flags)is not list or any(type(flag)is not str for flag in flags)
                    or set(flags)!=NATIVE_FLAGS):raise ValueError
    except (KeyError,TypeError,ValueError):raise DailyError("DAILY_NATIVE_POLICY_BINDING_REQUIRED") from None
    return {"mode":NATIVE_MODE,"display_name":NATIVE_LABEL,"source_policy":policy,
            "source_ref":dict(view["source_ref"]),"native_transform":native_semantics(),
            "quantity_basis":QUANTITY_BASIS,"volume_unit":"UNVERIFIED",
            "cost_semantics":dict(COST_SEMANTICS),"factor_ref":None,"total_return":False,
            "adjustment_anchor":"SOURCE_FROZEN_ACQUISITION_NOT_ANCHOR_INVARIANT"}


def validate_price_policy(view,request):
    policy=view.get("price_policy")
    mode=policy.get("mode") if type(policy)is dict else policy
    provider=request.provider
    if view.get("quantity_basis")!=QUANTITY_BASIS:raise DailyError("DAILY_QUANTITY_BASIS_UNSUPPORTED")
    if provider=="yfinance":
        if mode not in YAHOO_MODES:raise DailyError("DAILY_PRICE_POLICY_UNSUPPORTED")
        name="拆股调整价格模拟（不含分红）"
    elif provider=="akshare":
        if mode==NATIVE_MODE:return _native_policy(view,request,policy)
        if mode!=SINA_MODE:raise DailyError("DAILY_PRICE_POLICY_UNSUPPORTED")
        if request.ticker in {"CIEN","AI"}:raise DailyError("DAILY_KNOWN_FACTOR_QUALITY_RESTRICTION")
        # The data transform owns all d/f/c parsing, interval propagation and
        # affine OHLC math. We require an immutable factor evidence reference,
        # not client-supplied adjustment values or an unadjusted fallback.
        factor=view.get("price_transform")
        if view.get("corporate_action_basis")!=SINA_MODE or type(factor)is not dict or factor.get("price_policy")!=SINA_MODE or factor.get("transform")!="SINA_QFQ_DECIMAL_EXACT_AFFINE_96_V1" or factor.get("interval_rule")!="ASCENDING_FACTOR_FORWARD_PROPAGATION_FIRST_INTERVAL_BACK_EXTENDED" or factor.get("precision")!="DECIMAL_96_NO_QUANTIZATION_NOT_SDK_FLOAT_ROUND4" or factor.get("atomic_server_snapshot_claim")is not False:
            raise DailyError("DAILY_FACTOR_BINDING_REQUIRED")
        for field in ("factor_chain_sha256","factor_entity_sha256","price_entity_sha256"):
            if type(factor.get(field))is not str or len(factor[field])!=64 or any(c not in "0123456789abcdef" for c in factor[field]):raise DailyError("DAILY_FACTOR_BINDING_REQUIRED")
        try:
            if type(factor["factor_rows"])is not int or not 1<=factor["factor_rows"]<=4096 or Decimal(factor["anchor_f"])!=1 or Decimal(factor["anchor_c"])!=0:raise ValueError
            for k in ("price_received_at","factor_received_at"):parse_instant(factor[k])
            for row in view["rows"]:
                loc=row["factor_locator"]
                if row["price_policy"]!=SINA_MODE or row["quantity_basis"]!=QUANTITY_BASIS or row["price_unit"]!="USD_per_adjusted_simulation_unit" or loc["factor_chain_sha256"]!=factor["factor_chain_sha256"]:raise ValueError
                f,c=Decimal(loc["f"]),Decimal(loc["c"])
                if not f.is_finite() or not c.is_finite() or f<=0:raise ValueError
                with localcontext() as ctx:
                    ctx.prec=96
                    for key in ("open","high","low","close"):
                        if Decimal(number(row["source_unadjusted_ohlc"][key]))*f+c!=Decimal(number(row[key])):raise ValueError
        except (KeyError,TypeError,ValueError,ArithmeticError):raise DailyError("DAILY_FACTOR_BINDING_REQUIRED") from None
        name="前复权价格路径模拟（含源端分红调整）"
    else:raise DailyError("DAILY_PRICE_POLICY_UNSUPPORTED")
    return {"mode":mode,"display_name":name,"source_policy":policy,"cost_semantics":dict(COST_SEMANTICS),
            "factor_ref":factor if provider=="akshare" else None,"total_return":False,
            "adjustment_anchor":"SOURCE_FROZEN_ACQUISITION_NOT_ANCHOR_INVARIANT"}

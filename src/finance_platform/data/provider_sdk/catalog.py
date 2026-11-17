"""Declared SDK capability metadata, never acquired coverage or licensed rights."""
def sdk_catalog_entries():
    return [{"provider":provider,"name":name,"assets":["US_EQUITY"] if provider=="akshare" else ["US_EQUITY","US_ETF"],"frequencies":["1Day"],
             "capabilities":["provider_observation","bounded_daily_OHLCV","observed_receipt"] + (["stock_us_daily_native_qfq_v1"] if provider=="akshare" else []),
             "data_family":"provider-observation-1","price_basis":"PROVIDER_OBSERVED","raw_compatible":False,
             "history":"UNKNOWN_NOT_ACQUIRED","verified_market_coverage":[],"rights":"USER_DECLARATION_REQUIRED_EXTERNAL_RIGHTS_UNVERIFIED",
             "rights_verified":False,"formal_research_approved":False,"automatic_export":False,"export_purpose_required":True,
             "external_dependency":"PINNED_SDK_REQUIRED","sdk_version":version}
            for provider,name,version in [("yfinance","yfinance 美股/ETF 日线观察","1.8.0"),("akshare","AKShare原生前复权（上游四位舍入）","1.19.3+guard1")]]


def catalog_with_sdk():
    from finance_platform.data.catalog import builtin_catalog
    return builtin_catalog()+sdk_catalog_entries()

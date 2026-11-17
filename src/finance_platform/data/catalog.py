"""Capabilities are declarations, never certified market coverage or rights."""


def _legacy_catalog():
    return [
        {"provider": "synthetic", "name": "合成开发与离线协议重放", "assets": ["US_EQUITY", "US_ETF"],
         "frequencies": ["1Day", "1Min"], "capabilities": ["bars", "identities", "actions", "PIT"],
         "history": "USER_FIXTURE_CLOCK_ONLY", "verified_market_coverage": [], "rights": "SYNTHETIC_ONLY", "external_dependency": "NONE"},
        {"provider": "local", "name": "本地 CSV / Parquet", "assets": ["US_EQUITY", "US_ETF"], "frequencies": ["1Day", "1Min"],
         "capabilities": ["field_mapping", "preview", "error_rows", "import", "licensed_export"], "history": "INPUT_DECLARED_NOT_CERTIFIED",
         "verified_market_coverage": [], "rights": "EXPLICIT_SOURCE_DECLARATION_REQUIRED", "external_dependency": "WAITING_DATA_RIGHTS"},
        {"provider": "nyse", "name": "NYSE Closing Prices / Reference 既有受控重放", "assets": ["US_EQUITY", "US_ETF"], "frequencies": ["1Day"],
         "capabilities": ["original_price_parser", "reference_parser", "guarded_raw_replay"], "history": "ORIGINAL_S02_EVIDENCE_UNCHANGED",
         "verified_market_coverage": [], "rights": "S02.06_UNVERIFIED", "external_dependency": "WAITING_DATA_RIGHTS"},
        {"provider": "alpaca", "name": "Alpaca US raw stock bars / WebSocket", "assets": ["US_EQUITY", "US_ETF"], "frequencies": ["1Day", "1Min"],
         "capabilities": ["REST_bars", "pagination", "IEX", "SIP", "WS_bars", "WS_updatedBars"],
         "fields": ["t", "o", "h", "l", "c", "v", "n", "vw"], "history": "PROVIDER_DOCUMENTED_SINCE_2016_NOT_ACQUIRED",
         "verified_market_coverage": [], "rights": "ACCOUNT_PLAN_AND_USES_REQUIRE_EVIDENCE", "external_dependency": "WAITING_SECRET",
         "documentation": "https://docs.alpaca.markets/us/reference/stockbars"},
        {"provider": "sec", "name": "SEC EDGAR submissions / companyfacts", "assets": ["ISSUER_CIK_NOT_SECURITY"], "frequencies": ["filing"],
         "capabilities": ["submissions", "historical_files", "companyfacts", "taxonomy_unit_accession_preservation"],
         "history": "API_RESPONSE_OBSERVED_ONLY_NO_HISTORICAL_AVAILABILITY_INFERRED", "verified_market_coverage": [],
         "rights": "PUBLIC_ACCESS_POLICY_APPLIES_NO_FORMAL_RESEARCH_CERTIFICATION", "external_dependency": "WAITING_USER_DECISION",
         "documentation": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces"},
        {"provider": "rest", "name": "受限 JSON rows/token 扩展协议", "assets": [], "frequencies": [],
         "capabilities": ["HTTPS_allowlist", "canonical_record_schema", "opaque_token_pagination"], "history": "NONE_DECLARED",
         "verified_market_coverage": [], "rights": "SERVER_POLICY_AND_SOURCE_LICENSE_REQUIRED", "external_dependency": "WAITING_USER_DECISION"},
    ]


def builtin_catalog():
    from .provider_sdk.catalog import sdk_catalog_entries
    return _legacy_catalog()+sdk_catalog_entries()

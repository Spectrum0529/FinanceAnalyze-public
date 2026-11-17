"""Known ETF benchmark proxies, with identity bound to the actual chart."""
import re

from .contracts import DataError


_ETF_PROXIES = (
    ("sp500", "SPY", "标普 500 ETF（SPY）", "ARCX",
     "https://www.ssga.com/us/en/institutional/etfs/state-street-spdr-sp-500-etf-trust-spy"),
    ("nasdaq100", "QQQ", "纳斯达克 100 ETF（QQQ）", "XNAS",
     "https://www.invesco.com/us/en/financial-products/etfs/invesco-qqq-trust-series-1.html"),
    ("csi300", "ASHR", "沪深 300 ETF（ASHR，美国上市）", "ARCX",
     "https://etf.dws.com/en-us/AssetDownload/Inline/d17db09a-fa57-4a42-b8cc-cd18361af1e9/ASHR-SUM.pdf"),
)
_PROFILE = {
    "provider": "yfinance", "market": "US", "asset_class": "US_ETF",
    "currency": "USD", "adjustment": "split_adjusted",
}


def presets():
    return [{**_PROFILE, "index_id": index_id, "ticker": ticker, "label": label,
             "mic": mic, "identity_source": source}
            for index_id, ticker, label, mic, source in _ETF_PROXIES]


def capture_identity(provider, symbols, metadata, securities):
    preset = next((item for item in presets()
                   if item["provider"] == provider and symbols == [item["ticker"]]), None)
    if preset is None:
        return None
    if type(securities) is not list or len(securities) != 1 or type(securities[0]) is not dict:
        raise DataError("BENCHMARK_IDENTITY_UNVERIFIED")
    security = securities[0]
    instrument = security.get("instrument")
    reference = security.get("identity_reference")
    if (security.get("ticker") != preset["ticker"]
            or type(instrument) is not dict
            or any(instrument.get(key) != preset[key] for key in ("ticker", "asset_class", "mic", "currency"))
            or type(reference) is not dict or set(reference) != {"security_id", "content_hash"}
            or type(reference["security_id"]) is not str or not reference["security_id"]
            or type(reference["content_hash"]) is not str or not re.fullmatch("[0-9a-f]{64}", reference["content_hash"])
            or metadata.get("currency") != instrument["currency"]
            or metadata.get("adjustment") != preset["adjustment"]
            or metadata.get("timezone") != "America/New_York"
            or metadata.get("price_unit") != "per_adjusted_simulation_unit"
            or metadata.get("volume_unit") != "provider_reported_unverified"):
        raise DataError("BENCHMARK_IDENTITY_UNVERIFIED")
    return {**preset, "currency": instrument["currency"],
            "identity_basis": "ISSUER_ETF_AND_YAHOO_CHART_METADATA",
            "provider_identity_reference": dict(reference)}

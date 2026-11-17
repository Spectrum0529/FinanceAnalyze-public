"""Candidate only: full public AKShare algorithm, with two installed guards.

Run only in the existing single-call SDK child after AccessPolicy, distribution
verification and Windows job limits. No market access from a direct CLI.
"""
from datetime import date
import hashlib
import importlib
from importlib import metadata
from pathlib import Path
import re
from threading import Lock
from types import SimpleNamespace

import numpy as np
import pandas as pd

INTERFACE = "stock_us_daily_native_qfq_v1"
SOURCE_ID = "history-v4-akshare-stock-us-daily-native-qfq"
PRICE_POLICY = "AKSHARE_STOCK_US_DAILY_NATIVE_QFQ_ROUND4_V1"
ADAPTATION = "AKSHARE_NATIVE_QFQ_GUARDS_V1"
UPSTREAM_RELEASE = "1.19.3"
RELEASE = "1.19.3+guard1"
GUARDED_MODULE_SHA256 = "3cd5d3c37529eb18a1a5fd3a0a542deca323ede66c222d4b0b066078c086487a"
CONS_SHA256 = "435bf1763531d0eb33d9a8ba25fbc9ab3c5bcc11bbd7834978cbdfdec4c0bbb9"
WHEEL_SHA256 = "a93cbe67acf2042e922bc05a3a817ee51406e21855b8da98e23fa60b18325b12"
_SDK_LOCK = Lock()
_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _inputs(call):
    symbol = call.get("provider_symbol")
    if (call.get("provider") != "akshare" or call.get("interface") != INTERFACE
            or type(symbol) is not str or not re.fullmatch(r"[A-Z][A-Z0-9]{0,17}", symbol)):
        raise ValueError("SDK_PARAMETERS_INVALID")
    # Upstream itself documents these factor series as incorrect.
    if symbol in {"CIEN", "AI"}:
        raise ValueError("AKSHARE_NATIVE_KNOWN_FACTOR_INVALID")
    start, end = call.get("start"), call.get("end")
    if type(start) is not str or type(end) is not str:
        raise ValueError("SDK_PARAMETERS_INVALID")
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if (a.isoformat() != start or b.isoformat() != end or a < date(1900, 1, 2)
            or not 0 < (b-a).days <= 11346 or b > date.today()
            or type(call.get("max_rows")) is not int or not 0 < call["max_rows"] <= 9000):
        raise ValueError("SDK_PARAMETERS_INVALID")
    return symbol


def _load_sdk():
    """Refuse the official unpatched package; never rewrite runtime source."""
    dist = metadata.distribution("akshare")
    if dist.version != RELEASE:
        raise ValueError("SDK_VERSION_UNREVIEWED")
    for name, digest in (("stock_us_sina.py", GUARDED_MODULE_SHA256), ("cons.py", CONS_SHA256)):
        path = Path(dist.locate_file("akshare/stock/" + name))
        if _sha(path.read_bytes().replace(b"\r\n", b"\n")) != digest:
            raise ValueError("SDK_INTERFACE_CHANGED")
    ak = importlib.import_module("akshare")
    module = importlib.import_module("akshare.stock.stock_us_sina")
    if (ak.stock_us_daily is not module.stock_us_daily
            or Path(module.__file__).resolve() != Path(dist.locate_file("akshare/stock/stock_us_sina.py")).resolve()):
        raise ValueError("SDK_INTERFACE_CHANGED")
    return ak, module


class _FixedRequests:
    """Bind the SDK's two GETs to existing bounded SinaHTTP ports."""
    def __init__(self, symbol, price_port, factor_port, budget):
        self.budget = budget
        self.routes = {
            "https://finance.sina.com.cn/staticdata/us/" + symbol:
                (price_port, "sina_us_full_history"),
            "https://finance.sina.com.cn/us_stock/company/reinstatement/" + symbol + "_qfq.js":
                (factor_port, "sina_qfq_factors"),
        }
        self.used = set()

    def get(self, url, **unknown):
        if unknown or url not in self.routes or url in self.used:
            raise ValueError("SDK_DESTINATION_FORBIDDEN")
        self.used.add(url)
        port, role = self.routes[url]
        response = port.get(url)
        raw = response.body
        # Original bounded entity, including failures, precedes SDK parsing.
        self.budget.entities.append({
            "role": role, "origin": "finance.sina.com.cn",
            "path": url.removeprefix("https://finance.sina.com.cn"), "parameters": {},
            "body": raw, "observed_received_at": response.received_at,
            "entity_sha256": _sha(raw), "entity_bytes": len(raw),
            "decoded_entity_bytes": len(raw),
            "content_decoding": response.decoding.get("content_decoding", "IDENTITY_ENTITY_NOT_TLS_WIRE"),
            "decoding": response.decoding,
        })
        return SimpleNamespace(text=raw.decode("utf-8", errors="strict"))


def _invoke(ak, module, requests_port, symbol):
    # Only this module's requests binding changes, inside a dedicated child.
    # Neither requests.get globally nor any SDK algorithm/decoder is replaced.
    with _SDK_LOCK:
        original = module.requests
        module.requests = requests_port
        try:
            result = ak.stock_us_daily(symbol=symbol, adjust="qfq")
            if len(requests_port.used) != 2:
                raise ValueError("SDK_INTERFACE_CHANGED")
            return result
        finally:
            module.requests = original


def _normalize(frame, call):
    if (not isinstance(frame, pd.DataFrame) or not 0 < len(frame) <= 40000
            or frame.columns.has_duplicates or set(frame.columns) != set(_COLUMNS)):
        raise ValueError("AKSHARE_NATIVE_FRAME_INVALID")
    frame = frame.loc[:, _COLUMNS].copy()
    dates = pd.to_datetime(frame["date"], errors="raise")
    if (dates.isna().any() or dates.dt.tz is not None
            or not dates.is_monotonic_increasing or dates.duplicated().any()
            or not dates.eq(dates.dt.normalize()).all()):
        raise ValueError("AKSHARE_NATIVE_DATE_INVALID")
    frame["date"] = dates
    selected = frame.loc[(dates >= pd.Timestamp(call["start"])) & (dates < pd.Timestamp(call["end"]))].copy()
    if not 0 < len(selected) <= call["max_rows"]:
        raise ValueError("AKSHARE_NATIVE_WINDOW_INVALID")
    prices = selected[["open", "high", "low", "close"]].to_numpy(dtype=float)
    volume = selected["volume"].to_numpy(dtype=float)
    if (not np.isfinite(prices).all() or not (prices > 0).all()
            or not np.isfinite(volume).all() or not (volume >= 0).all()
            or not (prices[:, 1] >= prices.max(axis=1)).all()
            or not (prices[:, 2] <= prices.min(axis=1)).all()):
        raise ValueError("AKSHARE_NATIVE_PRICE_INVALID")
    return selected.reset_index(drop=True), {
        "full_history_rows": len(frame), "selected_window_rows": len(selected),
        "full_history_start": dates.iloc[0].date().isoformat(),
        "full_history_end": dates.iloc[-1].date().isoformat(),
    }


def native_qfq(call, budget, pins, *, fixture_exchange=None):
    """Returns (native DataFrame, provenance), not the legacy observation tuple.

    The candidate dispatcher maps this DataFrame using existing SDK snapshots.
    Existing parent distribution, AccessPolicy and job guards remain required.
    """
    from finance_platform.data.contracts import DataError
    from .sina_transport import SinaHTTP
    # Pure existing runtime pin checker has no dependency on the old engine.
    from .sina_runtime import verify_runtime
    try:
        symbol = _inputs(call)
        budget.check()
        runtime = verify_runtime(pins, budget)
        ak, module = _load_sdk()
        requests_port = _FixedRequests(symbol,
            SinaHTTP(call, budget, fixture_exchange=fixture_exchange, kind="prices"),
            SinaHTTP(call, budget, fixture_exchange=fixture_exchange, kind="qfq"), budget)
        frame = _invoke(ak, module, requests_port, symbol)
        budget.check()
        selected, counts = _normalize(frame, call)
        return selected, {
            "source_id": SOURCE_ID, "interface": INTERFACE, "price_policy": PRICE_POLICY,
            "adaptation": ADAPTATION, "provider": "akshare", "version": RELEASE,
            "upstream_version": UPSTREAM_RELEASE,
            "official_wheel_sha256": WHEEL_SHA256, "guarded_module_sha256": GUARDED_MODULE_SHA256,
            "cons_sha256": CONS_SHA256, "runtime": runtime, "pandas_version": pd.__version__,
            "calculation": "UPSTREAM_PANDAS_FLOAT64_ROUND4_V1",
            "fill": "UPSTREAM_FULL_FRAME_FFILL_THEN_BFILL",
            "volume_unit": "UNVERIFIED", "volume_adjustment": "UPSTREAM_UNADJUSTED",
            "requested_window": {key: call[key] for key in ("start", "end")}, **counts,
        }
    except DataError:
        raise
    except Exception:
        # Never expose upstream exception text or an entity in public errors.
        budget.abort("AKSHARE_NATIVE_QFQ_INVALID")

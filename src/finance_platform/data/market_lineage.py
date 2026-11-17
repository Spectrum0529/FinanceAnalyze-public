"""Owner-scoped adapter for exact history and fixed-library price snapshots."""
from datetime import date, timedelta

from finance_platform.core.contracts import fingerprint
from .contracts import DataError
from .market_merge import MAX_MERGE_ROWS, PRICE_FIELDS
from .provider_sdk.history_contracts import NATIVE_PRICE_POLICY, QUANTITY_BASIS, YAHOO_PRICE_POLICY
from .simple_inputs import validate_rows

REF_FIELDS = ("source", "id", "version", "content_hash")


def exclusive(value):
    return (date.fromisoformat(value) + timedelta(days=1)).isoformat()


def inclusive(value):
    return (date.fromisoformat(value) - timedelta(days=1)).isoformat()


def history_store(library):
    from .history_store import HistoryStore
    from .provider_sdk.service import ProviderDataService
    return HistoryStore(ProviderDataService(library.db, library.policy))


def history_metadata(summary):
    provider = summary["provider"]
    expected = {"yfinance": YAHOO_PRICE_POLICY, "akshare": NATIVE_PRICE_POLICY}
    if (provider not in expected or summary["price_policy"] != expected[provider]
            or summary["identity"]["currency"] != "USD"
            or summary["quantity_basis"] != QUANTITY_BASIS or summary["raw_compatible"] is not False):
        raise DataError("MARKET_MERGE_INCOMPATIBLE")
    return {"currency": summary["identity"]["currency"], "timezone": "America/New_York",
            "adjustment": "split_adjusted" if provider == "yfinance" else "qfq",
            "price_unit": "per_adjusted_simulation_unit", "volume_unit": "provider_reported_unverified"}


def project(views, start, end):
    rows = [{"ticker": ticker, "session_date": row["session_date"],
             **{key: row[key] for key in PRICE_FIELDS}}
            for ticker, view in sorted(views.items()) for row in view["rows"]]
    if len(rows) > MAX_MERGE_ROWS:
        raise DataError("MARKET_MERGE_LIMIT")
    return validate_rows(rows, start, end, sorted(views), allow_missing_volume=True)


def native_frames(views, meta, start, end):
    if not views:
        raise DataError("MARKET_MERGE_INCOMPATIBLE")
    provider = next(iter(views.values()))["provider"]
    profile = {"provider": provider, "frequency": "1Day", **meta,
               "price_basis": next(iter(views.values()))["price_policy"], "securities": []}
    references, split_dates = [], []
    action_evidence = provider == "yfinance"
    for ticker, view in sorted(views.items()):
        instrument = view["identity"]
        if history_metadata(view) != meta or instrument["ticker"] != ticker:
            raise DataError("MARKET_MERGE_INCOMPATIBLE")
        profile["securities"].append({"ticker": ticker, "identity": instrument["instrument_id"],
                                      "calendar_id": instrument["calendar_id"], "mic": instrument["mic"]})
        references.append({"ticker": ticker, "history_id": view["history_id"], "version": view["version"],
                           "snapshot_content_hash": view["history_content_hash"]})
        for row in view["rows"]:
            actions = row.get("source_corporate_action_evidence")
            if type(actions) is not dict or "splits" not in actions:
                action_evidence = False
            elif actions["splits"] is not None:
                split_dates.append(row["session_date"])
    return {"profile": profile, "rows": project(views, start, end),
            "vintages": [fingerprint(references)], "native_references": references,
            "action_evidence": action_evidence, "split_dates": split_dates}


def read_native_references(library, owner, references, start, end, *, active=None):
    store = history_store(library)
    views = {}
    for item in references:
        if active is not None:
            active()
        view = store.view(owner, item["history_id"], item["version"], start, exclusive(end), durable_reuse=True)
        if view["ticker"] != item["ticker"] or view["history_content_hash"] != item["snapshot_content_hash"]:
            raise DataError("MARKET_PROVENANCE_INVALID")
        views[item["ticker"]] = view
    return views


def load_history(store, owner, ref, summary):
    if summary["content_hash"] != ref["content_hash"]:
        raise DataError("DATASET_REFERENCE_MISMATCH")
    if summary["row_count"] > MAX_MERGE_ROWS:
        raise DataError("MARKET_MERGE_LIMIT")
    span = summary["requested_range"]
    start, end = span["start"], inclusive(span["end"])
    view = store.view(owner, ref["id"], ref["version"], start, span["end"])
    if view["content_hash"] != ref["content_hash"]:
        raise DataError("DATASET_REFERENCE_MISMATCH")
    value = native_frames({view["ticker"]: view}, history_metadata(view), start, end)
    return {**value, "scope": "public", "kind": "dataset", "start": start, "end": end,
            "connection_id": None, "received_at": view["observed_received_at"],
            "security_metadata": [{"ticker": view["ticker"], "instrument": view["identity"],
                                   "identity_reference": view["identity_evidence_ref"]}]}


def _massive_frame(record):
    meta = record["metadata"]
    if (record["source"]["provider"] != "massive" or meta["adjustment"] != "raw"
            or meta["currency"] != "USD" or meta["timezone"] != "America/New_York"
            or meta["price_unit"] != "per_share" or meta["volume_unit"] != "shares"
            or not record.get("row_storage") and not record.get("market_provenance")):
        raise DataError("MARKET_MERGE_INCOMPATIBLE")
    securities = []
    for item in record.get("security_metadata") or ():
        ticker, mic = item.get("ticker"), item.get("exchange")
        figi = item.get("share_class_figi") or item.get("composite_figi")
        if (not figi or item.get("currency") != meta["currency"] or item.get("provider") != "massive"
                or item.get("provider_ticker") != ticker or mic not in {"XNAS", "XNYS", "ARCX", "XASE"}):
            raise DataError("MARKET_MERGE_INCOMPATIBLE")
        securities.append({"ticker": ticker, "identity": figi, "mic": mic,
                           "calendar_id": "XNAS" if mic == "XNAS" else "XNYS"})
    securities.sort(key=lambda item: item["ticker"])
    if [item["ticker"] for item in securities] != record["universe"]["tickers"]:
        raise DataError("MARKET_MERGE_INCOMPATIBLE")
    return {"profile": {"provider": "massive", "frequency": "1Day", **meta, "price_basis": "raw",
                       "securities": securities}, "rows": record["rows"], "vintages": [record["content_hash"]],
            "native_references": [], "action_evidence": False, "split_dates": []}


def load_library(library, owner, ref, summary):
    if summary["scope"] not in {"public", "synthetic"} or summary["kind"] != "dataset":
        raise DataError("MARKET_MERGE_INCOMPATIBLE")
    if (summary["version"], summary["content_hash"]) != (ref["version"], ref["content_hash"]):
        raise DataError("DATASET_REFERENCE_MISMATCH")
    if summary["quality"]["rows"] > MAX_MERGE_ROWS:
        raise DataError("MARKET_MERGE_LIMIT")
    record = library.get(owner, ref["id"], selectable=True, include_rows=True)
    acquisition = record.get("row_storage", {}).get("acquisition")
    native = record.get("market_provenance", {}).get("native_references")
    values = {key: record[key] for key in ("scope", "connection_id", "start", "end", "received_at", "kind",
                                           "security_metadata")}
    if acquisition or native:
        if native:
            views = read_native_references(library, owner, native, record["start"], record["end"])
        else:
            from .native_library_download import native_source_views
            views = dict(native_source_views(library, owner, record, active=lambda: None))
        frame = native_frames(views, record["metadata"], record["start"], record["end"])
        if frame["rows"] != record["rows"]:
            raise DataError("NATIVE_DATASET_ROW_BINDING_MISMATCH")
        return {**frame, **values}
    return {**_massive_frame(record), **values}


def _verified_derived_views(library, owner, record, rows, *, active=None):
    provenance = record.get("market_provenance", {})
    native = provenance.get("native_references")
    if not native:
        raise DataError("MARKET_PROVENANCE_INVALID")
    full = read_native_references(library, owner, native, record["start"], record["end"], active=active)
    frame = native_frames(full, record["metadata"], record["start"], record["end"])
    profile = frame["profile"]
    expected = {"provider": profile["provider"], "price_policy": profile["price_basis"],
                "quantity_basis": QUANTITY_BASIS, "raw_compatible": False}
    if (profile != provenance["profile"] or frame["native_references"] != native
            or record.get("acquisition") != expected or frame["rows"] != rows
            or fingerprint(frame["rows"]) != provenance["rows_hash"]):
        raise DataError("MARKET_PROVENANCE_INVALID")
    return full


def derived_research_acquisition(library, owner, record, rows, *, context):
    from .native_library_download import acquisition_security
    views = _verified_derived_views(library, owner, record, rows, active=context.require_active)
    return {**record["acquisition"], "securities": [acquisition_security(view) for view in views.values()]}


def read_derived(library, owner, reference, run_id, record, *, start=None, end=None):
    data = library.read_for_backtest(owner, reference, run_id, start=start, end=end)
    _verified_derived_views(library, owner, record, record["rows"])
    provenance = record["market_provenance"]
    selected = read_native_references(library, owner, provenance["native_references"],
                                      data["window"]["start"], data["window"]["end"])
    if project(selected, data["window"]["start"], data["window"]["end"]) != data["rows"]:
        raise DataError("MARKET_PROVENANCE_INVALID")
    return {**data, "history_views": selected, "quantity_basis": QUANTITY_BASIS, "raw_compatible": False,
            "price_policy": provenance["profile"]["price_basis"], "backtest_mode": "ADJUSTED_PRICE_SIMULATION"}

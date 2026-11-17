"""NYSE Closing Prices v2.3 pipe-text adapter: daily prices, never auctions.

Keeps native dated symbol/CUSIP identity pending historical mapping. Unknown
publication/delivery times stay unknown under the S01.03 availability contract.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from predictor.contracts.records import RawRecord, TIME_FIELDS
from predictor.contracts.snapshot import content_hash
from predictor.data.acquisition import replay_response, SOURCE, SCHEMA, _immutable, json_bytes
from predictor.logging import utc_timestamp
from predictor.secrets import reject_credentials

HEADER = ("symbol", "cusip", "open", "high", "low", "last", "net chg", "tot vol", "last trade",
          "closing bid", "closing bid size", "closing ask", "closing ask size", "listing market")
LISTING_CODES = {"A": "NYSE_AMERICAN", "F": "TXSE", "L": "LTSE", "M": "NYSE_TEXAS", "N": "NYSE",
                 "P": "NYSE_ARCA", "Q": "NASDAQ", "V": "IEX", "Z": "BATS"}


class PriceError(ValueError):
    pass


def _parser_hash():
    # This reads trusted implementation source, never a market-data partition.
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _price(value, *, signed=False):
    if not value:
        return None
    base = r"(?:\d+(?:\.\d{1,2})?|\.\d{1,2})"
    pattern = "-?" + base if signed else base
    if not re.fullmatch(pattern, value):
        raise PriceError("Invalid USD price precision or unit")
    return format(Decimal(value), ".2f")


def _size(value):
    if not value:
        return None
    if not re.fullmatch(r"\d+", value):
        raise PriceError("Volume/quote size must be whole shares")
    return int(value)


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise PriceError("Timezone-aware observation timestamp required") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PriceError("Timezone-aware observation timestamp required")
    return parsed.astimezone(timezone.utc)


def _mappings(values, session):
    reject_credentials(values)
    result = {}
    ids = set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {"symbol", "cusip", "listing_market", "security_id", "valid_from", "valid_to", "evidence_reference"}:
            raise PriceError("Explicit dated identity mapping with evidence required")
        if not all(isinstance(item[k], str) and item[k].strip() for k in ("symbol", "cusip", "listing_market", "security_id", "valid_from", "evidence_reference")):
            raise PriceError("Empty identity mapping rejected")
        try:
            first = date.fromisoformat(item["valid_from"])
            last = date.fromisoformat(item["valid_to"]) if item["valid_to"] else None
        except (TypeError, ValueError):
            raise PriceError("Invalid identity validity dates") from None
        if item["valid_from"] != first.isoformat() or (last and (last <= first or item["valid_to"] != last.isoformat())):
            raise PriceError("Invalid identity validity interval")
        if first <= session and (last is None or session < last):
            key = item["symbol"]
            if key in result or item["security_id"] in ids:
                raise PriceError("Ambiguous or reused security identity mapping")
            ids.add(item["security_id"])
            result[key] = item
    return result


def parse_response(raw, manifest, *, mappings=(), processed_at=None):
    if not isinstance(raw, bytes) or not raw:
        raise PriceError("Empty raw price response")
    if manifest.get("source_id") != SOURCE or manifest.get("schema_version") != SCHEMA or manifest.get("transport_exchange") != "XNYS":
        raise PriceError("Unsupported price source/schema/exchange")
    try:
        session = date.fromisoformat(manifest["session"])
        text = raw.decode("ascii")
    except (ValueError, KeyError, UnicodeError):
        raise PriceError("Invalid source session or non-ASCII format") from None
    if manifest["session"] != session.isoformat():
        raise PriceError("Canonical source session required")
    observed = _instant(manifest["response_received_at"])
    processed_at = processed_at or utc_timestamp()
    if _instant(processed_at) < observed:
        raise PriceError("Processing precedes actual response reception")
    from hashlib import sha256
    if sha256(raw).hexdigest() != manifest.get("raw_response_hash"):
        raise PriceError("Raw response hash mismatch")
    declared_scope = manifest.get("scope")
    if declared_scope not in {"synthetic_only", "public_provider_sample"}:
        raise PriceError("Explicit sample scope required")
    identity_map = _mappings(mappings, session)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise PriceError("Empty raw price table")
    header = tuple(v.strip().lower() for v in lines[0].split("|"))
    has_header = header == HEADER
    if not has_header and header[0] in {"symbol", "ticker"}:
        raise PriceError("Price header differs from supported v2.3 layout")
    rows = {}
    symbol_keys = {}
    duplicates = 0
    for line in lines[1:] if has_header else lines:
        values = [v.strip() for v in line.split("|")]
        if len(values) != len(HEADER):
            raise PriceError("Invalid price column count")
        symbol, cusip = values[:2]
        listing = values[13]
        if not symbol or len(symbol) > 18 or not re.fullmatch(r"[A-Z0-9 .\-/]+", symbol):
            raise PriceError("Invalid source symbol")
        if cusip and not re.fullmatch(r"[A-Z0-9*@#]{9}", cusip):
            raise PriceError("Invalid source CUSIP")
        if listing and listing not in LISTING_CODES:
            raise PriceError("Unknown listing exchange code")
        prices = {field: _price(values[i], signed=field == "net_change_usd") for i, field in (
            (2, "open_usd"), (3, "high_usd"), (4, "low_usd"), (5, "last_usd"), (6, "net_change_usd"),
            (9, "closing_bid_usd"), (11, "closing_ask_usd"))}
        volume = _size(values[7])
        if volume is None:
            raise PriceError("Missing source volume is not zero")
        last_trade = None
        if values[8]:
            try:
                last_trade = datetime.strptime(values[8], "%m/%d/%Y").date()
            except ValueError:
                raise PriceError("Invalid last-trade reference date") from None
            if last_trade > session:
                raise PriceError("Last-trade date exceeds file session")
        mapping = identity_map.get(symbol)
        if mapping and (mapping["cusip"] != cusip or mapping["listing_market"] != listing):
            raise PriceError("Source ticker/CUSIP/listing does not match dated identity")
        # Quote OHLC as supplied. Zero-volume high/low/last can refer to an older
        # trading date; do not manufacture a current trade, halt or return.
        row = {"session": session.isoformat(), "symbol": symbol, "cusip": cusip or None,
            "listing_market_code": listing or None, "listing_market": LISTING_CODES.get(listing), "transport_exchange": "XNYS",
            "listing_quality": "SOURCE_CODE_DOCUMENTED" if listing else "MISSING_SOURCE_LISTING_CODE",
            "security_id": mapping["security_id"] if mapping else None,
            "identity_status": "DATED_MAPPING_DECLARED_NOT_CERTIFIED" if mapping else "UNRESOLVED_NATIVE_IDENTITY",
            "identity_evidence": mapping["evidence_reference"] if mapping else None,
            "prices": prices, "price_unit": "USD_per_share", "price_basis": "source_daily_summary_not_auction",
            "volume": volume, "volume_unit": "shares_on_transport_exchange_not_consolidated",
            "volume_source_literal": values[7], "volume_scale_status": "UNVERIFIED_SOURCE_SCALE_NO_DIVISOR_APPLIED",
            "volume_quality": "SUSPECT_MAGNITUDE" if volume > 10000000000 else "UNVERIFIED",
            "volume_usable_for_research": False,
            "closing_bid_size_shares": _size(values[10]), "closing_ask_size_shares": _size(values[12]),
            "last_trade_reference_date": last_trade.isoformat() if last_trade else None,
            "trading_status": "TRADED_ON_SOURCE_EXCHANGE" if volume > 0 else "NO_TRADE_ON_SOURCE_EXCHANGE_REASON_UNKNOWN",
            "ohlc_may_be_stale": volume == 0, "auction_price": None, "auction_status": "REFERENCE_UNAVAILABLE_IN_SOURCE",
            "source_publication_at": None, "historical_first_available_at": None,
            "response_received_at": manifest["response_received_at"], "source_id": SOURCE, "schema_version": SCHEMA,
            "formal_label_eligible": False}
        key = (symbol, cusip, listing)
        if key in rows:
            if rows[key] != row:
                raise PriceError("Conflicting duplicate source price key")
            duplicates += 1
        else:
            if symbol in symbol_keys and symbol_keys[symbol] != key:
                raise PriceError("Ambiguous same-session ticker mapping")
            symbol_keys[symbol] = key
            rows[key] = row
    if not rows:
        raise PriceError("Price table has no observations")
    if set(identity_map) - {key[0] for key in rows}:
        raise PriceError("Requested mapped security absent from source response")
    envelopes = []
    for key, payload in sorted(rows.items()):
        native_id = "native:" + content_hash({"symbol": key[0], "cusip": key[1], "listing": key[2], "session": session.isoformat()})
        quality = {field: "missing" for field in TIME_FIELDS}
        quality.update(event_time="date_only", ingested_at="exact", processed_at="exact", revision_time="not_revised",
                       valid_from="date_only", valid_to="open_end")
        meta = {"record_id": SOURCE + ":" + native_id, "security_id": payload["security_id"] or native_id,
            "source_id": SOURCE, "event_time": session.isoformat(), "published_at": None, "first_available_at": None,
            "ingested_at": manifest["response_received_at"], "processed_at": processed_at, "revision_time": None,
            "valid_from": session.isoformat(), "valid_to": None, "version": 1, "content_hash": content_hash(payload),
            "license_id": "public-provider-sample-evaluation-unverified-broader-rights",
            "availability_basis": "live_observed", "timestamp_quality": quality,
            "availability_evidence": {"publication_reference": None, "vendor_reference": None,
                                      "latency_assumption": None, "official_time_references": {}}}
        RawRecord.from_mapping(meta)
        envelopes.append({"metadata": meta, "payload": payload})
    return {"scope": declared_scope, "uses_market_data": declared_scope == "public_provider_sample",
        "source_id": SOURCE, "schema_version": SCHEMA, "session": session.isoformat(),
        "raw_response_hash": manifest["raw_response_hash"], "row_count": len(envelopes), "duplicate_rows_ignored": duplicates,
        "input_has_header": has_header,
        "formal_label_eligible": False, "data_certification": "UNVERIFIED", "model_evaluation": "NOT_RUN",
        "gaps": ["official_primary_listing_auction", "historical_permanent_identity", "historical_publication_and_revision_history",
                 "corporate_action_and_terminal_recovery", "source_volume_scale", "broader_use_and_retention_license"], "records": envelopes}


def replay_prices(policy, directory, *, mappings=(), processed_at=None):
    raw, manifest = replay_response(policy, directory)
    # Record first successful processing once. Replaying the same parser/mapping
    # reuses the observed time rather than manufacturing a new earlier snapshot.
    directory = policy.authorize(directory)
    binding = {"raw_response_hash": manifest["raw_response_hash"], "schema_version": SCHEMA,
               "mapping_hash": content_hash(list(mappings)), "parser_hash": _parser_hash()}
    stamp_path = directory / "processing.json"
    if stamp_path.exists():
        stamp = json.loads(policy.read_bytes(stamp_path))
        if any(stamp.get(k) != value for k, value in binding.items()) or (processed_at and processed_at != stamp["processed_at"]):
            raise PriceError("Replay processing/mapping binding changed; preserve old capture")
        processed_at = stamp["processed_at"]
    else:
        if not directory.is_relative_to(policy.partitions["development"]):
            raise PriceError("Processing journal must be in development partition")
        processed_at = processed_at or utc_timestamp()
        stamp = {**binding, "processed_at": processed_at}
    result = parse_response(raw, manifest, mappings=mappings, processed_at=processed_at)
    _immutable(policy, stamp_path, json_bytes(stamp))
    return result

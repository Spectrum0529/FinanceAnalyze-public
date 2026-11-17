"""Bounded local-development daily inputs; no SDK runner/policy modification.

Collection is delegated by the orchestrator to the existing data.collect job.
Observations retain their own family and never become legacy raw/PIT snapshots.
"""
from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
import re

from finance_platform.core.contracts import canonical, fingerprint, identifier
from finance_platform.data.contracts import DataError
from finance_platform.data.provider_sdk.contracts import (
    DECLARATION, FAMILY, PROVIDERS, now, source_request_parameters, require_declaration,
)

from finance_platform.data.provider_sdk.history_contracts import HISTORY_PROFILE, HISTORY_ROWS, HISTORY_DAYS, HISTORY_EARLIEST, symbol, sina_symbol, history_window

COMMON_EXAMPLES = (('AAPL', 'Apple'), ('MSFT', 'Microsoft'), ('NVDA', 'NVIDIA'), ('AMD', 'Advanced Micro Devices'), ('GOOG', 'Alphabet'), ('AMZN', 'Amazon'), ('SPY', 'SPDR S&P 500 ETF'), ('QQQ', 'Invesco Nasdaq-100 ETF'), ('VTI', 'Vanguard Total Stock Market ETF'), ('IWM', 'iShares Russell 2000 ETF'), ('DIA', 'SPDR Dow Jones ETF'), ('TLT', 'iShares Treasury ETF'), ('GLD', 'SPDR Gold Shares'), ('BRK-B', 'Berkshire Hathaway Class B'))
IDENTITY_BASIS = "CURRENT_PROVIDER_META_NOT_HISTORICAL_SECURITY_MASTER"
CATALOG_REFERENCE = "https://akshare.akfamily.xyz/data/stock/stock.html"
MAX_SCAN = 1000


def catalog(query=""):
    if type(query) is not str or len(query) > 64 or any(ord(c) < 32 for c in query):
        raise DataError("SECURITY_SEARCH_QUERY_INVALID")
    term = query.strip().casefold()
    items = [{"ticker": ticker, "name": name, "identity_verified": False, "suggestion_only": True,
              "provider_symbols": {"yfinance": ticker, "akshare": ticker if sina_symbol(ticker) else None}}
             for ticker, name in COMMON_EXAMPLES if not term or term in ticker.casefold() or term in name.casefold()]
    if query.strip():
        try:
            typed = symbol(query)
            if typed not in {v["ticker"] for v in items}:
                items.append({"ticker": typed, "name": typed, "identity_verified": False, "suggestion_only": True,
                              "provider_symbols": {"yfinance": typed, "akshare": typed if sina_symbol(typed) else None}})
        except DataError:
            pass
    return {"items": items[:20], "providers": list(PROVIDERS), "provider_labels": {"yfinance": "yfinance 拆股调整价格模拟（不含分红）", "akshare": "AKShare / 新浪 前复权价格路径模拟"},
            "typed_ticker_supported": True, "provider_search_available": True, "start_inclusive": True, "end_exclusive": True,
            "max_calendar_days": HISTORY_DAYS, "max_rows": HISTORY_ROWS, "earliest_start": HISTORY_EARLIEST,
            "identity_basis": IDENTITY_BASIS, "binding_reference": CATALOG_REFERENCE, "formal_research_approved": False}


def _identity(ticker):
    raise DataError("SECURITY_LOOKUP_REQUIRED")


def _connection(provider):
    return {"endpoint": PROVIDERS[provider]["origin"], "key_env": None,
            "secret_env": None, "user_agent_env": None, "feed": None}


def _source_ref(source):
    return {"source_id": source["source_id"], "version": source["version"],
            "content_hash": source["content_hash"]}


def _suitable(source, provider):
    if source.get("provider") != provider:
        return False
    try:
        require_declaration(source)
    except DataError:
        return False
    return (source.get("connection") == _connection(provider)
            and source.get("frequencies") == ["1Day"]
            and "US_EQUITY" in source.get("assets", [])
            and set(("open", "high", "low", "close", "volume"))
            <= set(source.get("fields", [])))


class DailyObservationAdapter:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def catalog(query=""):
        return catalog(query=query)

    def _source(self, owner_id, provider):
        # Bounded pagination; always reload through hash-checking source().
        for offset in range(0, MAX_SCAN, 100):
            page = self.service.list_sources(owner_id, 100, offset)
            for item in page["items"]:
                source = self.service.source(owner_id, item["source_id"])
                if _suitable(source, provider):
                    return source
            if page["total"] <= offset + 100:
                break
        else:
            raise DataError("DAILY_SOURCE_LIMIT")
        source_id = "daily-development-" + ("akshare-sina" if provider == "akshare" else provider)
        try:
            existing = self.service.source(owner_id, source_id)
        except DataError as error:
            if error.code != "RESOURCE_NOT_FOUND":
                raise
        else:
            if _suitable(existing, provider):
                return existing
            # Do not replace an existing unsuitable declaration or legacy source.
            raise DataError("DAILY_SOURCE_CONFLICT")
        declaration = {
            "source_id": source_id, "name": ("AKShare / Sina stock_us_daily unadjusted-only" if provider == "akshare" else provider) + " personal daily development",
            "provider": provider, "capabilities": ["daily_observations"],
            "assets": ["US_EQUITY"], "frequencies": ["1Day"],
            "fields": ["open", "high", "low", "close", "volume"],
            "declared_history": "UNKNOWN", "verified_coverage": [],
            "license": {"license_id": "personal-development-" + provider,
                        "evidence": {"evidence_class": DECLARATION, "affirmed": True,
                                     "statement": "Local personal-development observation acquisition and daily assumption simulation requested by this account; no external rights or historical coverage certification."},
                        "use_allowed": ["development"], "rights_verified": False},
            "connection": _connection(provider),
        }
        # Atomic create-if-absent avoids appending an overriding version if an
        # account changes its declaration between the lookup and registration.
        # This fixed internal declaration satisfies the existing SDK schema;
        # arbitrary caller-supplied sources/URLs never reach this insert.
        require_declaration(declaration)
        key = self.service._source_key(owner_id, source_id)
        digest = fingerprint(declaration)
        with self.service.db.transaction() as con:
            legacy = con.execute("SELECT 1 FROM data_sources WHERE source_id=? AND owner_id=?",
                                 (source_id, owner_id)).fetchone()
            old = con.execute("SELECT owner_id,content_hash FROM data_sources "
                              "WHERE source_id=? ORDER BY version DESC LIMIT 1", (key,)).fetchone()
            if legacy or (old and (old["owner_id"] != owner_id or old["content_hash"] != digest)):
                raise DataError("DAILY_SOURCE_CONFLICT")
            if old is None:
                con.execute("INSERT INTO data_sources VALUES(?,?,?,?,?,?)",
                            (key, 1, owner_id, canonical(declaration), digest, now()))
        source = self.service.source(owner_id, source_id)
        # A concurrent source replacement must not be silently adopted.
        if source["content_hash"] != fingerprint(declaration):
            raise DataError("DAILY_SOURCE_CHANGED")
        return source

    def prepare(self, owner_id, *, ticker, start, end, provider="yfinance"):
        from finance_platform.data.history_gateway import HistoryGateway
        gateway = HistoryGateway(self.service, None)
        ticker = symbol(ticker)
        history_window(start, end)
        security = gateway.store.latest_security(owner_id, ticker, fresh=False)
        if security is None: raise DataError("SECURITY_LOOKUP_REQUIRED")
        source = gateway.source(owner_id, provider)
        dataset = self.service.create_history_dataset(owner_id, source["source_id"], security["security_id"])
        params = {"symbols": [ticker], "provider_symbols": {ticker: ticker}, "start": start, "end": end, "timeframe": "1Day"}
        resolved = self.service.resolve_call(owner_id, dataset["dataset_id"], params)
        result = {"dataset_id": dataset["dataset_id"], "source_ref": _source_ref(source), "dataset_hash": dataset["content_hash"],
                  "parameters": params, "resolved_call": resolved, "data_family": FAMILY, "scope": "DEVELOPMENT", "identity_basis": IDENTITY_BASIS}
        return {**result, "request_hash": fingerprint(result)}

    def gateway(self, runner):
        from finance_platform.data.history_gateway import HistoryGateway
        return HistoryGateway(self.service, runner)

    def read(self, owner_id, result_id, *, expected_hash=None, prepared=None):
        identifier(owner_id)
        identifier(result_id)
        if expected_hash is not None and (type(expected_hash) is not str
                or re.fullmatch("[a-f0-9]{64}", expected_hash) is None):
            raise DataError("OBSERVATION_HASH_MISMATCH")
        first = self.service.observation(owner_id, result_id, 100, 0)
        total = first["total"]
        if type(total) is not int or not 1 <= total <= (HISTORY_ROWS if first.get("request_parameters", {}).get("profile") == HISTORY_PROFILE else 400):
            raise DataError("DAILY_OBSERVATION_INVALID")
        body = {k: deepcopy(v) for k, v in first.items()
                if k not in {"limit", "offset", "total", "content_hash"}}
        expected = first["content_hash"]
        if expected_hash is not None and expected_hash != expected:
            raise DataError("OBSERVATION_HASH_MISMATCH")
        for offset in range(100, total, 100):
            page = self.service.observation(owner_id, result_id, 100, offset)
            if page["content_hash"] != expected or page["total"] != total:
                raise DataError("OBSERVATION_HASH_MISMATCH")
            body["rows"].extend(page["rows"])
        if len(body["rows"]) != total or fingerprint(body) != expected:
            raise DataError("OBSERVATION_HASH_MISMATCH")
        if (body.get("data_family") != FAMILY or body.get("raw_compatible") is not False
                or body.get("price_basis") != "PROVIDER_OBSERVED"
                or body.get("historical_first_available_at") is not None
                or body.get("availability_basis") != "OBSERVED_RECEIPT_ONLY"
                or body.get("row_count") != total):
            raise DataError("DAILY_OBSERVATION_INVALID")
        dataset = self.service.dataset(owner_id, body["dataset_id"])
        source = self.service.source(owner_id, dataset["source_id"], dataset["source_version"])
        require_declaration(source)
        if (dataset.get("scope") != "DEVELOPMENT" or dataset.get("data_family") != FAMILY
                or body["source_ref"] != _source_ref(source)):
            raise DataError("DAILY_OBSERVATION_BINDING_MISMATCH")
        call = body["request_parameters"]
        ticker = symbol(call.get("ticker"))
        provider = body.get("provider")
        if provider not in PROVIDERS:
            raise DataError("DAILY_OBSERVATION_BINDING_MISMATCH")
        params = {"symbols": [ticker], "provider_symbols": {
            ticker: call["provider_symbol"] if provider == "akshare" and source.get("connection", {}).get("endpoint") == "https://63.push2his.eastmoney.com" else ticker},
            "start": call["start"], "end": call["end"], "timeframe": "1Day"}
        if (source_request_parameters(source, dataset, params, acquisition=False) != call
                or body["date_range"] != {k: call[k] for k in ("start", "end")}):
            raise DataError("DAILY_OBSERVATION_BINDING_MISMATCH")
        seen = set()
        for row in body["rows"]:
            day = row.get("session_date")
            try:
                valid_day = type(day) is str and date.fromisoformat(day).isoformat() == day
            except ValueError:
                valid_day = False
            if (not valid_day or not call["start"] <= day < call["end"] or day in seen
                    or row.get("existing_instrument_id") != call["existing_instrument_id"]
                    or row.get("provider_symbol") != call["provider_symbol"]
                    or row.get("price_basis") != "PROVIDER_OBSERVED"
                    or row.get("raw_compatible") is not False
                    or row.get("historical_first_available_at") is not None
                    or row.get("availability_basis") != "OBSERVED_RECEIPT_ONLY"):
                raise DataError("DAILY_OBSERVATION_INVALID")
            seen.add(day)
        if prepared is not None:
            frozen = {k: v for k, v in prepared.items() if k != "request_hash"}
            if (fingerprint(frozen) != prepared.get("request_hash")
                    or body["dataset_id"] != prepared["dataset_id"]
                    or dataset["content_hash"] != prepared["dataset_hash"]
                    or body["source_ref"] != prepared["source_ref"]
                    or body["request_parameters"] != prepared["resolved_call"]
                    or body["date_range"] != {k: prepared["parameters"][k] for k in ("start", "end")}):
                raise DataError("DAILY_OBSERVATION_BINDING_MISMATCH")
        return {**body, "content_hash": expected}

"""Single leased acquisition/cache boundary; original SDKRunner and application DB."""
from datetime import date, timedelta
import hashlib
import time

from finance_platform.core.contracts import fingerprint, identifier
from finance_platform.data.contracts import DataError
from .history_store import HistoryStore
from .provider_sdk.contracts import PROVIDERS, FAMILY, DECLARATION, require_declaration, checked_json
from .provider_sdk.history_contracts import HISTORY_PROFILE, HISTORY_DAYS, HISTORY_ROWS, history_window, symbol, sina_symbol
from .provider_sdk.history_contracts import YAHOO_PRICE_POLICY, SINA_PRICE_POLICY, NATIVE_INTERFACE, NATIVE_SOURCE_ID, NATIVE_PRICE_POLICY
from .provider_sdk.security_identity import lookup_call, search_call, identity_from_entity, search_from_entity
from .provider_sdk.sina_quality import POLICY_ID,POLICY_HASH


class HistoryGateway:
    def __init__(self, service, runner):
        self.service, self.runner = service, runner
        self.store = HistoryStore(service)

    def source(self, owner, provider):
        if provider not in PROVIDERS: raise DataError("PROVIDER_UNSUPPORTED")
        source_id = NATIVE_SOURCE_ID if provider=="akshare" else "history-v2-yfinance"
        declaration = {"source_id": source_id, "name": "AKShare原生前复权（上游四位舍入）" if provider == "akshare" else "yfinance 拆股调整价格模拟（不含分红）",
            "provider": provider, "capabilities": ["daily_observations", HISTORY_PROFILE]+([NATIVE_INTERFACE] if provider=="akshare" else []), "assets": ["US_EQUITY", "US_ETF"], "frequencies": ["1Day"],
            "fields": ["open", "high", "low", "close", "volume"], "declared_history": "UNKNOWN", "verified_coverage": [],
            "license": {"license_id": "personal-history-v2-"+provider, "evidence": {"evidence_class": DECLARATION, "affirmed": True,
                "statement": "User-requested personal-development saved history and adjusted-price path simulation; external rights, historical identity and market coverage remain unverified."},
                "use_allowed": ["development"], "rights_verified": False},
            "connection": {"endpoint": PROVIDERS[provider]["origin"], "key_env": None, "secret_env": None, "user_agent_env": None, "feed": None}}
        try:
            existing = self.service.source(owner, source_id)
        except DataError as e:
            if e.code != "RESOURCE_NOT_FOUND": raise
            self.service.register_source(owner, declaration)
            existing = self.service.source(owner, source_id)
        if existing["content_hash"] != fingerprint(declaration): raise DataError("HISTORY_SOURCE_CONFLICT")
        require_declaration(existing)
        return existing

    @staticmethod
    def _owner(context):
        context.require_active()
        owner = context.run.owner_id
        identifier(owner)
        return owner

    def _run(self, context, call, deadline):
        if self.runner is None: raise DataError("SDK_EXECUTION_NOT_DISPATCHED")
        if context.run.parent_run_id is not None: raise DataError("SDK_RETRY_REQUIRES_NEW_COLLECTION")
        context.require_active()
        if time.monotonic() >= deadline: raise DataError("SDK_JOB_TIMEOUT")
        return self.runner.run(context, context.run.owner_id, call, whole_deadline=deadline)

    def resolve_security(self, context, ticker, deadline):
        owner = self._owner(context)
        cached = self.store.latest_security(owner, ticker)
        if cached is not None: return cached
        call = lookup_call(ticker)
        manifest, artifacts, _ = self._run(context, call, deadline)
        rows = checked_json(artifacts["observations.json"])
        entities = [e for e in manifest["entities"] if e["role"] == "metadata_chart"]
        if len(entities) != 1 or entities[0].get("origin") not in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"} or entities[0].get("path") != "/v8/finance/chart/" + ticker:
            raise DataError("SECURITY_IDENTITY_UNAVAILABLE")
        entity = {**entities[0], "body": artifacts[entities[0]["artifact"]]}
        value = identity_from_entity(call, entity)
        if rows != [value] or manifest["sdk_ref"].get("operation") != "identity": raise DataError("SECURITY_IDENTITY_BINDING_MISMATCH")
        context.require_active()
        source = self.source(owner, "yfinance")
        ref = self.service.archive(owner, source, entity["body"], parameters={"operation": "identity", "ticker": ticker},
                                   received_at=entity["observed_received_at"], scope="DEVELOPMENT", schema="provider-security-identity-1")
        return self.store.save_security(context, value, evidence={"archive": ref, "sdk_ref": manifest["sdk_ref"], "source_ref": {k: source[k] for k in ("source_id", "version", "content_hash")}})

    def search(self, context, query):
        self._owner(context)
        call = search_call(query)
        manifest, artifacts, _ = self._run(context, call, time.monotonic()+240)
        entities = [e for e in manifest["entities"] if e["role"] == "security_search"]
        if len(entities) != 1 or entities[0].get("origin") != "query2.finance.yahoo.com" or entities[0].get("path") != "/v1/finance/search": raise DataError("SECURITY_SEARCH_RESPONSE_INVALID")
        rows = search_from_entity({**entities[0], "body": artifacts[entities[0]["artifact"]]})
        if rows != checked_json(artifacts["observations.json"]): raise DataError("SECURITY_SEARCH_RESPONSE_INVALID")
        return {"items": rows, "identity_verified": False, "identity_lookup_required": True,
                "evidence_sha256": hashlib.sha256(artifacts[entities[0]["artifact"]]).hexdigest()}

    def obtain(self, context, request, *, collection_unit=None, whole_deadline=None):
        owner = self._owner(context)
        value = request.payload() if callable(getattr(request, "payload", None)) else dict(request)
        ticker, provider = symbol(value.get("ticker")), value.get("provider", "yfinance")
        if provider not in PROVIDERS: raise DataError("PROVIDER_UNSUPPORTED")
        try:
            start = value["start_date"]
            last = date.fromisoformat(value["end_date"])
            if last.isoformat() != value["end_date"]: raise ValueError
            end = (last+timedelta(days=1)).isoformat()
        except (KeyError, ValueError, TypeError): raise DataError("SDK_DATE_RANGE_VIOLATION") from None
        history_window(start, end)
        policy = value.get("history_policy", "reuse_or_download")
        if policy not in {"reuse_or_download", "refresh"}: raise DataError("HISTORY_POLICY_INVALID")
        explicit, version = value.get("history_id"), value.get("history_version")
        if explicit is not None:
            if policy == "refresh" or type(version) is not int or version < 1: raise DataError("HISTORY_EXPLICIT_VERSION_REQUIRED")
            ref = self.store.freeze_reference(owner, explicit, version)
            summary = self.store.summary(owner, explicit, version)
            if summary["ticker"] != ticker or summary["provider"] != provider: raise DataError("HISTORY_REQUEST_BINDING_MISMATCH")
            action = "EXPLICIT_FROZEN_VERSION"
        else:
            if version is not None: raise DataError("HISTORY_EXPLICIT_VERSION_REQUIRED")
            ref = None if policy == "refresh" else self.store.compatible(owner, ticker, provider, start, end)
            action = "REUSED_FROZEN_VINTAGE" if ref else "DOWNLOADED_NEW_VINTAGE"
        if ref is None:
            deadline = time.monotonic()+240
            if whole_deadline is not None:deadline=min(deadline,whole_deadline)
            security = self.resolve_security(context, ticker, deadline)
            if provider == "akshare" and (not sina_symbol(ticker) or security["provider_symbols"].get("akshare") != ticker): raise DataError("SINA_SYMBOL_MAPPING_UNVERIFIED")
            if provider == "akshare" and ticker in {"AI", "CIEN"}: raise DataError("SINA_KNOWN_FACTOR_QUALITY_UNSUPPORTED")
            source = self.source(owner, provider)
            dataset = self.service.create_history_dataset(owner, source["source_id"], security["security_id"])
            params = {"symbols": [ticker], "provider_symbols": {ticker: ticker}, "start": start, "end": end, "timeframe": "1Day"}
            call = self.service.resolve_call(owner, dataset["dataset_id"], params)
            collection = "history-"+context.run.run_id
            if collection_unit is not None:
                identifier(collection_unit)
                collection += "-"+collection_unit
            binding = fingerprint({"dataset_id": dataset["dataset_id"], "source_hash": source["content_hash"], "call": call, "family": FAMILY})
            observation = self.service.existing_unit(owner, dataset["dataset_id"], collection, binding)
            if observation is None:
                manifest, artifacts, execution = self._run(context, call, deadline)
                context.require_active()
                observation = self.service.persist_capture(context, owner, dataset["dataset_id"], collection, call, manifest, artifacts, execution)
            series_key = fingerprint({"provider": provider, "source_ref": {k: source[k] for k in ("source_id", "version", "content_hash")},
                "security_semantics": security["semantic_hash"], "profile": HISTORY_PROFILE, "frequency": "1Day", "timezone": "America/New_York",
                "price_policy": YAHOO_PRICE_POLICY if provider == "yfinance" else NATIVE_PRICE_POLICY})
            ref = self.store.save(context, observation, security, series_key=series_key)
        self.store.pin(owner, ref["history_id"], ref["version"], context.run.run_id, kind="run", context=context)
        view = self.store.view(owner, ref["history_id"], ref["version"], start, end, pinned={"kind": "run", "reference_id": context.run.run_id})
        summary = self.store.summary(owner, ref["history_id"], ref["version"], include_deleted=True)
        view["history_cache"].update(action=action, freshness=summary["freshness"], automatically_refreshed=False)
        return view

    def freeze_reference(self, owner_id, history_id, version=None):
        return self.store.freeze_reference(owner_id, history_id, version)

    def pin(self, owner_id, history_id, version, reference_id, *, kind="run", connection=None, context=None):
        return self.store.pin(owner_id, history_id, version, reference_id, kind=kind, connection=connection, context=context)

    def read_pinned(self, owner_id, history_id, version, reference_id, *, kind="result", start=None, end=None):
        summary = self.store.summary(owner_id, history_id, version, include_deleted=True)
        return self.store.view(owner_id, history_id, version, start or summary["requested_range"]["start"], end or summary["requested_range"]["end"], pinned={"kind": kind, "reference_id": reference_id})

    def list(self, *args, **kwargs): return self.store.list(*args, **kwargs)
    def summary(self, *args, **kwargs): return self.store.summary(*args, **kwargs)
    def lifecycle(self, *args, **kwargs): return self.store.lifecycle(*args, **kwargs)

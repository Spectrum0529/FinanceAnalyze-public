"""New daily adapters on the existing admitted SDK runner and HTTPS budget."""
from datetime import datetime, timezone
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlsplit

from finance_platform.data.contracts import DataError
from finance_platform.data.provider_sdk.contracts import LIMITS, checked_json
from finance_platform.data.provider_sdk.network_gate import BoundedSafeHTTP

from .goal_data import Unit
from .provider_adapters import AK_URL, NASDAQ_URL, collect_akshare, collect_nasdaq, collect_tushare
from .tushare_reference import FIELDS, collect_reference, calendar_records, identity_record
from .akshare_reference import collect_roster, validate_xlsx, SSE_REFERER, roster_category, listed_identity

SDK = {
    "akshare_cn": ("akshare", "1.19.3+guard1", collect_akshare),
    "openbb_nasdaq": ("openbb-nasdaq", "2.0.0", collect_nasdaq),
    "tushare": ("tushare", "1.4.29", collect_tushare),
}
TUSHARE_TLS = "https://api.tushare.pro/"


def check_tushare_body(raw, budget, token):
    if token and token.encode("ascii") in raw:
        budget.abort("SDK_CREDENTIAL_IN_FINANCIAL_BODY")
    body = checked_json(raw)
    if token:
        pending = [body]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and token in value:
                budget.abort("SDK_CREDENTIAL_IN_FINANCIAL_BODY")
            if isinstance(value, dict):
                pending.extend(value)
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    return body


def verify(name, version, pins):
    pin = pins.get(name, {})
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        raise DataError("GOAL_DATA_RUNTIME_UNAVAILABLE") from None
    if dist.version != version or pin.get("version") != version or not pin.get("modules"):
        raise DataError("SDK_INTERFACE_CHANGED")
    for relative, digest in pin["modules"].items():
        raw = Path(dist.locate_file(relative)).read_bytes()
        if sha256(raw.replace(b"\r\n", b"\n")).hexdigest() != digest:
            raise DataError("SDK_INTERFACE_CHANGED")
    return dict(name=name, version=version, artifact_sha256=pin["artifact_sha256"])


def collect_child(call, budget, pins, *, fixture_exchange=None, token=None):
    provider = call["provider"]
    operation = call.get("operation", "daily")
    expected = {"provider", "ticker", "start", "end"} | ({"operation"} if operation != "daily" else set())
    reference_operation = (provider == "tushare" and operation in FIELDS or
                           provider == "akshare_cn" and operation == "exchange_list")
    if set(call) != expected or provider not in SDK or operation != "daily" and not reference_operation:
        raise DataError("SDK_INPUT_INVALID")
    if provider == "tushare" and fixture_exchange is None and not token:
        raise DataError("GOAL_DATA_CREDENTIAL_REQUIRED")
    name, version, adapter = SDK[provider]
    refs = [verify(name, version, pins)]
    if provider == "openbb_nasdaq":
        refs.append(verify("openbb-core", "2.0.1", pins))
    unit = Unit(call["ticker"], call["start"], call["end"])
    if operation == "exchange_list":
        def read_roster(url, params, shanghai):
            if fixture_exchange is not None:
                budget.begin()
                raw = fixture_exchange(provider, unit)
                budget.consume(raw, 0)
            else:
                parsed = urlsplit(url)
                port = BoundedSafeHTTP(budget)
                port.allowed_hosts, port.path_prefixes = (parsed.hostname,), (parsed.path,)
                response = port.fetch(url, params, role="goal_akshare_exchange_list",
                                      referer=SSE_REFERER if shanghai else None)
                if response.status in {401, 402, 403}:
                    budget.abort("GOAL_DATA_ACCESS_REJECTED")
                if response.status == 429:
                    budget.abort("GOAL_DATA_RATE_LIMITED")
                if response.status != 200:
                    budget.abort("GOAL_DATA_PROVIDER_REJECTED")
                raw = response.body
            body = checked_json(raw, max_nodes=400000) if shanghai else validate_xlsx(raw)
            budget.entities.append(dict(role="goal_daily", observed_received_at=datetime.now(timezone.utc).isoformat(),
                                        body=raw))
            return body

        rows = collect_roster(unit, read_roster)
        budget.check()
        if budget.requests != 1 or len(budget.entities) != 1:
            raise DataError("GOAL_DATA_SDK_INTERFACE_CHANGED")
        return {"packages": refs, "provider": provider}, {"row_count":len(rows)}, rows
    captured = False

    def transport(_provider, _unit, shape):
        nonlocal captured
        budget.check()
        if captured or _provider != provider or _unit != unit:
            budget.abort("GOAL_DATA_SDK_INTERFACE_CHANGED")
        captured = True
        if fixture_exchange is not None:
            budget.begin()
            raw = fixture_exchange(provider, unit)
            budget.consume(raw, 0)
        else:
            if provider == "tushare":
                url, params = TUSHARE_TLS, {}
            elif provider == "akshare_cn":
                url, params = AK_URL, shape
            else:
                parsed = urlsplit(NASDAQ_URL + "/" + shape)
                url = parsed._replace(query="").geturl()
                params = dict(parse_qsl(parsed.query, strict_parsing=True))
            path = urlsplit(url)
            port = BoundedSafeHTTP(budget)
            port.allowed_hosts, port.path_prefixes = (path.hostname,), (path.path,)
            if provider == "tushare":
                payload = {**shape, "params": {key: value for key, value in shape["params"].items()
                                                if key != "ts_type_name"}}
                response = port.fetch(url, params, json_body=json.dumps(payload, separators=(",", ":")).encode(),
                                      role="goal_tushare_daily")
            else:
                response = port.fetch(url, params, role="goal_" + provider + "_daily")
            if response.status in {401, 402, 403}:
                budget.abort("GOAL_DATA_ACCESS_REJECTED")
            if response.status == 429:
                budget.abort("GOAL_DATA_RATE_LIMITED")
            if response.status != 200:
                budget.abort("GOAL_DATA_PROVIDER_REJECTED")
            raw = response.body
        body = check_tushare_body(raw, budget, token) if provider == "tushare" else checked_json(raw)
        if provider == "tushare" and token and type(body) is dict and body.get("code") != 0:
            # Remote error text is unnecessary for the SDK's typed failure.
            return body
        budget.entities.append(dict(role="goal_daily", observed_received_at=datetime.now(timezone.utc).isoformat(),
                                    body=raw))
        return body

    options = {"token": token or "synthetic-offline-only"} if provider == "tushare" else {}
    rows = (adapter(unit, transport, **options) if operation == "daily" else
            collect_reference(operation, unit, transport, options["token"]))
    budget.check()
    if not captured:
        raise DataError("GOAL_DATA_SDK_INTERFACE_CHANGED")
    return {"packages": refs, "provider": provider}, {"row_count": len(rows)}, rows


def runner_collection(runners, *, credential=None, us_history=None, massive=None, cboe=None):
    """Accept only server-registered runners with exact bindings."""
    def collect(owner, provider, connection_id, unit, context, deadline, *, operation="daily", asset=None):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise DataError("GOAL_DATA_DEADLINE_EXCEEDED")
        whole_deadline = time.monotonic() + remaining
        if provider == "openbb_cboe" and cboe is not None:
            return cboe(owner, unit, context, whole_deadline, asset)
        if provider in {"yfinance", "akshare"} and us_history is not None:
            from finance_platform.core.contracts import canonical, fingerprint
            security = us_history.resolve_security(context, unit.symbol, whole_deadline)
            if security["instrument"]["asset_class"] != asset or security["instrument"]["currency"] != "USD":
                raise DataError("SECURITY_MARKET_OR_IDENTITY_UNSUPPORTED")
            view = us_history.obtain(context, dict(ticker=unit.symbol, provider=provider, start_date=unit.start,
                                                   end_date=unit.end, history_policy="refresh"),
                                     collection_unit=fingerprint(vars(unit))[:24], whole_deadline=whole_deadline)
            provenance = {key: view[key] for key in ("observation_ref", "source_ref", "identity_evidence_ref")}
            rows = [{k: row[k] for k in ("session_date", "open", "high", "low", "close", "volume")}
                    | {"ticker": unit.symbol} for row in view["rows"]]
            return dict(rows=rows, source_bytes=canonical(provenance).encode(), sdk={"provider": provider},
                        received_at=view["observed_received_at"], scope="public",
                        source_reference=provenance)
        runner = runners.get(provider)
        if runner is None:
            raise DataError("GOAL_DATA_RUNTIME_UNAVAILABLE")
        kwargs = {}
        if provider == "tushare" and runner.fixture is None:
            if credential is None:
                raise DataError("GOAL_DATA_CREDENTIAL_REQUIRED")
            kwargs["goal_token"] = credential(owner, connection_id, TUSHARE_TLS)
        call = dict(provider=provider, ticker=unit.symbol, start=unit.start, end=unit.end)
        if operation != "daily":
            call["operation"] = operation
        manifest, artifacts, execution = runner.run(
            context, owner, call,
            whole_deadline=whole_deadline, **kwargs,
        )
        entities = manifest["entities"]
        if (len(entities) != 1 or entities[0]["role"] != "goal_daily"
                or manifest["sdk_ref"]["provider"] != provider
                or manifest["stats"]["physical_attempts"] != 1
                or execution["admitted_after_job_assignment"] is not True or execution["child_reaped"] is not True
                or execution["network_class"] not in {"OFFLINE_SYNTHETIC", "BOUNDED_PUBLIC_SDK"}):
            raise DataError("GOAL_DATA_CAPTURE_INVALID")
        return dict(source_bytes=artifacts[entities[0]["artifact"]], sdk=manifest["sdk_ref"],
                    received_at=entities[0]["observed_received_at"],
                    rows=checked_json(artifacts["observations.json"]),
                    scope="synthetic" if execution["network_class"] == "OFFLINE_SYNTHETIC" else "public")

    existing = {"yfinance", "akshare"} if us_history is not None else set()
    other = ({"massive"} if massive is not None else set()) | ({"openbb_cboe"} if cboe is not None else set())
    collect.supports = (set(runners) | existing | other).__contains__
    collect.requests_per_unit = lambda provider: 2 * LIMITS["requests"] if provider in existing | {"openbb_cboe"} else 1
    def scope(provider):
        if provider in existing | other:
            return "public"
        runner = runners.get(provider)
        return None if runner is None else "synthetic" if runner.fixture is not None else "public"
    collect.scope = scope
    collect.reference_available = "tushare" in runners
    collect.akshare_reference_available = "akshare_cn" in runners
    collect.massive = massive
    return collect


def tushare_credential(store, library):
    def resolve(owner, connection_id, endpoint):
        connection = library.connection(owner, connection_id)
        reference = connection.get("credential_ref")
        if connection.get("provider") != "tushare" or not reference:
            raise DataError("GOAL_DATA_CREDENTIAL_REQUIRED")
        target = store.freeze_target(owner, "tushare", None)
        if endpoint != TUSHARE_TLS or target.base_url != endpoint:
            raise DataError("GOAL_DATA_CREDENTIAL_TARGET_MISMATCH")
        return store.resolve_for_provider(owner, reference, target=target)
    return resolve


def install_sources(runtime, library, *, runners, credential_store, fence, sessions=None, identities=None,
                    us_history=None, cboe=None):
    from functools import partial
    from .goal_data_service import install, server_sessions
    from .cn_identity import reviewed_identity
    from .goal_massive import batches

    # The SDKRunner binding and fixture identity come from trusted composition.
    port = getattr(runtime, "goal_massive_client", None)
    massive = (partial(batches, library, runtime.massive_control, *port) if port is not None else None)
    if cboe is None and getattr(runtime, "openbb_cboe_enabled", False):
        from finance_platform.data.openbb_source import capability
        if capability(enabled=True, runtime_binding=runtime.openbb_runtime_binding)["available"]:
            cboe = partial(collect_cboe, runtime, library)
    return install(runtime, library, collect=runner_collection(
        runners, credential=tushare_credential(credential_store, library), us_history=us_history,
        massive=massive, cboe=cboe),
        fence=fence, sessions=sessions or server_sessions, identities=identities or reviewed_identity)


def collect_cboe(runtime, library, owner, unit, context, deadline, asset):
    from finance_platform.core.contracts import canonical
    from finance_platform.data.openbb_source import gateway_and_runner, validate_window_symbol
    from finance_platform.data.openbb_library_download import capture_unit
    from finance_platform.data.simple_chunks import DatasetChunks

    validate_window_symbol(unit.symbol, unit.start, unit.end)
    gateway, runner = gateway_and_runner(runtime)
    identity = gateway.resolve_security(context, unit.symbol, deadline)
    if identity["instrument"]["asset_class"] != asset:
        raise DataError("SECURITY_MARKET_OR_IDENTITY_UNSUPPORTED")
    chunks = DatasetChunks(library.policy, owner, binding=context.storage_binding)
    rows, _, evidence, _ = capture_unit(context, owner, vars(unit), unit.symbol, gateway, runner, chunks,
                                        deadline=deadline, collection_unit=unit.start+"-"+unit.end)
    evidence["location_id"] = context.storage_binding["locations"]["market_data"]["location_id"]
    return dict(rows=rows, scope="public", source_bytes=canonical(evidence).encode(), sdk=evidence["sdk_ref"],
                received_at=evidence["observed_received_at"])


def cn_references(collect, owner, connection_id, requirement, context, deadline, persist):
    """Resolve calendar per exchange and identity per ticker from the pinned SDK."""
    from .goal_data import day

    observations, identities, references, sessions, calendars = {}, {}, [], {}, {}
    for ticker in requirement["symbols"]:
        context.require_active()
        unit = Unit(ticker, requirement["start"], requirement["end"])
        exchange = ticker.split(".")[-1]
        if exchange not in observations:
            capture = collect(owner, "tushare", connection_id, unit, context, deadline, operation="trade_cal")
            mic, days = calendar_records(capture["rows"], unit)
            reference = persist(capture)
            observations[exchange] = days
            calendars[mic] = f"tushare:trade_cal:{reference['sha256']}:{mic}"
            references.append(reference)
        sessions[ticker] = observations[exchange]
        capture = collect(owner, "tushare", connection_id, unit, context, deadline, operation="stock_basic")
        reference = persist(capture)
        references.append(reference)
        observed_day = datetime.fromisoformat(capture["received_at"]).date().isoformat()
        identities[ticker] = identity_record(capture["rows"], unit, received_date=observed_day, evidence=reference)
    return {"sessions": sessions, "source": calendars}, identities, references


def akshare_references(collect, owner, connection_id, requirement, context, deadline, persist):
    """One official roster per requested exchange/board, reused across tickers."""
    rosters, identities, references = {}, {}, []
    for ticker in requirement["symbols"]:
        context.require_active()
        unit = Unit(ticker, requirement["start"], requirement["end"])
        category = roster_category(ticker)
        if category not in rosters:
            capture = collect(owner, "akshare_cn", connection_id, unit, context, deadline, operation="exchange_list")
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if ticker.endswith(".SZ") else "application/json"
            reference = persist(capture, media)
            rosters[category] = (capture, reference)
            references.append(reference)
        capture, reference = rosters[category]
        observed = datetime.fromisoformat(capture["received_at"]).date().isoformat()
        identities[ticker] = listed_identity(capture["rows"], unit, observed, reference)
    return identities, references


def verify_reference_snapshot(library, owner, requirement, provenance):
    """Reproject the original owner-scoped reference objects before zero-request reuse."""
    snapshot = provenance["reference_snapshot"]
    captures = provenance["reference_captures"]
    storage = library.market.storage
    fields = ("schema", "category", "location_id", "owner_id", "key", "sha256", "bytes", "media_type")
    source, sessions, identities, originals, exchange_days = {}, {}, {}, {}, {}

    def original(ref, expected_provider):
        if ref not in captures or ref["sdk"]["provider"] != expected_provider:
            raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
        key = (ref["location_id"], ref["key"])
        if key not in originals:
            originals[key] = storage.read(owner, {field: ref[field] for field in fields}, category="market_data",
                                          maximum_bytes=LIMITS["response_bytes"])
        return originals[key]

    def reference_rows(operation, raw):
        body = checked_json(raw)
        values = body["data"]
        fields = FIELDS[operation]
        if body["code"] != 0 or values["fields"] != list(fields):
            raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
        return [dict(zip(fields, row, strict=True)) for row in values["items"]]

    if snapshot["kind"] not in {"TUSHARE", "AKSHARE"}:
        raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
    for ticker in requirement["symbols"]:
        unit = Unit(ticker, requirement["start"], requirement["end"])
        mic = "XSHG" if ticker.endswith(".SH") else "XSHE"
        identity_ref = provenance["identity_evidence"][ticker]["evidence"]["capture"]
        observed = datetime.fromisoformat(identity_ref["observed_received_at"]).date().isoformat()
        if snapshot["kind"] == "AKSHARE":
            raw = original(identity_ref, "akshare_cn")
            rows = collect_roster(unit, lambda _url, _params, shanghai:
                                  checked_json(raw, max_nodes=400000) if shanghai else validate_xlsx(raw))
            identities[ticker] = listed_identity(rows, unit, observed, identity_ref)
            sessions[ticker] = snapshot["sessions"][ticker]
            source[mic] = provenance["calendar"][mic]
            continue
        if mic not in exchange_days:
            calendar_tag = provenance["calendar"][mic]
            matches = [ref for ref in captures if calendar_tag == f"tushare:trade_cal:{ref['sha256']}:{mic}"]
            if len(matches) != 1:
                raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
            calendar_raw = original(matches[0], "tushare")
            verified_mic, exchange_days[mic] = calendar_records(reference_rows("trade_cal", calendar_raw), unit)
            if verified_mic != mic:
                raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
            source[mic] = calendar_tag
        sessions[ticker] = exchange_days[mic]
        rows = reference_rows("stock_basic", original(identity_ref, "tushare"))
        identities[ticker] = identity_record(rows, unit, received_date=observed, evidence=identity_ref)
    if set(originals) != {(ref["location_id"], ref["key"]) for ref in captures}:
        raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
    return {"sessions": sessions, "source": source}, identities

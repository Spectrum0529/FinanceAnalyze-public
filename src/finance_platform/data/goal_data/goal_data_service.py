"""Use Core Jobs and the dataset library for bounded goal data preparation."""
from datetime import datetime, timezone
from hashlib import sha256
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from finance_platform.core.contracts import fingerprint, identifier
from finance_platform.data.contracts import DataError
from finance_platform.data.provider_sdk.contracts import LIMITS
from .goal_data import CAPABILITIES, REF_FIELDS, Requirement, missing_units, merge_rows
from .goal_job_receipts import GoalJobReceipts

HANDLER = "data.goal-prepare"
MAX_CATALOG = 100


@dataclass
class GuardedContext:
    base: object
    guard: object
    receipts: GoalJobReceipts

    def __getattr__(self, name):
        return getattr(self.base, name)

    def require_active(self):
        self.guard()

    def assert_fenced(self, connection):
        self.guard(connection)

    def begin_provider_request(self):
        return self.receipts.begin(self.job_id, self.guard)

    def finish_provider_request(self, attempt, state):
        self.receipts.finish(self.job_id, attempt, state)


def server_sessions(need, *, today):
    """Use versioned exchange calendars and reject beyond supported bounds."""
    import exchange_calendars
    from .goal_data import day, symbol

    market = need.get("market")
    if market not in {"US", "CN"}:
        raise DataError("GOAL_DATA_REQUIREMENT_UNSUPPORTED")
    start, end = day(need.get("start")), day(need.get("end"))
    if not start <= end < today or (end - start).days >= 366:
        raise DataError("GOAL_DATA_DATE_INVALID")
    tickers = need.get("symbols")
    if (type(tickers) is not list or not 1 <= len(tickers) <= 32
            or not all(symbol(ticker, need["market"]) for ticker in tickers)):
        raise DataError("GOAL_DATA_SYMBOL_INVALID")
    by_exchange, sources = {}, {}
    for ticker in tickers:
        name = "XNAS" if market == "US" else "XSHG" if ticker.endswith(".SH") else "XSHE"
        if name not in by_exchange:
            if name == "XSHE":
                from .cn_calendar import shenzhen_sessions
                by_exchange[name], sources[name] = shenzhen_sessions(start, end)
            else:
                calendar = exchange_calendars.get_calendar(name)
                try:
                    by_exchange[name] = [stamp.date().isoformat() for stamp in calendar.sessions_in_range(start, end)]
                except ValueError:
                    raise DataError("GOAL_DATA_CALENDAR_UNAVAILABLE") from None
                sources[name] = f"exchange_calendars:{exchange_calendars.__version__}:{name}"
    return dict(sessions={ticker: by_exchange["XNAS" if market == "US" else
                                               "XSHG" if ticker.endswith(".SH") else "XSHE"]
                          for ticker in tickers}, source=sources)


def fixed_ref(record):
    return {key: record[key] for key in REF_FIELDS}


def _compatible(record, req, provider, connection_id, cap):
    meta = record.get("metadata", {})
    provenance = record.get("goal_data_provenance")
    if (record.get("kind") != "dataset" or record.get("scope") not in {"synthetic", "public"}
            or record.get("source", {}).get("provider") != provider or record.get("connection_id") != connection_id
            or meta != metadata_for(req.adjustment, cap)):
        return False
    if provenance is not None:
        return (provenance["market"], provenance["asset"], provenance["kind"], provenance["frequency"]) == (
            req.market, req.asset, req.kind, req.frequency)
    if req.market != "US":
        return False
    # Earlier provider records require an explicit asset identity for every requested symbol they contain.
    identities = record.get("security_metadata") or []
    if type(identities) is not list:
        return False
    observed = {item.get("ticker"): item.get("asset_class") or item.get("instrument", {}).get("asset_class")
                for item in identities}
    overlap = set(record["universe"]["tickers"]) & set(req.symbols)
    return bool(overlap) and all(observed.get(ticker) == req.asset for ticker in overlap)


def metadata_for(adjustment, cap):
    units = {
        "provider_observed_unverified": ("per_provider_observed_unit", "provider_reported_unverified"),
        "qfq": ("per_adjusted_simulation_unit", "provider_reported_unverified"),
    }
    if cap.provider == "yfinance":
        price, volume = "per_adjusted_simulation_unit", "provider_reported_unverified"
    else:
        price, volume = units.get(adjustment, ("per_share", "shares"))
    return dict(currency=cap.currency, timezone=cap.timezone, adjustment=adjustment,
                price_unit=price, volume_unit=volume)


def _slice(record, req):
    return [row for row in record["rows"] if (row["ticker"], row["session_date"]) in req.keys]


def _plan(library, owner, req, provider, connection_id, cap, scope, *, reference_connection=None):
    candidates = library.list_datasets(owner, limit=MAX_CATALOG)["items"]
    best = None, [], missing_units(req, ())
    for summary in candidates:
        if summary.get("scope") != scope or not _compatible(summary, req, provider, connection_id, cap):
            continue
        provenance = summary.get("goal_data_provenance", {})
        if "reference_captures" in provenance and (reference_connection is None or
                provenance.get("reference_snapshot", {}).get("connection_id") != reference_connection):
            continue
        record = library.get(owner, summary["dataset_id"], include_rows=True, selectable=True)
        if fixed_ref(record) != fixed_ref(summary) or not _compatible(record, req, provider, connection_id, cap):
            raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
        rows = _slice(record, req)
        units = missing_units(req, rows)
        if (not units and set(record["universe"]["tickers"]) == set(req.symbols)
                and record["start"] == req.start and record["end"] == req.end and len(record["rows"]) == len(rows)):
            return record, rows, (), True
        if len(rows) > len(best[1]):
            best = record, rows, units
    return (*best, False)


def _reference_snapshot(library, owner, need, connection_id, provider, scope, reference_connection, today):
    from .goal_sdk import verify_reference_snapshot

    for summary in library.list_datasets(owner, limit=MAX_CATALOG)["items"]:
        provenance = summary.get("goal_data_provenance", {})
        snapshot = provenance.get("reference_snapshot")
        if (snapshot is None or snapshot["connection_id"] != reference_connection
                or summary["scope"] != scope or summary["start"] != need.get("start")
                or summary["end"] != need.get("end")
                or sorted(summary["universe"]["tickers"]) != sorted(need.get("symbols", []))):
            continue
        req = Requirement.parse({**need, "required_sessions": snapshot["sessions"]}, today=today)
        if not _compatible(summary, req, provider, connection_id, req.require_capability(provider)):
            continue
        record = library.get(owner, summary["dataset_id"], include_rows=True, selectable=True)
        if fixed_ref(record) != fixed_ref(summary):
            raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
        calendar, securities = verify_reference_snapshot(library, owner, need, provenance)
        market, identities = _research_market(owner, req, provider, calendar, lambda *args: securities[args[2]])
        digest = fingerprint(calendar["sessions"])
        if (market != provenance["research_market"] or identities != provenance["identity_evidence"]
                or digest != provenance["required_sessions_sha256"] or calendar["sessions"] != snapshot["sessions"]
                or calendar["source"] != provenance["calendar"]):
            raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
        merge_rows(req, record["rows"])
        return record
    return None


def install(runtime, library, *, collect, fence, sessions=server_sessions, identities=None, clock=None):
    """Bind trusted runner/parent-fence ports supplied by the integrator."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    receipts = GoalJobReceipts(library.db)

    def market_today(need):
        zone = "America/New_York" if need.get("market") == "US" else "Asia/Shanghai"
        return clock().astimezone(ZoneInfo(zone)).date()

    def parse(value):
        return Requirement.parse(value, today=market_today(value))

    def handle(context, parameters):
        owner = context.run.owner_id
        if getattr(context.run, "parent_run_id", None) is not None:
            raise DataError("GOAL_DATA_RETRY_REQUIRES_RECONCILIATION")
        mode = context.internal_origin()
        if (mode == "goal") != ("task_id" in parameters):
            raise DataError("GOAL_DATA_JOB_BINDING_REQUIRED")
        validate(parameters)
        connection_id = parameters["connection_id"]
        provider = library.connection(owner, connection_id)["provider"]
        if provider != parameters["provider"]:
            raise DataError("GOAL_DATA_SOURCE_CONFLICT")
        def check(connection=None):
            if mode == "goal":
                fence(context, parameters, connection)
            elif connection is None:
                context.require_active()
            else:
                context.assert_fenced(connection)
        check()
        guarded = GuardedContext(context, check, receipts)
        binding = context.storage_binding
        storage = library.market.storage
        reference_evidence = []
        byte_count = 0

        def charge(raw):
            nonlocal byte_count
            byte_count += len(raw)
            if byte_count > parameters["byte_limit"]:
                raise DataError("GOAL_DATA_BUDGET_EXCEEDED")

        if parameters.get("reference_connection") is not None:
            from .goal_sdk import akshare_references, cn_references

            def persist(capture, media_type="application/json"):
                if capture["scope"] != parameters["scope"]:
                    raise DataError("GOAL_DATA_SOURCE_CONFLICT")
                raw = capture["source_bytes"]
                charge(raw)
                with library.db.transaction() as con:
                    check(con)
                    ref = storage.put(binding, "market_data", raw, media_type=media_type, connection=con)
                return {**ref, "sdk":capture["sdk"], "observed_received_at":capture["received_at"]}

            args = (collect, owner, parameters["reference_connection"], parameters["requirement"],
                    guarded, parameters["deadline"], persist)
            if parameters.get("reference_kind") == "AKSHARE":
                securities, reference_evidence = akshare_references(*args)
                calendar = parameters["calendar_evidence"]
            else:
                calendar, securities, reference_evidence = cn_references(*args)
            prior, prepared = prepare(owner, parameters["requirement"], connection_id, provider, parameters["scope"],
                grant=parameters["grant"], calendar=calendar,
                identity_resolver=lambda _owner, _provider, ticker, _start, _end: securities[ticker],
                reference_connection=parameters["reference_connection"])
            if byte_count + prepared["byte_limit"] > parameters["byte_limit"]:
                raise DataError("GOAL_DATA_BUDGET_EXCEEDED")
            prepared["byte_limit"] = parameters["byte_limit"]
            parameters = {**parameters, **prepared}
        if mode == "service":
            context.require_active()
            if parameters["scope"] != collect.scope(provider):
                raise DataError("GOAL_DATA_SOURCE_CONFLICT")
            prior, prepared = prepare(owner, parameters["requirement"], connection_id, provider, parameters["scope"],
                grant=dict(download_requests=32, download_rows=10000, download_bytes=64*1024**2, download_jobs=1))
            if prepared is None:
                return {**fixed_ref(prior), "reused": True}
            parameters = {**parameters, **prepared, "deadline": time.time() + 1800,
                          "effect_id": context.job_id}
        req = parse(parameters["requirement"])
        cap = req.require_capability(provider)
        prior = None
        rows = []
        if parameters["prior"] is not None and parameters["acquisition"] != "FULL_WINDOW_REFRESH":
            prior = library.get(owner, parameters["prior"]["dataset_id"], include_rows=True, selectable=True)
            if fixed_ref(prior) != parameters["prior"] or not _compatible(prior, req, provider, connection_id, cap):
                raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
            rows.extend(_slice(prior, req))
        units = missing_units(req, rows)
        if fingerprint([vars(unit) for unit in units]) != parameters["unit_hash"]:
            raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
        if len(units) > parameters["call_limit"] or len(req.keys) > parameters["row_limit"]:
            raise DataError("GOAL_DATA_BUDGET_EXCEEDED")

        evidence = []
        batches = (collect.massive(guarded, connection_id, req, units, deadline=parameters["deadline"],
                                   maximum=parameters["request_limit"], charge=charge,
                                   remaining_bytes=lambda: parameters["byte_limit"] - byte_count,
                                   scope=parameters["scope"])
                   if units and provider == "massive" else None)
        from contextlib import closing, nullcontext
        with closing(batches) if batches is not None else nullcontext():
            for index, unit in enumerate(units):
                check()
                capture = (next(batches) if batches is not None else
                           collect(owner, provider, connection_id, unit, guarded, parameters["deadline"],
                                   **({"asset": req.asset} if provider in {"yfinance", "akshare", "openbb_cboe"} else {})))
                check()
                if capture["scope"] != parameters["scope"]:
                    raise DataError("GOAL_DATA_SOURCE_CONFLICT")
                raw = capture["source_bytes"]
                charge(raw)
                if type(capture["rows"]) is not list or any(
                    row["ticker"] != unit.symbol or not unit.start <= row["session_date"] <= unit.end
                    for row in capture["rows"]
                ):
                    raise DataError("GOAL_DATA_RESPONSE_SCOPE")
                rows.extend(capture["rows"])
                if len(rows) > parameters["row_limit"]:
                    raise DataError("GOAL_DATA_BUDGET_EXCEEDED")
                with library.db.transaction() as con:
                    check(con)
                    ref = storage.put(binding, "market_data", raw, media_type="application/json", connection=con)
                evidence.append(dict(symbol=unit.symbol, start=unit.start, end=unit.end, capture=ref,
                                     sdk=capture["sdk"], received_at=capture["received_at"],
                                     volume_basis=("LOTS_X_100_SHARES" if req.market == "CN" else "PROVIDER_OBSERVED")))
                context.checkpoint(min(90, (index + 1) * 90 // len(units)), {"completed_units": index + 1})
        normalized = merge_rows(req, rows, allow_missing_volume=req.adjustment != "raw")
        metadata = metadata_for(req.adjustment, cap)
        provenance = dict(provider=provider, market=req.market, asset=req.asset, kind=req.kind,
                          frequency=req.frequency, previous=parameters["prior"], sources=evidence,
                          acquisition=parameters["acquisition"],
                          exchanges={ticker: ("XSHG" if ticker.endswith(".SH") else "XSHE")
                                     for ticker in req.symbols} if req.market == "CN" else {},
                          calendar=parameters["calendar"],
                          required_sessions_sha256=fingerprint(parameters["requirement"]["required_sessions"]))
        if parameters.get("research_market") is not None:
            provenance["research_market"] = parameters["research_market"]
            provenance["identity_evidence"] = parameters["identity_evidence"]
            if any(fingerprint(provenance["identity_evidence"][ticker]) != detail["identity_sha256"]
                   for ticker, detail in provenance["research_market"]["securities"].items()):
                raise DataError("GOAL_DATA_IDENTITY_UNVERIFIED")
        if reference_evidence:
            provenance["reference_captures"] = reference_evidence
            provenance["reference_snapshot"] = dict(connection_id=parameters["reference_connection"],
                kind=parameters["reference_kind"], sessions=parameters["requirement"]["required_sessions"])
        universe = library.resolve_universe(owner, dict(manual=" ".join(req.symbols), groups=[], exclude=""))
        request = dict(connection_id=connection_id, name=parameters["name"],
                       universe=universe, start=req.start, end=req.end, metadata=metadata, rows=normalized,
                       source=dict(provider=provider, reference=parameters["effect_id"], universe_as_of=None),
                       received_at=clock().isoformat(), scope=parameters["scope"], kind="dataset", benchmark=None)
        return library.import_dataset(owner, request, "job:" + context.run.run_id, context=context,
                                      goal_data_provenance=provenance, publication_guard=check)

    def validate(parameters):
        common = {"provider", "connection_id", "scope", "requirement", "name"}
        goal = {"deadline", "task_id", "effect_id", "call_limit", "byte_limit"}
        references = {"reference_connection", "grant", "reference_kind", "calendar_evidence"}
        plan = {"prior", "row_limit", "request_limit", "unit_hash", "calendar", "research_market", "identity_evidence",
                "acquisition"}
        keys = set(parameters)
        if keys == common:
            return
        if keys not in (common | goal | references, common | goal | plan):
            raise DataError("GOAL_DATA_JOB_BINDING_REQUIRED")
        if not all(type(parameters[key]) is str and parameters[key] for key in ("task_id", "effect_id")):
            raise DataError("GOAL_DATA_JOB_BINDING_REQUIRED")

    runtime.handlers.register(HANDLER, handle, validate=validate, recoverable=False, auto_recovery_limit=0,
                              internal_only=True)

    def prepare(owner, need, connection_id, provider, scope, *, grant, calendar=None, identity_resolver=None,
                reference_connection=None):
        today = market_today(need)
        calendar = calendar or sessions(need, today=today)
        need = {**need, "required_sessions": calendar["sessions"]}
        req = Requirement.parse(need, today=today)
        cap = req.require_capability(provider)
        prior, _, units, reusable = _plan(library, owner, req, provider, connection_id, cap, scope,
                                          reference_connection=reference_connection)
        if reusable and reference_connection is None:
            return prior, None
        acquisition = "FULL_WINDOW" if prior is None else "GAP_FILL" if units else "SINGLE_SNAPSHOT_SLICE"
        if prior is not None and units and req.adjustment != "raw":
            units = missing_units(req, ())
            acquisition = "FULL_WINDOW_REFRESH"
        if units and not collect.supports(provider):
            raise DataError("GOAL_DATA_RUNTIME_UNAVAILABLE")
        if scope not in {"synthetic", "public"} or prior is not None and prior["scope"] != scope:
            raise DataError("GOAL_DATA_SOURCE_CONFLICT")
        if provider == "massive" and units:
            from .goal_massive import massive_bound
            requests = massive_bound(units)
            bytes_required = grant.get("download_bytes", 0)
            if not bytes_required:
                raise DataError("GOAL_DATA_BUDGET_EXCEEDED")
        else:
            requests = len(units) * collect.requests_per_unit(provider) if units else 0
            bytes_required = requests * LIMITS["response_bytes"]
        if (requests > grant.get("download_requests", 0) or len(req.keys) > grant.get("download_rows", 0)
                or bytes_required > grant.get("download_bytes", 0) or not grant.get("download_jobs", 0)):
            raise DataError("GOAL_DATA_BUDGET_EXCEEDED")
        research_market, identity_evidence = _research_market(owner, req, provider, calendar, identity_resolver or identities)
        return prior, dict(requirement=need, provider=provider, connection_id=connection_id,
                           prior=None if prior is None else fixed_ref(prior), scope=scope,
                           acquisition=acquisition,
                           call_limit=len(units), row_limit=len(req.keys), byte_limit=bytes_required, request_limit=requests,
                           unit_hash=fingerprint([vars(unit) for unit in units]), calendar=calendar["source"],
                           research_market=research_market, identity_evidence=identity_evidence)

    def data(principal, request, grant, active, link):
        active()
        plan = request["outputs"]["PLAN"]
        needs = plan.get("data_needs", [])
        if len(needs) != 1:
            return dict(kind="WAITING", reason_code="GOAL_DATA_REQUIREMENT_UNSUPPORTED")
        try:
            owner = principal.user_id
            source = request["snapshot"]["data_source"]
            connection_id = source["connection_id"]
            provider = library.connection(owner, connection_id)["provider"]
            if provider != source["provider"]:
                raise DataError("GOAL_DATA_SOURCE_CONFLICT")
            need = dict(needs[0])
            today = market_today(need)
            reference_connection = source.get("reference_connection_id") or (connection_id if provider == "tushare" else None)
            if need.get("market") == "CN":
                from .goal_data import day, symbol

                tickers = need.get("symbols")
                if (type(tickers) is not list or not 1 <= len(tickers) <= 32
                        or not all(symbol(ticker, "CN") for ticker in tickers)
                        or len(set(tickers)) != len(tickers)
                        or not 0 <= (day(need.get("end")) - day(need.get("start"))).days < 366
                        or day(need["end"]) >= today):
                    raise DataError("GOAL_DATA_REQUIREMENT_INVALID")
                # A trusted original remains usable when this host loses a runner
                # or gains calendar coverage after the dataset was published.
                approved_references = [reference_connection]
                if provider == "akshare_cn":
                    approved_references.append(connection_id)
                for approved in dict.fromkeys(approved_references):
                    if approved is None:
                        continue
                    saved = _reference_snapshot(library, owner, need, connection_id, provider, source["scope"], approved, today)
                    if saved is not None:
                        return _done(saved, reused=True)
            calendar = None
            own_roster = False
            if (need.get("market") == "CN" and provider == "akshare_cn"
                    and getattr(collect, "akshare_reference_available", False)):
                try:
                    calendar = sessions(need, today=today)
                except DataError as exc:
                    if exc.code != "GOAL_DATA_CALENDAR_UNAVAILABLE":
                        raise
                else:
                    own_roster = True
                    reference_connection = connection_id
            if (need.get("market") == "CN" and reference_connection is not None
                    and (own_roster or getattr(collect, "reference_available", False))):
                if source.get("execution") != "BOUND":
                    raise DataError("GOAL_DATA_RUNTIME_UNAVAILABLE")
                if len(tickers) > 8:
                    raise DataError("GOAL_DATA_REQUIREMENT_INVALID")
                if (library.connection(owner, reference_connection)["provider"] != ("akshare_cn" if own_roster else "tushare")
                        or not collect.supports(provider)):
                    raise DataError("GOAL_DATA_REFERENCE_INVALID")
                try:
                    known = sessions(need, today=today)
                except DataError as exc:
                    if exc.code != "GOAL_DATA_CALENDAR_UNAVAILABLE":
                        raise
                else:
                    req = Requirement.parse({**need, "required_sessions": known["sessions"]}, today=today)
                    cap = req.require_capability(provider)
                    saved, _, _, exact = _plan(library, owner, req, provider, connection_id, cap, source["scope"])
                    provenance = saved.get("goal_data_provenance", {}) if exact else {}
                    if provenance.get("research_market") and "reference_captures" not in provenance:
                        return _done(saved, reused=True)
                if (need.get("asset"), need.get("kind"), need.get("frequency"), need.get("currency"),
                        need.get("adjustment")) != ("CN_EQUITY", "bars", "1Day", "CNY", "raw"):
                    raise DataError("GOAL_DATA_REQUIREMENT_UNSUPPORTED")
                if own_roster:
                    from .akshare_reference import roster_category
                    overhead = len({roster_category(ticker) for ticker in tickers})
                else:
                    overhead = len(tickers) + len({ticker.split(".")[-1] for ticker in tickers})
                reference_bytes = overhead * LIMITS["response_bytes"]
                if (grant.get("download_jobs", 0) < 1 or grant.get("download_requests", 0) < overhead
                        or grant.get("download_bytes", 0) < reference_bytes
                        or grant.get("download_rows", 0) < len(tickers)):
                    raise DataError("GOAL_DATA_BUDGET_EXCEEDED")
                reduced = {**grant, "download_requests": grant["download_requests"] - overhead,
                           "download_bytes": grant["download_bytes"] - reference_bytes}
                parameters = dict(requirement=need, connection_id=connection_id, provider=provider, scope=source["scope"],
                                  reference_connection=reference_connection, grant=reduced,
                                  reference_kind="AKSHARE" if own_roster else "TUSHARE", calendar_evidence=calendar,
                                  byte_limit=grant["download_bytes"], call_limit=overhead)
                prior = None
            else:
                prior, parameters = prepare(owner, need, connection_id, provider, source["scope"], grant=grant)
            if parameters is None:
                return _done(prior, reused=True, window={"start": needs[0]["start"], "end": needs[0]["end"]},
                             symbols=needs[0]["symbols"])
            if parameters["call_limit"] and source.get("execution") != "BOUND":
                return dict(kind="WAITING", reason_code="GOAL_DATA_RUNTIME_UNAVAILABLE")
            effect = request["operation_key"]
            identifier(effect)
            key = "goal-data-" + sha256(effect.encode()).hexdigest()
            parameters.update(deadline=request["deadline"], task_id=request["task_id"], effect_id=effect,
                              name="Goal research " + request["task_id"][:64])
            # The gate must also be checked by the child against the same durable intent.
            active()
            child = runtime.jobs.submit_internal(principal, HANDLER, parameters, idempotency_key=key,
                                        storage_binding=request["snapshot"]["storage"],
                                        goal_link=lambda con, job_id: link(con, {"kind": "job", "id": job_id}))
            return dict(kind="PENDING", child=dict(kind="job", id=child["job_id"]))
        except DataError as exc:
            return dict(kind="WAITING", reason_code=exc.code)

    def poll(principal, child):
        job = runtime.jobs.get(principal, child["id"])
        if job["handler"] != HANDLER:
            raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
        if job["status"] == "SUCCEEDED":
            record = library.get(principal.user_id, job["result"]["dataset_id"], selectable=True)
            reused = job["result"].get("reused") is True
            if fixed_ref(record) != fixed_ref(job["result"]) or (not reused and record["origin_job_id"] != job["job_id"]):
                raise DataError("GOAL_DATA_REFERENCE_MISMATCH")
            return _done(record, reused=reused)
        if job["status"] in {"FAILED", "CANCELLED"}:
            if receipts.uncertain(job["job_id"]):
                return dict(kind="WAITING", reason_code="GOAL_EFFECT_UNCERTAIN")
            return dict(kind="FAILED", reason_code=job["error_code"] or "GOAL_DATA_CHILD_FAILED")
        return dict(kind="PENDING", child=child)

    def manual(principal, request, key, provider):
        cap = CAPABILITIES[provider]
        adjustment = cap.adjustments[0]
        if request["kind"] != "dataset" or request.get("benchmark") is not None:
            raise DataError("GOAL_DATA_REQUIREMENT_UNSUPPORTED")
        if request["metadata"] not in (None, metadata_for(adjustment, cap)):
            raise DataError("GOAL_DATA_REQUIREMENT_UNSUPPORTED")
        owner = principal.user_id
        library.validate_selection(owner, request["universe"])
        need = dict(market=cap.market, asset=cap.assets[0], kind="bars", frequency="1Day",
                    currency=cap.currency, adjustment=adjustment, start=request["start"], end=request["end"],
                    symbols=request["universe"]["tickers"])
        scope = collect.scope(provider)
        if scope is None:
            raise DataError("GOAL_DATA_RUNTIME_UNAVAILABLE")
        # Keep the job's idempotency payload stable. The worker resolves the plan
        # against the already snapped location and the exchange calendar.
        parameters = dict(requirement=need, provider=provider, connection_id=request["connection_id"],
                          scope=scope, name=request["name"] or "日线数据")
        return runtime.jobs.submit_internal(principal, HANDLER, parameters, idempotency_key=key)

    runtime.goal_provider_download = manual
    runtime.goal_provider_status = {
        provider: ("BOUND_PUBLIC" if collect.scope(provider) == "public" else "BOUND_OFFLINE_FIXTURE")
        for provider in CAPABILITIES if collect.supports(provider)
    }
    return data, poll


def _done(record, *, reused, window=None, symbols=None):
    provenance = record.get("goal_data_provenance", {})
    return dict(kind="DONE", output=dict(reference=fixed_ref(record), reused=reused,
                window=window or {"start":record["start"], "end":record["end"]},
                symbols=symbols or record["universe"]["tickers"],
                coverage=record["quality"], provenance=dict(source=record["source"],
                digest=fingerprint(provenance) if provenance else None,
                acquisition="REUSED" if reused else provenance.get("acquisition"),
                receipt_only=True, pit_status=record["pit_status"])))


def _research_market(owner, req, provider, calendar, identities):
    if req.market != "CN" or identities is None:
        return None, {}
    security = {}
    records = {}
    for ticker in req.symbols:
        identity = identities(owner, provider, ticker, req.start, req.end)
        if identity is None:
            return None, {}
        mic = "XSHG" if ticker.endswith(".SH") else "XSHE"
        if (type(identity) is not dict or set(identity) != {"symbol", "mic", "asset", "valid_from", "valid_through", "evidence"}
                or identity["symbol"] != ticker or identity["mic"] != mic or identity["asset"] != "CN_EQUITY"
                or not identity["valid_from"] <= req.start <= req.end <= identity["valid_through"]
                or type(identity["evidence"]) is not dict or not identity["evidence"]):
            raise DataError("GOAL_DATA_IDENTITY_UNVERIFIED")
        security[ticker] = {"mic": mic, "identity_sha256": fingerprint(identity)}
        records[ticker] = identity
    market_calendar = calendar["source"]
    if type(market_calendar) is not dict or set(market_calendar) != {row["mic"] for row in security.values()}:
        raise DataError("GOAL_DATA_CALENDAR_UNAVAILABLE")
    return dict(schema="CN_EQUITY_RAW_CNY_DAILY_SIMULATION_1", calendar=market_calendar,
                required_sessions_sha256=fingerprint(calendar["sessions"]), securities=security,
                corporate_actions="UNVERIFIED"), records

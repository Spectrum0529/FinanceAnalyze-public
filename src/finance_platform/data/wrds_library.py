"""WRDS job admission, capture and publication through the fixed data library."""
from contextlib import closing
from datetime import datetime, timezone
import json
import re

from finance_platform.core.contracts import fingerprint, identifier
from finance_platform.core.errors import PlatformError
from .contracts import DataError
from .simple_chunks import DatasetChunks, FORMAT
from .wrds import (MAX_DAYS, MAX_ROWS, MAX_SECURITIES, META,
                   PROFILE, SCOPE, WrdsCredentials, WrdsSource, history_request, lookup_request)


LOOKUP_JOB = "data.wrds-security-lookup"
HISTORY_JOB = "data.wrds-history"
_GENERATION = re.compile(r"[0-9a-f]{32}")
_REFERENCE = re.compile(r"credential:FA_[0-9a-f]{64}")


def is_history_request(value):
    return type(value) is dict and "securities" in value


def capability(runtime):
    ready = callable(getattr(runtime, "wrds_history_submit", None))
    return {
        "provider": "wrds",
        "credentials_required": True,
        "status": ("REGISTERED_USER_CONFIGURATION_REQUIRED_REAL_CALLS_UNVERIFIED"
                   if ready else "SERVICE_UNREGISTERED"),
        "available": ready,
        "lookup": ready,
        "history": ready,
        "max_symbols": MAX_SECURITIES,
        "maximum_calendar_days": MAX_DAYS,
        "max_rows": MAX_ROWS,
        "metadata": dict(META),
        "data_capability": {
            "profile": PROFILE,
            "market": "US",
            "assets": ["US_EQUITY", "US_ETF"],
            "frequencies": ["1Day"],
            "adjustments": ["raw"],
            "entitlement": "UNVERIFIED",
            "backtest_input": "FIXED_DATASET_AFTER_QUALIFICATION",
            "ai_research_input": "SINGLE_ASSISTANT_SCHEMA_PLAN_LOCAL_TOOLS",
            "autonomous_research_input": "UNAVAILABLE",
            "automatic_gap_acquisition": False,
        },
    }


def _bindings(row):
    if (type(row) is not dict or set(row) != {"request", "credential_ref", "credential_generation"}
            or type(row["credential_ref"]) is not str or not _REFERENCE.fullmatch(row["credential_ref"])
            or type(row["credential_generation"]) is not str
            or not _GENERATION.fullmatch(row["credential_generation"])):
        raise DataError("WRDS_CREDENTIAL_REQUIRED")


def require_capture(body, manifest, identities, context):
    if (body["source"].get("provider") != "wrds" or body["scope"] != SCOPE
            or body["kind"] != "dataset" or body["metadata"] != META
            or context is None or context.run.handler != HISTORY_JOB
            or type(manifest) is not dict or manifest.get("format") != FORMAT
            or type(identities) is not list or not identities
            or type(manifest.get("wrds_capture")) is not dict
            or manifest["wrds_capture"].get("profile") != PROFILE
            or manifest["wrds_capture"].get("identities") != identities):
        raise DataError("WRDS_CAPTURE_REQUIRED")


def install_wrds(runtime, library, *, source=None):
    generation_for = getattr(runtime, "wrds_credential_generation", None)
    resolve = getattr(runtime, "wrds_credential", None)
    if not callable(generation_for) or not callable(resolve):
        return

    source = source if source is not None else WrdsSource()

    def download(context, request, credentials, recheck):
        owner = context.run.owner_id
        parameters = history_request(request)
        securities, start, end = parameters
        universe = library.resolve_universe(owner, {"manual": " ".join(item.ticker for item in securities)})
        binding = library.market.bind(owner, context=context)
        storage = library.market.storage
        chunks = DatasetChunks(library.policy, owner, binding=binding)
        descriptors, originals, coverage = [], [], {}

        def retain(raw, rows):
            context.require_active()
            originals.append(storage.put(binding, "market_data", raw,
                                         media_type="application/vnd.financeanalyze.wrds-source+json"))
            descriptors.append(chunks.put(rows, start=start.isoformat(), end=end.isoformat(),
                                          scope=SCOPE, context=context, provider="wrds"))
            for row in rows:
                ticker, day = row["ticker"], row["session_date"]
                item = coverage.setdefault(ticker, {"ticker": ticker, "rows": 0, "first": day, "last": day})
                item["rows"] += 1
                item["last"] = day

        capture = source.history(context, credentials, parameters, retain)
        received = datetime.now(timezone.utc).isoformat()
        capture.update({
            "received_at": received,
            "source_origin": "WRDS PostgreSQL over verified TLS",
            "terms": "WRDS_SUBSCRIPTION_LOCAL_USE_NO_REDISTRIBUTION_GRANT",
            "projected_source": [
                {"ref": ref,
                 "normalized_sha256": descriptor["rows_hash"]}
                for ref, descriptor in zip(originals, descriptors, strict=True)
            ],
        })
        manifest = {
            "format": FORMAT, "chunks": descriptors, "row_count": capture["rows"],
            "securities": [coverage[item.ticker] for item in securities],
            "status": "COMPLETE_REQUESTED_SYMBOL_RESPONSES",
            "completeness": "UNVERIFIED_PROVIDER_RESPONSE",
            "missing_sessions": "NOT_INFERRED", "wrds_capture": capture,
        }
        body = {
            "connection_id": request["connection_id"], "name": request["name"],
            "universe": universe, "start": start.isoformat(), "end": end.isoformat(),
            "metadata": dict(META), "rows": None, "kind": "dataset", "benchmark": None,
            "source": {"provider": "wrds", "reference": PROFILE, "universe_as_of": None},
            "scope": SCOPE, "received_at": received,
        }
        return library.import_dataset(owner, body, "job:" + context.run.run_id, context=context,
                                      security_metadata=capture["identities"], manifest=manifest,
                                      publication_guard=lambda _: recheck())

    def install_handler(handler, parse):
        def validate(parameters):
            _bindings(parameters)
            parse(parameters["request"])

        def execute(context, parameters):
            validate(parameters)
            owner, request, ref, generation = (context.run.owner_id, parameters["request"],
                                               parameters["credential_ref"], parameters["credential_generation"])
            context.require_active()
            connection = library.connection(owner, request["connection_id"])
            if connection["provider"] != "wrds" or connection["credential_ref"] != ref:
                raise DataError("WRDS_CREDENTIAL_REQUIRED")

            def recheck():
                if generation_for(owner, ref) != generation:
                    raise DataError("CREDENTIAL_GENERATION_CHANGED")

            credentials = WrdsCredentials.from_value(resolve(owner, ref, expected_generation=generation))
            if handler == LOOKUP_JOB:
                result = source.lookup(context, credentials, parse(request))
                recheck()
                return result
            return download(context, request, credentials, recheck)

        runtime.handlers.register(handler, execute, validate=validate, internal_only=True,
                                  recoverable=False, auto_recovery_limit=0)

        def replay(principal, request, key):
            owner = principal.user_id
            with closing(runtime.database.connect()) as connection:
                old = connection.execute(
                    "SELECT j.job_id,j.handler,j.parameters_json FROM jobs j JOIN job_keys k "
                    "ON k.job_id = j.job_id WHERE k.owner_id=? AND j.owner_id=? AND k.idempotency_key=?",
                    (owner, owner, key),
                ).fetchone()
            if old is None:
                return None
            if old[1] != handler or fingerprint(json.loads(old[2]).get("request")) != fingerprint(request):
                raise DataError("IDEMPOTENCY_CONFLICT")
            return runtime.jobs.get(principal, old[0])

        def submit(principal, body):
            runtime.require_owner(principal, principal.user_id, write=True)
            if type(body) is not dict or set(body) != {"request", "idempotency_key"}:
                raise DataError("WRDS_INPUT_INVALID")
            request, key, owner = body["request"], body["idempotency_key"], principal.user_id
            identifier(key)
            parse(request)
            # Replays yield the original job even after credential rotation.
            old = replay(principal, request, key)
            if old is not None:
                return old
            connection = library.connection(owner, request["connection_id"])
            if connection["provider"] != "wrds":
                raise DataError("WRDS_CREDENTIAL_REQUIRED")
            ref = connection["credential_ref"]
            if type(ref) is not str or not _REFERENCE.fullmatch(ref):
                raise DataError("WRDS_CREDENTIAL_REQUIRED")
            parameters = {"request": request, "credential_ref": ref,
                          "credential_generation": generation_for(owner, ref)}
            try:
                return runtime.jobs.submit_internal(principal, handler, parameters, idempotency_key=key)
            except PlatformError as error:
                if error.code == "CONFLICT":
                    old = replay(principal, request, key)
                    if old is not None:
                        return old
                raise

        return submit

    runtime.wrds_lookup_submit = install_handler(LOOKUP_JOB, lookup_request)
    runtime.wrds_history_submit = install_handler(HISTORY_JOB, history_request)

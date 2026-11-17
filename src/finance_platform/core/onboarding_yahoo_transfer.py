"""Re-establish owner-bound Yahoo acquisition using the actual guarded downloader."""
from contextlib import closing
from datetime import date
import json
from typing import Literal

from pydantic import Field

from finance_platform.data.market_lifecycle import read_connection
from finance_platform.data.native_library_download import download_native
from finance_platform.data.provider_sdk.history_contracts import QUANTITY_BASIS, YAHOO_PRICE_POLICY
from finance_platform.data.simple_chunks import BATCH_ROWS, MAX_CHUNKS
from finance_platform.data.simple_inputs import tickers
from .contracts import digest, fingerprint, identifier
from .errors import PlatformError
from .team_profiles import Strict


HANDLER = "onboarding.migration-yahoo"
RETRY_KEY_PREFIX = "migration-yahoo-retry-"
IDENTITY_FIELDS = ("instrument_id", "ticker", "currency", "mic", "asset_class")
REF_FIELDS = ("source_ref", "history_ref", "observation_ref", "identity_evidence_ref")
CHUNK_FIELDS = ("rows_hash", "row_count", "first_key", "last_key")
YAHOO_METADATA = {"currency": "USD", "timezone": "America/New_York",
                  "price_unit": "per_adjusted_simulation_unit", "volume_unit": "provider_reported_unverified",
                  "adjustment": "split_adjusted"}


class ChunkCommitment(Strict):
    rows_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    row_count: int = Field(ge=1, le=BATCH_ROWS)
    first_key: list[str] = Field(min_length=2, max_length=2)
    last_key: list[str] = Field(min_length=2, max_length=2)


class CaptureReference(Strict):
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class SourceReference(CaptureReference):
    source_id: str
    version: int = Field(ge=1)


class HistoryReference(CaptureReference):
    history_id: str
    version: int = Field(ge=1)


class ObservationReference(CaptureReference):
    result_id: str


class SecurityReference(CaptureReference):
    security_id: str


class SecurityCommitment(Strict):
    ticker: str
    instrument: dict[str, str]
    source_ref: SourceReference
    history_ref: HistoryReference
    observation_ref: ObservationReference
    identity_evidence_ref: SecurityReference


class NativeCapture(Strict):
    provider: Literal["yfinance"]
    price_policy: Literal["YAHOO_NATIVE_SPLIT_ADJUSTED_PRICE_PATH_V1"]
    quantity_basis: Literal["ADJUSTED_SIMULATION_UNITS"]
    metadata: dict
    row_count: int = Field(ge=1)
    chunks: list[ChunkCommitment] = Field(min_length=1, max_length=MAX_CHUNKS)
    securities: list[SecurityCommitment] = Field(min_length=1, max_length=32)


class NativeYahooPayload(Strict):
    name: str = Field(min_length=1, max_length=120)
    start: str
    end: str
    symbols: list[str] = Field(min_length=1, max_length=32)
    capture: NativeCapture


class RetryBinding(Strict):
    previous_job_id: str
    key_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RebindRequest(Strict):
    origin_id: str
    origin_version: str
    origin_content_hash: str
    payload: NativeYahooPayload
    connection_id: str
    connection_revision: int = Field(ge=0)
    bundle_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    retry: RetryBinding | None = None


def _changed():
    return PlatformError("MIGRATION_YAHOO_CAPTURE_CHANGED", 409)


def classify_capture(body, manifest, identities):
    acquisition = manifest["acquisition"]
    if (acquisition["provider"] != "yfinance" or acquisition["price_policy"] != YAHOO_PRICE_POLICY
            or acquisition["quantity_basis"] != QUANTITY_BASIS or acquisition["raw_compatible"] is not False):
        raise _changed()
    instruments = {entry["ticker"]: {field: entry["instrument"][field] for field in IDENTITY_FIELDS}
                   for entry in identities}
    securities = [{"ticker": entry["ticker"], "instrument": instruments[entry["ticker"]],
                   **{name: entry[name] for name in REF_FIELDS}} for entry in acquisition["securities"]]
    return {"provider": "yfinance", "price_policy": YAHOO_PRICE_POLICY, "quantity_basis": QUANTITY_BASIS,
            "metadata": body["metadata"], "row_count": manifest["row_count"],
            "chunks": [{field: chunk[field] for field in CHUNK_FIELDS} for chunk in manifest["chunks"]],
            "securities": securities}


def _comparison(capture):
    value = capture.model_dump(exclude={"securities"})
    # Destination acquisition IDs and window receipt timestamps are new.
    # Match the exact rows, identity and source declaration across owners.
    value["securities"] = [{"ticker": item.ticker, "instrument": item.instrument,
                            "source_id": item.source_ref.source_id,
                            "source_hash": item.source_ref.content_hash} for item in capture.securities]
    return value


def require_same_capture(expected, body, manifest, identities):
    observed = NativeCapture.model_validate(classify_capture(body, manifest, identities))
    if ((body["start"], body["end"], body["universe"]["tickers"]) !=
            (expected.start, expected.end, expected.symbols)
            or _comparison(observed) != _comparison(expected.capture)):
        raise _changed()


def validate_payload(value):
    payload = NativeYahooPayload.model_validate(value)
    start, end = date.fromisoformat(payload.start), date.fromisoformat(payload.end)
    if (start.isoformat() != payload.start or end.isoformat() != payload.end or start > end
            or tickers(" ".join(payload.symbols)) != payload.symbols
            or payload.capture.metadata != YAHOO_METADATA
            or [item.ticker for item in payload.capture.securities] != payload.symbols
            or sum(chunk.row_count for chunk in payload.capture.chunks) != payload.capture.row_count):
        raise PlatformError("MIGRATION_PACKAGE_INVALID", 422)
    for item in payload.capture.securities:
        if set(item.instrument) != set(IDENTITY_FIELDS) or item.instrument["ticker"] != item.ticker:
            raise PlatformError("MIGRATION_PACKAGE_INVALID", 422)
        for ref in (item.source_ref.source_id, item.history_ref.history_id,
                    item.observation_ref.result_id, item.identity_evidence_ref.security_id):
            identifier(ref)
    return payload


def _connection_revision(con, owner, connection_id):
    connection = read_connection(con, owner, connection_id)
    user = con.execute("SELECT enabled,role FROM users WHERE user_id=?", (owner,)).fetchone()
    if (not user or not user["enabled"] or user["role"] not in {"admin", "researcher"}
            or connection["archived"] or connection["provider"] != "yfinance"
            or connection["credential_ref"] is not None):
        raise PlatformError("MIGRATION_YAHOO_CONNECTION_INVALID", 409)
    return connection["revision"]


def connection_revision(runtime, owner, connection_id):
    with closing(runtime.database.connect()) as con:
        return _connection_revision(con, owner, connection_id)


def job_key(origin):
    return "migration-yahoo-" + fingerprint(origin)


def job_record(runtime, owner, origin):
    with closing(runtime.database.connect()) as con:
        return con.execute("""WITH RECURSIVE attempts(job_id, depth) AS (
            SELECT j.job_id,0 FROM job_keys k JOIN jobs j USING(job_id)
             WHERE k.owner_id=:owner AND j.owner_id=:owner AND j.handler=:handler AND k.idempotency_key=:key
            UNION ALL
            SELECT j.job_id,a.depth+1 FROM attempts a
              JOIN job_keys k ON k.owner_id=:owner AND k.idempotency_key=:prefix || a.job_id
              JOIN jobs j ON j.job_id=k.job_id AND j.owner_id=:owner AND j.handler=:handler
        )
        SELECT j.job_id,j.status,j.error_code,j.parameters_json,j.result_json
          FROM attempts a JOIN jobs j USING(job_id) ORDER BY a.depth DESC LIMIT 1""",
            {"owner": owner, "handler": HANDLER, "key": job_key(origin), "prefix": RETRY_KEY_PREFIX}).fetchone()


def previous_job(runtime, owner, job_id):
    with closing(runtime.database.connect()) as con:
        return con.execute("""SELECT job_id,status,parameters_json FROM jobs
                               WHERE owner_id=? AND handler=? AND job_id=?""",
                           (owner, HANDLER, job_id)).fetchone()


def retry_successor(runtime, owner, job_id):
    with closing(runtime.database.connect()) as con:
        return con.execute("""SELECT j.job_id,j.status,j.parameters_json FROM job_keys k JOIN jobs j USING(job_id)
                               WHERE k.owner_id=? AND j.owner_id=? AND j.handler=? AND k.idempotency_key=?""",
                           (owner, owner, HANDLER, RETRY_KEY_PREFIX + job_id)).fetchone()


def submit(runtime, principal, item, origin, bundle_sha256, *, retry=None):
    from finance_platform.data.source_onboarding import ensure_yahoo_connection
    key = RETRY_KEY_PREFIX + retry.previous_job_id if retry is not None else job_key(origin)
    connection_key = (key + "-connection" if retry is None
                      else "migration-yahoo-connection-" + fingerprint(retry.model_dump()))
    owner = principal.user_id
    library = runtime.dataset_library
    connection = ensure_yahoo_connection(library, principal, connection_key)["connection_id"]
    revision = connection_revision(runtime, owner, connection)
    request = RebindRequest(origin_id=item.origin_id, origin_version=item.origin_version,
        origin_content_hash=item.origin_content_hash, payload=validate_payload(item.payload),
        connection_id=connection, connection_revision=revision, bundle_sha256=bundle_sha256, retry=retry)
    return runtime.jobs.submit_internal(principal, HANDLER, request.model_dump(), idempotency_key=key)


def validate_job(parameters):
    request = RebindRequest.model_validate(parameters)
    identifier(request.origin_id)
    identifier(request.connection_id)
    digest(request.origin_content_hash)
    if request.retry is not None:
        identifier(request.retry.previous_job_id)
        digest(request.bundle_sha256)
    validate_payload(parameters["payload"])


def install(runtime):
    def recover(context, parameters):
        request = RebindRequest.model_validate(parameters)
        expected = request.payload
        owner = context.run.owner_id
        library = runtime.dataset_library

        def verify_connection():
            if connection_revision(runtime, owner, request.connection_id) != request.connection_revision:
                raise PlatformError("MIGRATION_YAHOO_CONNECTION_INVALID", 409)

        def commit_guard(con):
            if _connection_revision(con, owner, request.connection_id) != request.connection_revision:
                raise PlatformError("MIGRATION_YAHOO_CONNECTION_INVALID", 409)

        def before_publish(body, manifest, identities):
            context.require_active()
            verify_connection()
            require_same_capture(expected, body, manifest, identities)

        context.require_active()
        verify_connection()
        body = {"connection_id": request.connection_id, "name": expected.name,
                "universe": library.resolve_universe(owner, {"manual": " ".join(expected.symbols)}),
                "start": expected.start, "end": expected.end, "metadata": None,
                "kind": "dataset", "benchmark": None}
        result = download_native(runtime, library, context, body, "yfinance",
                                 before_publish=before_publish, publication_guard=commit_guard)
        return {**result, "origin_reference": {"id": request.origin_id, "version": request.origin_version,
                    "content_hash": request.origin_content_hash}}

    runtime.handlers.register(HANDLER, recover, validate=validate_job, recoverable=False,
                              auto_recovery_limit=0, internal_only=True)

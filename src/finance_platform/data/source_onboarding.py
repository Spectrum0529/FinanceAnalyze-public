"""Onboarding defaults and bounded, explicit source checks."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import time

from finance_platform.core.auth import AccountDelegation, require_owner
from finance_platform.core.contracts import identifier
from finance_platform.core.errors import PlatformError
from .contracts import DataError
from .market_lifecycle import read_connection, require_credential
from .provider_sdk.contracts import LIMITS
from .provider_sdk.history_contracts import YAHOO_PRICE_POLICY

CHECK_JOB = "data.connection-check"
PRESET = "first_example_yahoo"
DEFAULTS = {"US_EQUITY": "yfinance", "US_ETF": "yfinance", "CN_EQUITY": "akshare_cn", "CSV": "csv"}
PROVIDER_MARKETS = {
    "yfinance": "US", "akshare": "US", "openbb_cboe": "US", "openbb_nasdaq": "US",
    "massive": "US", "wrds": "US", "akshare_cn": "CN", "tushare": "CN", "csv": None,
}
US_BACKTEST = {"yfinance", "akshare", "massive", "wrds"}
PUBLIC_CHECK_PROVIDERS = {"yfinance"}
CHECK_WINDOW_DAYS = 7
CHECK_SECONDS = 90
CHECK_COOLDOWN = 60
CHECK_RESULT_SECONDS = 600

MIGRATIONS = ("""
CREATE TABLE data_connection_check_gate(
    provider TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    reserved_at REAL NOT NULL
);
""",)


def add_onboarding_catalog(catalog, *, check_registered=True):
    for item in catalog["items"]:
        provider = item["provider"]
        if provider not in PROVIDER_MARKETS:
            continue
        csv = provider == "csv"
        item["check_mode"] = ("IMPORT_PREVIEW" if csv else "UNAVAILABLE" if not check_registered else
                              "PUBLIC_SAMPLE" if provider in PUBLIC_CHECK_PROVIDERS else "CONFIGURATION_ONLY")
        item["onboarding_compatibility"] = {
            "market": PROVIDER_MARKETS[provider],
            "us_portfolio_backtest": None if csv else provider in US_BACKTEST,
            "scope": "LOCAL_ONLY" if provider == "wrds" else "LOCAL_PERSONAL",
        }
        if provider == "massive":
            item["explicit_sample_check"] = {
                "maximum_symbols": 1, "maximum_calendar_days": 7,
                "quota_or_cost": "PROVIDER_PLAN_DEPENDENT", "requires_confirmation": True,
            }
    catalog["onboarding"] = {
        "defaults": dict(DEFAULTS),
        "check_limits": {"ticker": "SPY", "calendar_days": CHECK_WINDOW_DAYS, "maximum_rows": CHECK_WINDOW_DAYS,
                         "seconds": CHECK_SECONDS, "physical_requests": 2 * LIMITS["requests"],
                         "response_bytes": LIMITS["response_bytes"], "total_response_bytes": 2 * LIMITS["unit_bytes"],
                         "cooldown_seconds": CHECK_COOLDOWN, "result_valid_seconds": CHECK_RESULT_SECONDS},
    }
    return catalog


def ensure_yahoo_connection(library, principal, idempotency_key):
    owner = principal.user_id
    require_owner(principal, owner, write=True)

    def guard(con):
        _verify_actor(con, owner)
        if isinstance(principal, AccountDelegation):
            principal.check_approval(con)

    def choose(con):
        existing = con.execute("""
            SELECT c.id FROM data_library_connections c
            LEFT JOIN data_library_connection_events e ON e.id=c.id AND e.revision=(
                SELECT max(revision) FROM data_library_connection_events WHERE id=c.id)
            WHERE c.owner=? AND coalesce(e.archived,0)=0
              AND json_extract(coalesce(e.body,c.body),'$.provider')='yfinance'
              AND json_type(coalesce(e.body,c.body),'$.credential_ref')='null'
            ORDER BY c.id LIMIT 1
        """, (owner,)).fetchone()
        if existing is None:
            created = library._insert_connection(con, owner, {
                "name": "Yahoo 财经 · 首次示例", "provider": "yfinance", "credential_ref": None,
            })
            return {"connection_id": created["connection_id"], "provider": "yfinance", "created": True}
        return {"connection_id": existing[0], "provider": "yfinance", "created": False}

    return library._write(owner, idempotency_key, {"action": "ensure_connection", "preset": PRESET},
                          choose, publication_guard=guard)


def _job_parameters(value):
    if (type(value) is not dict or set(value) != {"connection_id", "provider", "revision"}
            or type(value["provider"]) is not str or value["provider"] not in PROVIDER_MARKETS
            or type(value["revision"]) is not int or value["revision"] < 0):
        raise DataError("INVALID_INPUT")
    identifier(value["connection_id"])


def _job_view(job):
    return {key: job[key] for key in ("job_id", "run_id", "status")}


def _verify_actor(con, owner):
    row = con.execute("SELECT enabled,role FROM users WHERE user_id=?", (owner,)).fetchone()
    if not row or not row[0] or row[1] not in {"admin", "researcher"}:
        raise PlatformError("FORBIDDEN", 403)


def _configuration(runtime, owner, connection):
    provider = connection["provider"]
    if provider in {"massive", "tushare", "wrds"}:
        reference = connection["credential_ref"]
        if not reference:
            return "CREDENTIAL_REQUIRED"
        try:
            if provider == "wrds":
                generation = getattr(runtime, "wrds_credential_generation", None)
                if not callable(generation):
                    return "RUNTIME_UNAVAILABLE"
                generation(owner, reference)
            else:
                require_credential(runtime, owner, provider, reference)
        except PlatformError:
            return "CREDENTIAL_REQUIRED"
    runtime_status = getattr(runtime, "goal_provider_status", {})
    if provider in {"akshare_cn", "tushare", "openbb_nasdaq"} and provider not in runtime_status:
        return "RUNTIME_UNAVAILABLE"
    if provider == "openbb_cboe" and not getattr(runtime, "openbb_cboe_enabled", False):
        return "RUNTIME_UNAVAILABLE"
    return "SAVED"


def _result(params, *, configuration="SAVED", reachability="NOT_TESTED", sample_access="NOT_TESTED",
            actual_coverage=None, reason_code=None):
    checked = datetime.now(timezone.utc)
    return {**params, "configuration": configuration, "reachability": reachability,
            "sample_access": sample_access, "actual_coverage": actual_coverage,
            "checked_at": checked.isoformat(),
            "expires_at": (checked + timedelta(seconds=CHECK_RESULT_SECONDS)).isoformat(),
            "reason_code": reason_code}


def _yahoo_sample(gateway, context, params):
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=CHECK_WINDOW_DAYS - 1)
    request = {"ticker": "SPY", "provider": "yfinance", "start_date": start.isoformat(),
               "end_date": end.isoformat(), "history_policy": "refresh"}
    try:
        view = gateway.obtain(context, request, whole_deadline=time.monotonic() + CHECK_SECONDS)
    except PlatformError as error:
        if error.code in {"LEASE_LOST", "JOB_CANCELLED", "FORBIDDEN"}:
            raise
        if error.code == "HISTORY_WINDOW_NO_OBSERVED_ROWS":
            return _result(params, reachability="REACHED", sample_access="NO_ROWS", reason_code=error.code)
        return _result(params, reachability="UNCONFIRMED", reason_code=error.code)
    dates = [row["session_date"] for row in view["rows"]]
    if (view["provider"] != "yfinance" or view["ticker"] != "SPY" or view["price_policy"] != YAHOO_PRICE_POLICY
            or view["history_cache"]["action"] != "DOWNLOADED_NEW_VINTAGE"
            or not 1 <= len(dates) <= CHECK_WINDOW_DAYS or dates != sorted(set(dates))
            or not all(request["start_date"] <= day <= request["end_date"] for day in dates)):
        return _result(params, reachability="UNCONFIRMED", reason_code="CONNECTION_CHECK_SAMPLE_INVALID")
    return _result(params, reachability="REACHED", sample_access="RETURNED_ROWS",
                   actual_coverage={"ticker": "SPY", "start": dates[0], "end": dates[-1],
                                    "rows": len(dates), "currency": "USD", "adjustment": "split_adjusted"})


def install_connection_checks(runtime, library, gateway_factory):
    db = runtime.database
    db.apply_module_migrations("data_source_onboarding", MIGRATIONS)

    def current(con, context, params):
        context.assert_fenced(con)
        owner = context.run.owner_id
        _verify_actor(con, owner)
        return read_connection(con, owner, params["connection_id"])

    def run(context, params):
        with db.transaction() as con:
            connection = current(con, context, params)
            if connection["archived"] or connection["revision"] != params["revision"]:
                return _result(params, configuration="ARCHIVED" if connection["archived"] else "SAVED",
                               reason_code="CONNECTION_CHECK_STALE")
            if connection["provider"] != params["provider"]:
                raise DataError("CONNECTION_CHECK_STALE")
            configuration = _configuration(runtime, context.run.owner_id, connection)
            if params["provider"] not in PUBLIC_CHECK_PROVIDERS:
                return _result(params, configuration=configuration,
                               reachability="NOT_APPLICABLE" if params["provider"] == "csv" else "NOT_TESTED",
                               reason_code="CONNECTION_CHECK_CONFIGURATION_ONLY")
            if connection["credential_ref"] is not None:
                raise DataError("PUBLIC_PROVIDER_CREDENTIAL_FORBIDDEN")
            now = time.time()
            last = con.execute("""
                SELECT g.reserved_at,j.status FROM data_connection_check_gate g
                JOIN jobs j ON j.job_id=g.job_id WHERE g.provider=?
            """, (params["provider"],)).fetchone()
            if last and (now < last[0] + CHECK_COOLDOWN or last[1] == "RUNNING"):
                return _result(params, reason_code="CONNECTION_CHECK_RATE_LIMITED")
            con.execute("""
                INSERT INTO data_connection_check_gate VALUES(?,?,?)
                ON CONFLICT(provider) DO UPDATE SET job_id=excluded.job_id,reserved_at=excluded.reserved_at
            """, (params["provider"], context.job_id, now))
        result = _yahoo_sample(gateway_factory(runtime), context, params)
        with db.transaction() as con:
            connection = current(con, context, params)
            if connection["archived"] or connection["revision"] != params["revision"]:
                return _result(params, configuration="ARCHIVED" if connection["archived"] else "SAVED",
                               reason_code="CONNECTION_CHECK_STALE")
        return result

    runtime.handlers.register(CHECK_JOB, run, validate=_job_parameters, internal_only=True,
                              recoverable=False, auto_recovery_limit=0)

    def submit(principal, ident, body):
        owner = principal.user_id
        require_owner(principal, owner, write=True)
        if isinstance(principal, AccountDelegation):
            raise PlatformError("MCP_LOCAL_DATA_ONLY", 403)
        if type(body) is not dict or set(body) != {"idempotency_key"}:
            raise DataError("INVALID_INPUT")
        key = body["idempotency_key"]
        identifier(key)
        identifier(ident)
        with closing(db.connect()) as con:
            _verify_actor(con, owner)
            old = con.execute("""
                SELECT j.job_id,j.handler,j.parameters_json FROM job_keys k JOIN jobs j ON j.job_id=k.job_id
                WHERE k.owner_id=? AND k.idempotency_key=?
            """, (owner, key)).fetchone()
            if old:
                if old[1] != CHECK_JOB or json.loads(old[2])["connection_id"] != ident:
                    raise PlatformError("CONFLICT", 409)
                return _job_view(runtime.jobs.get(principal, old[0]))
            connection = read_connection(con, owner, ident)
        if connection["archived"]:
            raise DataError("MARKET_CONNECTION_ARCHIVED")
        parameters = {field: connection[field] for field in ("connection_id", "provider", "revision")}
        return _job_view(runtime.jobs.submit_internal(principal, CHECK_JOB, parameters, idempotency_key=key))

    def read(principal, ident, job_id):
        owner = principal.user_id
        require_owner(principal, owner)
        if isinstance(principal, AccountDelegation):
            raise PlatformError("MCP_LOCAL_DATA_ONLY", 403)
        identifier(job_id)
        with closing(db.connect()) as con:
            con.execute("BEGIN")
            row = con.execute("""
                SELECT parameters_json FROM jobs WHERE owner_id=? AND job_id=? AND handler=?
            """, (owner, job_id, CHECK_JOB)).fetchone()
            if row is None:
                raise DataError("RESOURCE_NOT_FOUND")
            params = json.loads(row[0])
            if params["connection_id"] != ident:
                raise DataError("RESOURCE_NOT_FOUND")
            connection = read_connection(con, owner, ident)
            configuration = "ARCHIVED" if connection["archived"] else _configuration(runtime, owner, connection)
        job = runtime.jobs.get(principal, job_id)
        result = job["result"]
        stale = connection["archived"] or connection["revision"] != params["revision"]
        if result is not None:
            expired = time.time() >= datetime.fromisoformat(result["expires_at"]).timestamp()
            stale = stale or expired or result["configuration"] != configuration
            if stale:
                result = {**result, "configuration": configuration, "reachability": "NOT_TESTED",
                          "sample_access": "NOT_TESTED", "actual_coverage": None,
                          "reason_code": "CONNECTION_CHECK_STALE"}
        return {**_job_view(job), "result": result, "error_code": job["error_code"], "stale": stale}

    runtime.connection_check_submit = submit
    runtime.connection_check_read = read

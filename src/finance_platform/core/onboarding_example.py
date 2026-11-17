"""Resume the first example through the existing market and portfolio queues."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Request
from pydantic import Field

from finance_platform.operations.launch import _process_lock
from finance_platform.operations.paths import contained
from .auth import AccountDelegation
from .errors import PlatformError
from .mcp_data_policy import require_dataset
from .source_binding import checked_regular_file
from .team_profiles import Strict


EXAMPLE_ID = "yahoo-buy-hold-v1"
EXAMPLE = {
    "id": EXAMPLE_ID, "symbol": "AAPL", "start_date": "2025-01-02", "end_date": "2025-03-31",
    "initial_cash": "10000.00", "fee_per_share": "0.005", "minimum_fee": "1.00",
    "spread_bps": "0", "currency": "USD",
}
HANDLERS = {"download": "data.library-download", "backtest": "research.dataset_task"}
MIGRATIONS = ("""
CREATE TABLE onboarding_first_example(
    owner_id TEXT NOT NULL REFERENCES users(user_id),
    example_id TEXT NOT NULL,
    intent_id TEXT NOT NULL UNIQUE,
    phase TEXT NOT NULL CHECK(phase IN ('download','backtest')),
    revision INTEGER NOT NULL,
    download_attempt INTEGER NOT NULL,
    backtest_attempt INTEGER NOT NULL,
    suspended INTEGER NOT NULL CHECK(suspended IN (0,1)),
    connection_id TEXT,
    dataset_reference TEXT,
    submission_error TEXT,
    PRIMARY KEY(owner_id,example_id)
);
""",)


class ExampleStart(Strict):
    example_id: Literal["yahoo-buy-hold-v1"]


class ExampleAction(Strict):
    action: Literal["advance", "retry", "cancel"]
    expected_revision: int = Field(ge=0)


class FirstExample:
    def __init__(self, runtime):
        self.runtime = runtime
        self.database = runtime.database
        self.library = runtime.dataset_library
        self.research = runtime.research_task_service
        self.database.apply_module_migrations("onboarding_first_example", MIGRATIONS)

    @contextmanager
    def _locked(self):
        # Queue submission takes the normal SQLite writer lock. Hold a
        # separate process lock so cancel and submit order across requests.
        root = Path(self.runtime.root)
        relative = ".local/platform/onboarding-first-example.lock"
        path = contained(root, root / relative)
        if path.exists():
            checked_regular_file(root, relative)
        with _process_lock(path):
            yield

    def _find(self, principal):
        with closing(self.database.connect()) as con:
            self._approval(principal, con)
            return con.execute("SELECT * FROM onboarding_first_example WHERE owner_id=? AND example_id=?",
                               (principal.user_id, EXAMPLE_ID)).fetchone()

    @staticmethod
    def _approval(principal, con):
        if isinstance(principal, AccountDelegation):
            principal.check_approval(con)

    @staticmethod
    def _key(intent, phase):
        return "onboarding-" + intent["intent_id"] + "-" + phase + "-" + str(intent[phase + "_attempt"])

    def _job(self, principal, intent, phase):
        with closing(self.database.connect()) as con:
            row = con.execute("""
                SELECT j.job_id,j.run_id,j.status,j.cancel_requested,j.error_code,j.result_json
                FROM job_keys k JOIN jobs j USING(job_id)
                WHERE k.owner_id=? AND j.owner_id=? AND k.idempotency_key=? AND j.handler=?
            """, (principal.user_id, principal.user_id, self._key(intent, phase), HANDLERS[phase])).fetchone()
        return row

    def _download_ref(self, principal, job):
        ident = json.loads(job["result_json"])["dataset_id"]
        owner = principal.user_id
        dataset = self.library.get(owner, ident, selectable=True)
        require_dataset(dataset)
        if (dataset["dataset_id"] != ident or dataset.get("origin_job_id") != job["job_id"]
                or dataset["kind"] != "dataset" or dataset["scope"] != "public"
                or dataset["source"]["provider"] != "yfinance" or dataset["universe"]["tickers"] != [EXAMPLE["symbol"]]
                or (dataset["start"], dataset["end"]) != (EXAMPLE["start_date"], EXAMPLE["end_date"])):
            raise PlatformError("DATASET_REFERENCE_MISMATCH", 409)
        return {field: dataset[field] for field in ("dataset_id", "version", "content_hash")}

    def _project(self, principal, row):
        if row is None:
            return {"example": EXAMPLE, "intent": None}
        download = self._job(principal, row, "download")
        backtest = self._job(principal, row, "backtest") if row["phase"] == "backtest" else None
        phase = row["phase"]
        job = download if phase == "download" else backtest
        job_status = None if job is None else job["status"]
        error = None if job is None else job["error_code"]
        ref = json.loads(row["dataset_reference"]) if row["dataset_reference"] else None
        result_id = None
        if row["suspended"]:
            status, next_action = "cancelled", "retry"
        elif job is None and row["submission_error"]:
            status, next_action, error = "failed", "retry", row["submission_error"]
        elif job is None:
            status, next_action = "pending", "advance"
        elif job_status in {"FAILED", "CANCELLED"}:
            status, next_action = job_status.lower(), "retry"
        elif job_status != "SUCCEEDED":
            status, next_action = job_status.lower(), None
        elif phase == "download":
            try:
                ref = self._download_ref(principal, download)
            except (KeyError, TypeError, PlatformError):
                status, next_action, error = "failed", None, "ONBOARDING_DATASET_UNAVAILABLE"
            else:
                status, next_action = "needs_advance", "advance"
        else:
            status, next_action = "failed", None
            error = "ONBOARDING_REPORT_UNAVAILABLE"
            try:
                report = self.research.result(principal, job["run_id"])
                if isinstance(principal, AccountDelegation):
                    self.research.require_mcp_report(principal, job["run_id"], report)
            except PlatformError as exc:
                if exc.status != 404:
                    raise
            else:
                if report.get("family") == "dataset-portfolio-result-1":
                    result_id = "tasks:" + job["run_id"]
                    phase, status, next_action, error = "result", "succeeded", "open_report", None
        return {"example": EXAMPLE, "intent": {
            "id": row["intent_id"], "revision": row["revision"], "phase": phase, "status": status,
            "cancel_requested": bool(job and job["cancel_requested"]), "job_status": job_status,
            "download_job_id": download["job_id"] if download else None,
            "backtest_job_id": backtest["job_id"] if backtest else None,
            "dataset_reference": ref, "result_id": result_id, "error_code": error, "next_action": next_action,
        }}

    def read(self, principal):
        return self._project(principal, self._find(principal))

    def _replace(self, principal, row, *, phase, suspended=False, retry=False, reference=None):
        download_attempt = row["download_attempt"] + int(retry and phase == "download")
        backtest_attempt = row["backtest_attempt"] + int(retry and phase == "backtest")
        binding = row["dataset_reference"] if reference is None else json.dumps(reference, sort_keys=True)
        with self.database.transaction() as con:
            self._approval(principal, con)
            con.execute("""
                UPDATE onboarding_first_example SET phase=?, revision=revision+1, download_attempt=?,
                       backtest_attempt=?, suspended=?, dataset_reference=?, submission_error=NULL,
                       connection_id=CASE WHEN ? THEN NULL ELSE connection_id END
                WHERE owner_id=? AND intent_id=?
            """, (phase, download_attempt, backtest_attempt, suspended, binding, retry and phase == "download",
                  principal.user_id, row["intent_id"]))
        return self._find(principal)

    def _connection(self, principal, row):
        if row["connection_id"]:
            return row["connection_id"]
        from finance_platform.data.source_onboarding import ensure_yahoo_connection
        existing = ensure_yahoo_connection(self.library, principal, self._key(row, "download") + "-connection")
        if existing["provider"] != "yfinance":
            raise PlatformError("ONBOARDING_CONNECTION_UNAVAILABLE", 503)
        ident = existing["connection_id"]
        with self.database.transaction() as con:
            self._approval(principal, con)
            con.execute("UPDATE onboarding_first_example SET connection_id=? WHERE owner_id=? AND intent_id=?",
                        (ident, principal.user_id, row["intent_id"]))
        return ident

    def _submit(self, principal, row):
        phase = row["phase"]
        key = self._key(row, phase)
        if phase == "download":
            self.runtime.dataset_download_submit(principal, {"idempotency_key": key, "request": {
                "connection_id": self._connection(principal, row), "name": "首次示例 · AAPL",
                "universe": self.library.resolve_universe(principal.user_id, {"manual": EXAMPLE["symbol"]}),
                "start": EXAMPLE["start_date"], "end": EXAMPLE["end_date"], "metadata": None,
                "kind": "dataset", "benchmark": None,
            }})
        else:
            ref = json.loads(row["dataset_reference"])
            value = self.library.get(principal.user_id, ref["dataset_id"], selectable=True)
            require_dataset(value)
            if any(value[field] != expected for field, expected in ref.items()):
                raise PlatformError("DATASET_REFERENCE_MISMATCH", 409)
            terms = ("start_date", "end_date", "initial_cash", "fee_per_share", "minimum_fee", "spread_bps")
            self.research.submit_portfolio(principal, {
                **{field: EXAMPLE[field] for field in terms},
                "dataset_id": ref["dataset_id"], "strategy": "buy_hold", "idempotency_key": key,
            })

    def _queue(self, principal, row):
        try:
            self._submit(principal, row)
        except PlatformError as error:
            if error.status in {401, 403} or self._job(principal, row, row["phase"]) is not None:
                raise
            with self.database.transaction() as con:
                self._approval(principal, con)
                con.execute("UPDATE onboarding_first_example SET submission_error=?,revision=revision+1 "
                            "WHERE owner_id=? AND intent_id=?",
                            (error.code, principal.user_id, row["intent_id"]))

    def start(self, principal):
        self.runtime.require_owner(principal, principal.user_id, write=True)
        with self._locked():
            with self.database.transaction() as con:
                self._approval(principal, con)
                con.execute("""
                    INSERT INTO onboarding_first_example VALUES (?, ?, ?, 'download', 0, 0, 0, 0, NULL, NULL, NULL)
                    ON CONFLICT(owner_id, example_id) DO NOTHING
                """, (principal.user_id, EXAMPLE_ID, uuid4().hex))
            row = self._find(principal)
            value = self._project(principal, row)
            if value["intent"]["status"] == "pending":
                self._queue(principal, row)
            return self.read(principal)

    def act(self, principal, ident, command):
        self.runtime.require_owner(principal, principal.user_id, write=True)
        with self._locked():
            row = self._find(principal)
            if row is None or row["intent_id"] != ident:
                raise PlatformError("NOT_FOUND", 404)
            value = self._project(principal, row)
            state = value["intent"]
            if command.expected_revision != row["revision"]:
                return value
            phase = row["phase"]
            if command.action == "cancel":
                self._cancel(principal, row, state)
            elif command.action == "advance" and state["next_action"] == "advance":
                if state["status"] == "needs_advance":
                    row = self._replace(principal, row, phase="backtest", reference=state["dataset_reference"])
                self._queue(principal, row)
            elif command.action == "retry" and state["next_action"] == "retry":
                if row["suspended"] and state["job_status"] == "SUCCEEDED":
                    row = self._replace(principal, row, phase="backtest",
                                        reference=self._download_ref(principal, self._job(principal, row, "download")))
                else:
                    row = self._replace(principal, row, phase=phase, retry=True)
                self._queue(principal, row)
            return self.read(principal)

    def _cancel(self, principal, row, state):
        phase = row["phase"]
        job_id = state[phase + "_job_id"]
        if state["job_status"] in {"QUEUED", "RUNNING"}:
            try:
                self.runtime.jobs.cancel(principal, job_id)
                return
            except PlatformError as error:
                if error.status != 409:
                    raise
            state = self.read(principal)["intent"]
        if state["status"] in {"pending", "needs_advance"}:
            self._replace(principal, row, phase=phase, suspended=True)


def create_router(runtime):
    router = APIRouter(prefix="/onboarding/first-example", tags=["onboarding"])
    service = FirstExample(runtime)

    @router.get("")
    def read(request: Request):
        return service.read(runtime.require_principal(request))

    @router.post("")
    def start(body: ExampleStart, request: Request):
        return service.start(runtime.require_principal(request, write=True))

    @router.post("/{intent_id}/actions")
    def act(intent_id: str, body: ExampleAction, request: Request):
        return service.act(runtime.require_principal(request, write=True), intent_id, body)

    return router

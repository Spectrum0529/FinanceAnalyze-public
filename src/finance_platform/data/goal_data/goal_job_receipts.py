"""Bind internal data jobs and record uncertain provider effects across restarts."""
from contextlib import closing
from uuid import uuid4

from finance_platform.data.contracts import DataError

MIGRATIONS = ("""
CREATE TABLE goal_data_transport_receipts(
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    state TEXT NOT NULL CHECK(state IN ('MAY_HAVE_SENT', 'NOT_SENT', 'RESPONSE_RECEIVED'))
);
CREATE INDEX goal_data_transport_job_state ON goal_data_transport_receipts(job_id,state);
""",)


class GoalJobReceipts:
    def __init__(self, db):
        self.db = db
        db.apply_module_migrations("goal_data_jobs", MIGRATIONS)

    def begin(self, job_id, guard):
        attempt = str(uuid4())
        with self.db.transaction() as con:
            guard(con)
            con.execute("INSERT INTO goal_data_transport_receipts VALUES (?,?,'MAY_HAVE_SENT')", (attempt, job_id))
        return attempt

    def finish(self, job_id, attempt, state):
        if state not in {"NOT_SENT", "RESPONSE_RECEIVED"}:
            raise ValueError("Invalid transport receipt")
        with self.db.transaction() as con:
            changed = con.execute("UPDATE goal_data_transport_receipts SET state=? "
                                  "WHERE job_id=? AND attempt_id=? AND state='MAY_HAVE_SENT'",
                                  (state, job_id, attempt)).rowcount
            if changed != 1:
                raise DataError("GOAL_DATA_TRANSPORT_RECEIPT_INVALID")

    def uncertain(self, job_id):
        with closing(self.db.connect()) as con:
            return con.execute("SELECT 1 FROM goal_data_transport_receipts WHERE job_id=? AND state='MAY_HAVE_SENT'",
                               (job_id,)).fetchone() is not None

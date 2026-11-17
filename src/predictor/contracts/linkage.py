"""Development-only append-only interface journal with rejection alerts.

No prospective store, real publication, independent timestamp authority or
tamper-proof service is implemented here.
"""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from predictor.research.append_guards import install as install_identity_guards

from predictor.access import AccessError, AccessPolicy
from predictor.contracts.forecast import Forecast, LinkageError, canonical, text, timestamp
from predictor.contracts.label import Evaluation, Label
from predictor.logging import utc_timestamp
from predictor.secrets import redact
from predictor.time.calendar import load_calendar, utc_text


class RecordJournal:
    def __init__(self, root: Path, database="data/development/record_linkage.sqlite3", *, calendar=None):
        self.root = root.resolve()
        self.policy = AccessPolicy(self.root)
        self.database = (self.root / database).resolve()
        # Do not create a record journal in raw/holdout/prospective/public paths.
        self.policy.authorize(self.database, operation="configuration")
        if not self.database.is_relative_to(self.policy.partitions["development"]):
            raise AccessError("Record journal is restricted to the development partition")
        self.calendar = calendar or load_calendar(self.root)[0]
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS forecasts (forecast_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS labels (label_id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES forecasts(forecast_id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluations (evaluation_id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES forecasts(forecast_id),
                    label_id TEXT NOT NULL REFERENCES labels(label_id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS invalidations (event_id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES forecasts(forecast_id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts (alert_id INTEGER PRIMARY KEY, recorded_at_utc TEXT NOT NULL, code TEXT NOT NULL, reference_id TEXT);
            """)
            for table in ("forecasts", "labels", "evaluations", "invalidations", "alerts"):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table}
                        BEGIN SELECT RAISE(ABORT, 'Append-only record journal'); END""")
            connection.execute('BEGIN IMMEDIATE')
            try:
                install_identity_guards(connection, ('forecasts','labels','evaluations','invalidations','alerts'))
                connection.execute('COMMIT')
            except BaseException:
                connection.execute('ROLLBACK')
                raise

    @contextmanager
    def connection(self):
        self.policy.authorize(self.database, operation="configuration")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, isolation_level=None, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=DELETE")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def alert(self, code, reference):
        reference = redact(reference) if isinstance(reference, str) else None
        with self.transaction() as connection:
            connection.execute("INSERT INTO alerts(recorded_at_utc,code,reference_id) VALUES (?,?,?)", (utc_timestamp(), code, reference))

    def _forecast(self, connection, forecast_id):
        row = connection.execute("SELECT payload FROM forecasts WHERE forecast_id=?", (forecast_id,)).fetchone()
        if not row:
            raise LinkageError("Unknown forecast link")
        return Forecast.from_mapping(json.loads(row[0]), self.calendar)

    def append_forecast(self, value):
        try:
            forecast = Forecast.from_mapping(value, self.calendar)
            with self.transaction() as connection:
                connection.execute("INSERT INTO forecasts VALUES (?,?)", (forecast.record()["forecast_id"], forecast.payload))
            return forecast
        except (ValueError, sqlite3.IntegrityError) as exc:
            self.alert("FORECAST_REJECTED", value.get("forecast_id") if isinstance(value, dict) else None)
            raise LinkageError("Forecast rejected; original retained and alert appended") from exc

    def append_label(self, value):
        try:
            if not isinstance(value, dict) or not isinstance(value.get("forecast_id"), str):
                raise LinkageError("Malformed label link")
            with self.transaction() as connection:
                forecast = self._forecast(connection, value["forecast_id"])
                label = Label.from_mapping(value, forecast, self.calendar)
                row = connection.execute("SELECT label_id,payload FROM labels WHERE forecast_id=? ORDER BY rowid DESC LIMIT 1", (value["forecast_id"],)).fetchone()
                if label.record()["previous_label_id"] != (row[0] if row else None):
                    raise LinkageError("Label correction must append to the existing chain head")
                if row and timestamp(label.record()["recorded_at"]) < timestamp(json.loads(row[1])["recorded_at"]):
                    raise LinkageError("Label correction cannot backdate its recording time")
                connection.execute("INSERT INTO labels VALUES (?,?,?)", (label.record()["label_id"], value["forecast_id"], label.payload))
            return label
        except (ValueError, sqlite3.IntegrityError) as exc:
            self.alert("LABEL_REJECTED", value.get("forecast_id") if isinstance(value, dict) else None)
            raise LinkageError("Label rejected; previous records retained and alert appended") from exc

    def append_evaluation(self, value):
        try:
            if not isinstance(value, dict) or not isinstance(value.get("forecast_id"), str) or not isinstance(value.get("label_id"), str):
                raise LinkageError("Malformed evaluation link")
            with self.transaction() as connection:
                forecast = self._forecast(connection, value["forecast_id"])
                row = connection.execute("SELECT payload FROM labels WHERE label_id=? AND forecast_id=?", (value["label_id"], value["forecast_id"])).fetchone()
                if not row:
                    raise LinkageError("Unknown label link")
                label = Label.from_mapping(json.loads(row[0]), forecast, self.calendar)
                evaluation = Evaluation.from_mapping(value, forecast, label)
                connection.execute("INSERT INTO evaluations VALUES (?,?,?,?)", (evaluation.record()["evaluation_id"], value["forecast_id"], value["label_id"], evaluation.payload))
            return evaluation
        except (ValueError, sqlite3.IntegrityError) as exc:
            self.alert("EVALUATION_REJECTED", value.get("forecast_id") if isinstance(value, dict) else None)
            raise LinkageError("Evaluation rejected; previous records retained and alert appended") from exc

    def invalidate(self, value):
        try:
            if not isinstance(value, dict) or set(value) != {"event_id", "forecast_id", "forecast_hash", "observed_at", "reason"}:
                raise LinkageError("Malformed invalidation event")
            value = json.loads(canonical(value))
            text(value["event_id"], "event_id")
            text(value["forecast_id"], "forecast_id")
            text(value["reason"], "reason")
            with self.transaction() as connection:
                forecast = self._forecast(connection, value["forecast_id"])
                if value["forecast_hash"] != forecast.fingerprint or timestamp(value["observed_at"]) < timestamp(forecast.record()["published_at"]):
                    raise LinkageError("Invalidation does not follow/link publication")
                value["observed_at"] = utc_text(timestamp(value["observed_at"]))
                connection.execute("INSERT INTO invalidations VALUES (?,?,?)", (value["event_id"], value["forecast_id"], canonical(value)))
        except (ValueError, sqlite3.IntegrityError) as exc:
            self.alert("INVALIDATION_REJECTED", value.get("forecast_id") if isinstance(value, dict) else None)
            raise LinkageError("Invalidation rejected and alert appended") from exc

    def mature_label(self, forecast_id, *, at_time):
        instant = timestamp(at_time)
        with self.connection() as connection:
            forecast = self._forecast(connection, forecast_id)
            rows = connection.execute("SELECT payload FROM labels WHERE forecast_id=? ORDER BY rowid DESC", (forecast_id,)).fetchall()
            for row in rows:
                label = Label.from_mapping(json.loads(row[0]), forecast, self.calendar)
                if timestamp(label.record()["recorded_at"]) <= instant:
                    label.require_mature(instant)
                    return label
        raise LinkageError("No recorded mature label at the requested fit/evaluation time")

    def snapshot(self, forecast_id):
        with self.connection() as connection:
            forecast = self._forecast(connection, forecast_id)
            result = {"forecast": forecast.record(), "forecast_hash": forecast.fingerprint}
            for table in ("labels", "evaluations", "invalidations"):
                result[table] = [json.loads(row[0]) for row in connection.execute(f"SELECT payload FROM {table} WHERE forecast_id=? ORDER BY rowid", (forecast_id,))]
            result["alerts"] = [{"recorded_at_utc": row[0], "code": row[1], "reference_id": row[2]} for row in connection.execute(
                "SELECT recorded_at_utc,code,reference_id FROM alerts WHERE reference_id=? ORDER BY alert_id", (forecast_id,))]
            result["current_status"] = "RETRACTED_AFTER_PUBLICATION" if result["invalidations"] else forecast.record()["applicability_status"]
            return result

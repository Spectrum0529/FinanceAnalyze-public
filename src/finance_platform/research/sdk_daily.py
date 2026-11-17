"""Bounded saved-day SDK execution through the existing trusted strategy worker."""
from contextlib import closing
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import json
from time import monotonic
from zoneinfo import ZoneInfo

from finance_platform.core.contracts import DataScope, OutputKind, RunMode, StrategyContext, identifier
from finance_platform.strategies.contracts import Manifest, StrategyError, digest
from finance_platform.strategies.worker import TrustedWorker, WorkerLimits

from .portfolio import fail


MAX_SESSIONS = 256
WALL_SECONDS = 100
HISTORY_ROWS = 64
FIELDS = {"open", "high", "low", "close", "volume"}
OUTPUTS = {"event": OutputKind.SIGNAL, "target": OutputKind.TARGET_QUANTITY}
CAPABILITIES = {
    "frequency": "1d",
    "decision": "OBSERVED_CLOSE_TO_NEXT_OBSERVED_OPEN",
    "styles": {"event": ["signal"], "target": ["target_quantity"]},
    "position_policy": "ONE_LONG_POSITION_OR_CASH_NO_IN_PLACE_RESIZING",
    "unsupported": ["target_weight", "score", "order_intent", "model", "external", "partial_rebalance"],
    "max_securities": 1,
    "max_sessions": MAX_SESSIONS,
    "history_rows": HISTORY_ROWS,
    "max_wall_seconds": WALL_SECONDS,
}


def binding(service, owner, instance_id, *, connection=None, verify_package=True):
    try:
        identifier(instance_id)
    except ValueError:
        fail("SDK_DAILY_INSTANCE_UNAVAILABLE")
    if connection is None:
        with closing(service.database.connect()) as con:
            return binding(service, owner, instance_id, connection=con, verify_package=verify_package)
    row = connection.execute("""
        SELECT i.status,i.active_run_id,i.updated_at,c.config_id,c.config_hash,c.mode,c.parameters_json,
               c.strategy_id,c.version,c.package_hash,v.trusted,v.manifest_json
          FROM strategy_instances i
          JOIN strategy_configs c ON c.config_id=i.config_id AND c.owner_id=i.owner_id
          JOIN strategy_runtime_versions v ON v.owner_id=c.owner_id AND v.strategy_id=c.strategy_id
               AND v.version=c.version AND v.package_hash=c.package_hash
          JOIN users u ON u.user_id=i.owner_id AND u.enabled=1 AND u.role!='viewer'
         WHERE i.owner_id=? AND i.instance_id=?
    """, (owner, instance_id)).fetchone()
    if not row or row["status"] != "VALIDATED" or row["active_run_id"] is not None:
        fail("SDK_DAILY_INSTANCE_UNAVAILABLE")
    if not row["trusted"]:
        fail("UNTRUSTED_PLUGIN_EXECUTION_DISABLED")
    try:
        raw = json.loads(row["manifest_json"])
        manifest = Manifest.parse(raw)
        params = manifest.parameters(json.loads(row["parameters_json"]))
        if verify_package and service.packages.verify(row["package_hash"]).raw != raw:
            fail("SDK_DAILY_PACKAGE_BINDING_MISMATCH")
    except (TypeError, StrategyError, json.JSONDecodeError):
        fail("SDK_DAILY_PACKAGE_BINDING_MISMATCH")
    if digest({"package_hash": row["package_hash"], "parameters": params, "mode": row["mode"]}) != row["config_hash"]:
        fail("SDK_DAILY_CONFIG_BINDING_MISMATCH")
    style = raw["style"]
    declared = OUTPUTS.get(style)
    if (row["mode"] != "BACKTEST" or "BACKTEST" not in raw["modes"] or raw["model"] is not None
            or "1d" not in raw["data"]["frequencies"] or not set(raw["data"]["fields"]) <= FIELDS
            or declared not in raw["outputs"]):
        fail("SDK_DAILY_CAPABILITY_UNSUPPORTED")
    if style == "target" and "kind" in params and params["kind"] != "target_quantity":
        fail("SDK_DAILY_CAPABILITY_UNSUPPORTED")
    if style == "target" and "target" in params:
        try:
            size = Decimal(params["target"])
            if not size.is_finite() or size != size.to_integral_value() or not 0 <= size <= 10**9:
                fail("SDK_DAILY_TARGET_QUANTITY_UNSUPPORTED")
        except (TypeError, InvalidOperation):
            fail("SDK_DAILY_TARGET_QUANTITY_UNSUPPORTED")
    reference = {k: row[k] for k in ("config_id", "config_hash", "strategy_id", "version",
                                    "package_hash", "mode", "status", "updated_at")}
    reference.update(owner_id=owner, instance_id=instance_id, trusted=True, style=style)
    return reference, params, raw


def catalog(service, owner):
    with closing(service.database.connect()) as con:
        rows = con.execute("""
            SELECT instance_id FROM strategy_instances WHERE owner_id=? AND status='VALIDATED'
            ORDER BY created_at DESC,instance_id LIMIT 100
        """, (owner,)).fetchall()
    from .portfolio import PortfolioError
    items = []
    for row in rows:
        try:
            ref, _, _ = binding(service, owner, row["instance_id"])
        except PortfolioError:
            continue
        items.append({k: ref[k] for k in ("instance_id", "strategy_id", "version", "style")} |
                     {"label": ref["strategy_id"] + " · v" + ref["version"]})
    return items


class DailyDecisions:
    """Present one completed bar plus a bounded causal history to each SDK call."""

    def __init__(self, service, reference, dataset, ticker, active):
        self.reference = reference
        self.service = service
        self.active = active
        self.deadline = monotonic() + WALL_SECONDS
        self.worker = TrustedWorker(service.packages, WorkerLimits(seconds=3), active_guard=self.check)
        self.reference, self.parameters, manifest = self.checked_binding(reference)
        self.timezone = ZoneInfo(dataset["metadata"]["timezone"])
        # The SDK v1 asset contract has only US equity/ETF. Synthetic fixtures
        # may omit a provider identity and retain their separate scope.
        security = next((item.get("instrument", {}) for item in (dataset.get("security_metadata") or [])
                         if item.get("ticker") == ticker), {})
        asset = security.get("asset_class") or ("US_EQUITY" if dataset["scope"] == "synthetic" else None)
        if asset not in manifest["assets"] or asset not in ("US_EQUITY", "US_ETF"):
            fail("SDK_DAILY_ASSET_UNSUPPORTED")
        self.instrument = security.get("instrument_id", ticker)
        self.scope = DataScope.SYNTHETIC if dataset["scope"] == "synthetic" else DataScope.DEVELOPMENT
        self.fields = set(manifest["data"]["fields"])
        self.state = {}
        self.history = []

    def checked_binding(self, ref):
        actual = binding(self.service, ref["owner_id"], ref["instance_id"])
        if actual[0] != ref:
            fail("SDK_DAILY_STRATEGY_BINDING_MISMATCH")
        return actual

    def check(self):
        self.active()
        if monotonic() >= self.deadline:
            fail("SDK_DAILY_EXECUTION_BUDGET_EXCEEDED")
        actual, _, _ = binding(self.service, self.reference["owner_id"], self.reference["instance_id"],
                                verify_package=False)
        if actual != self.reference:
            fail("SDK_DAILY_STRATEGY_BINDING_MISMATCH")

    def __call__(self, bar, quantity, equity):
        self.check()
        clock = datetime.combine(date.fromisoformat(bar.session_date), time(16), self.timezone).astimezone(timezone.utc)
        row = {"instrument_id": self.instrument, "session_date": bar.session_date, "available_at": clock.isoformat()}
        row.update({name: None if getattr(bar, name) is None else str(getattr(bar, name)) for name in self.fields})
        if any(row[k] is None for k in self.fields):
            fail("SDK_DAILY_DECLARED_FIELD_UNAVAILABLE")
        domain = StrategyContext(clock, RunMode.BACKTEST, self.parameters,
                                 {"rows": [row], "history": self.history},
                                 {"position_quantity": str(quantity), "equity": str(equity)},
                                 self.state, (self.instrument,), self.scope)
        outputs, self.state = self.worker.execute(self.reference["package_hash"], domain, trusted=True)
        self.history = [*self.history[-(HISTORY_ROWS-1):], row]
        if not outputs:
            return None, None
        if len(outputs) != 1 or outputs[0].kind != OUTPUTS[self.reference["style"]]:
            fail("SDK_DAILY_OUTPUT_UNSUPPORTED")
        out = outputs[0]
        if out.kind == OutputKind.SIGNAL:
            if out.value not in (0, 1):
                fail("SDK_DAILY_SIGNAL_UNSUPPORTED")
            return bool(out.value), out
        if out.value != out.value.to_integral_value() or not 0 <= out.value <= 10**9:
            fail("SDK_DAILY_TARGET_QUANTITY_UNSUPPORTED")
        if quantity and out.value not in (0, quantity):
            fail("SDK_DAILY_PARTIAL_REBALANCE_UNSUPPORTED")
        return int(out.value), out

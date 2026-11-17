"""Small development contract for hash-bound records and causal revision selection.

This is an in-memory contract adapter, not a vendor store or market feature engine.
Only admitted records enter the snapshot hash; unavailable future rows cannot
change an earlier input fingerprint. RawRecord supplies availability semantics.
"""
from dataclasses import dataclass
from datetime import date
import hashlib
import json

from predictor.contracts.forecast import canonical, timestamp
from predictor.contracts.protocol import SOURCE_HASH
from predictor.contracts.records import RawRecord, eligible_at
from predictor.time.calendar import utc_text


class SnapshotError(ValueError):
    pass


def content_hash(payload):
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    payload: str

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload.encode("utf-8")).hexdigest()

    def record(self):
        return json.loads(self.payload)


class DevelopmentRecords:
    """Append validated metadata/payload pairs. No raw/protected filesystem reads."""
    def __init__(self, calendar):
        self.calendar = calendar
        self._records = {}
        self._identities = {}
        self._alerts = []

    @property
    def alerts(self):
        return tuple(self._alerts)

    def append(self, envelope):
        if not isinstance(envelope, dict) or set(envelope) != {"metadata", "payload"} or not isinstance(envelope["payload"], dict):
            raise SnapshotError("Record envelope requires metadata and a JSON payload object")
        raw = RawRecord.from_mapping(envelope["metadata"])
        body = canonical(envelope["payload"])
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != raw.content_hash:
            self._alerts.append("CONTENT_HASH_MISMATCH")
            raise SnapshotError("Record payload content hash mismatch")
        key = (raw.record_id, raw.version)
        if key in self._records:
            self._alerts.append("DUPLICATE_RECORD_VERSION")
            raise SnapshotError("Duplicate record/version rejected; original retained")
        identity = (raw.security_id, raw.source_id)
        if raw.record_id in self._identities and self._identities[raw.record_id] != identity:
            self._alerts.append("RECORD_IDENTITY_MISMATCH")
            raise SnapshotError("Revision must preserve security/source identity")
        self._identities[raw.record_id] = identity
        self._records[key] = (raw, body)

    def snapshot(self, *, cutoff, event_reference):
        cutoff = timestamp(cutoff)
        if type(event_reference) is not date:
            raise SnapshotError("Snapshot event reference must be an explicit date")
        chosen = {}
        for raw, body in self._records.values():
            result = eligible_at(raw, self.calendar, cutoff=cutoff, event_reference=event_reference)
            if result.status != "ADMISSIBLE":
                continue
            previous = chosen.get(raw.record_id)
            if previous is None or raw.version > previous[0].version:
                chosen[raw.record_id] = (raw, body)
        values = [{"metadata": raw.record(), "payload": json.loads(body)} for _, (raw, body) in sorted(chosen.items())]
        return Snapshot(canonical({"cutoff": utc_text(cutoff), "event_reference": event_reference.isoformat(),
                                   "calendar_hash": self.calendar.calendar_hash, "protocol_hash": SOURCE_HASH, "records": values}))

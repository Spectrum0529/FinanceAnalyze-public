"""Immutable raw-record metadata and causal availability contracts.

References and timestamps are declarations, not independent authenticity or
license certification. No missing ingestion time is inferred or backdated.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re

from predictor.contracts.protocol import parse_json
from predictor.secrets import reject_credentials
from predictor.time.calendar import CalendarError, ExchangeCalendar, pinned_zone, utc_text


RECORD_VERSION = "raw-availability-1"
TIME_FIELDS = ("event_time", "published_at", "first_available_at", "ingested_at", "processed_at",
               "revision_time", "valid_from", "valid_to")
REQUIRED_FIELDS = {"record_id", "security_id", "source_id", *TIME_FIELDS, "version", "content_hash",
                   "license_id", "timestamp_quality", "availability_basis", "availability_evidence"}


class RecordError(ValueError):
    """Malformed metadata; errors never echo record contents."""


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise RecordError(f"{name} must be a non-empty string")
    return value


def _digest(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RecordError(f"{name} must be lowercase SHA-256")
    return value


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RecordError("An explicit timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


def _temporal(value, quality, field):
    if value is None:
        allowed = {"missing"}
        if field == "valid_to":
            allowed.add("open_end")
        if field == "revision_time":
            allowed.add("not_revised")
        if quality not in allowed:
            raise RecordError(f"{field}: null requires an explicit missing/open-end/not-revised quality")
        return None
    if not isinstance(value, str):
        raise RecordError(f"{field}: temporal value must be an ISO string or null")
    if quality == "date_only":
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
        raise RecordError(f"{field}: date-only quality requires YYYY-MM-DD")
    if quality not in {"exact", "official_exact"}:
        raise RecordError(f"{field}: non-null timestamp requires exact/date-only quality")
    try:
        return _instant(datetime.fromisoformat(value))
    except ValueError:
        raise RecordError(f"{field}: exact timestamp must include a timezone") from None


@dataclass(frozen=True)
class EvidenceReference:
    reference_id: str
    sha256: str

    @classmethod
    def parse(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"reference_id", "sha256"}:
            raise RecordError("Evidence reference requires reference_id and sha256")
        return cls(_text(value["reference_id"], "reference_id"), _digest(value["sha256"], "reference.sha256"))


@dataclass(frozen=True)
class LatencyAssumption:
    seconds: float
    rationale: str
    reference: EvidenceReference

    @classmethod
    def parse(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"seconds", "rationale", "reference"}:
            raise RecordError("Latency assumption requires seconds, rationale and reference")
        seconds = value["seconds"]
        try:
            finite = type(seconds) in {int, float} and math.isfinite(seconds)
        except OverflowError:
            finite = False
        if not finite or seconds < 0 or seconds > timedelta.max.total_seconds():
            raise RecordError("Latency seconds must be finite nonnegative numeric seconds, never boolean")
        reference = EvidenceReference.parse(value["reference"])
        if reference is None:
            raise RecordError("Latency assumptions require a traceable declaration reference")
        return cls(seconds, _text(value["rationale"], "latency rationale"), reference)


@dataclass(frozen=True)
class AvailabilityEvidence:
    publication_reference: EvidenceReference | None
    vendor_reference: EvidenceReference | None
    latency_assumption: LatencyAssumption | None
    official_time_references: tuple[tuple[str, EvidenceReference], ...]

    @classmethod
    def parse(cls, value):
        keys = {"publication_reference", "vendor_reference", "latency_assumption", "official_time_references"}
        if not isinstance(value, dict) or set(value) != keys:
            raise RecordError("Availability evidence has missing or unknown fields")
        official = value["official_time_references"]
        if not isinstance(official, dict) or any(key not in TIME_FIELDS for key in official):
            raise RecordError("Official time references must name declared temporal fields")
        references = tuple((key, EvidenceReference.parse(ref)) for key, ref in sorted(official.items()))
        if any(ref is None for _, ref in references):
            raise RecordError("Official time references cannot be null")
        vendor = EvidenceReference.parse(value["vendor_reference"])
        latency = LatencyAssumption.parse(value["latency_assumption"])
        if vendor is not None and latency is not None:
            raise RecordError("Choose proven vendor availability or a declared latency assumption")
        return cls(EvidenceReference.parse(value["publication_reference"]), vendor, latency, references)


@dataclass(frozen=True)
class RawRecord:
    record_id: str
    security_id: str
    source_id: str
    event_time: datetime | date | None
    published_at: datetime | date | None
    first_available_at: datetime | date | None
    ingested_at: datetime | date | None
    processed_at: datetime | date | None
    revision_time: datetime | date | None
    valid_from: datetime | date | None
    valid_to: datetime | date | None
    version: int
    content_hash: str
    license_id: str | None
    timestamp_quality: tuple[tuple[str, str], ...]
    availability_basis: str
    availability_evidence: AvailabilityEvidence

    @classmethod
    def from_mapping(cls, value):
        reject_credentials(value)
        if not isinstance(value, dict) or set(value) != REQUIRED_FIELDS:
            raise RecordError("Raw record has missing or unknown fields")
        version = value["version"]
        if type(version) is not int or version < 1:
            raise RecordError("version must be a positive integer, never boolean")
        quality = value["timestamp_quality"]
        if not isinstance(quality, dict) or set(quality) != set(TIME_FIELDS):
            raise RecordError("timestamp_quality must cover every temporal field")
        if any(not isinstance(q, str) for q in quality.values()):
            raise RecordError("Timestamp quality entries must be strings")
        basis = value["availability_basis"]
        if not isinstance(basis, str) or basis not in {"live_observed", "historical_reconstruction"}:
            raise RecordError("availability_basis must be live_observed or historical_reconstruction")
        evidence = AvailabilityEvidence.parse(value["availability_evidence"])
        if basis == "live_observed" and evidence.latency_assumption is not None:
            raise RecordError("Historical latency assumptions cannot substitute for live ingestion")
        license_id = value["license_id"]
        if license_id is not None:
            _text(license_id, "license_id")
        times = {field: _temporal(value[field], quality[field], field) for field in TIME_FIELDS}
        if version == 1 and quality["revision_time"] == "not_revised" and times["revision_time"] is not None:
            raise RecordError("Not-revised quality contradicts revision timestamp")
        return cls(**{key: _text(value[key], key) for key in ("record_id", "security_id", "source_id")}, **times,
                   version=version, content_hash=_digest(value["content_hash"], "content_hash"), license_id=license_id,
                   timestamp_quality=tuple(sorted(quality.items())), availability_basis=basis, availability_evidence=evidence)

    def record(self):
        result = {key: getattr(self, key) for key in ("record_id", "security_id", "source_id", "version", "content_hash", "license_id", "availability_basis")}
        for field in TIME_FIELDS:
            value = getattr(self, field)
            result[field] = utc_text(value) if isinstance(value, datetime) else value.isoformat() if value is not None else None
        reference = lambda ref: {"reference_id": ref.reference_id, "sha256": ref.sha256} if ref else None
        evidence = self.availability_evidence
        latency = evidence.latency_assumption
        result["timestamp_quality"] = dict(self.timestamp_quality)
        result["availability_evidence"] = {
            "publication_reference": reference(evidence.publication_reference), "vendor_reference": reference(evidence.vendor_reference),
            "latency_assumption": {"seconds": latency.seconds, "rationale": latency.rationale, "reference": reference(latency.reference)} if latency else None,
            "official_time_references": {field: reference(ref) for field, ref in evidence.official_time_references},
        }
        return result


@dataclass(frozen=True)
class Availability:
    record_id: str
    basis: str
    status: str
    available_at: datetime | None
    reasons: tuple[str, ...]
    quality_flags: tuple[str, ...]
    uses_latency_assumption: bool
    calendar_hash: str

    def record(self):
        return {"record_id": self.record_id, "basis": self.basis, "status": self.status,
                "available_at": utc_text(self.available_at) if self.available_at else None, "reasons": list(self.reasons),
                "quality_flags": list(self.quality_flags), "uses_latency_assumption": self.uses_latency_assumption,
                "calendar_hash": self.calendar_hash, "contract_version": RECORD_VERSION}


def _validity_time(value):
    # Economic/event validity dates are boundaries, not information availability.
    return _instant(value) if isinstance(value, datetime) else datetime.combine(value, datetime.min.time(), pinned_zone("America/New_York")).astimezone(timezone.utc)


def availability(record: RawRecord, calendar: ExchangeCalendar) -> Availability:
    reasons, flags = [], []
    evidence = record.availability_evidence
    official = dict(evidence.official_time_references)
    quality = dict(record.timestamp_quality)
    for field in TIME_FIELDS:
        if quality[field] == "official_exact" and field not in official:
            reasons.append(f"unproven_official_time:{field}")
        if field in official and quality[field] != "official_exact":
            reasons.append(f"unused_official_time_reference:{field}")
    if record.event_time is None:
        reasons.append("missing_critical_time:event_time")
    if record.valid_from is None:
        reasons.append("missing_critical_time:valid_from")
    if record.valid_to is None and quality["valid_to"] != "open_end":
        reasons.append("unknown_validity_end")
    if record.license_id is None:
        reasons.append("missing_license_id")
    if record.version > 1 and record.revision_time is None:
        reasons.append("missing_critical_time:revision_time")
    if record.version == 1 and record.revision_time is None and quality["revision_time"] != "not_revised":
        reasons.append("unknown_revision_state")

    def normalize(field):
        value = getattr(record, field)
        if value is None:
            reasons.append(f"missing_critical_time:{field}")
            return None
        if isinstance(value, datetime):
            return value
        flags.append(f"date_only_next_session_cutoff:{field}")
        try:
            day = calendar.shift(value, 1) if calendar.is_session(value) else calendar.resolve(value, direction="next")
            return calendar.clock(day).cutoff_utc
        except CalendarError:
            reasons.append(f"date_only_outside_calendar:{field}")
            return None

    published = normalize("published_at")
    if record.availability_basis == "live_observed":
        values = [published, normalize("first_available_at"), normalize("ingested_at"), normalize("processed_at")]
    else:
        flags.append("historical_reconstruction_not_observed_ingestion")
        if evidence.publication_reference is None:
            reasons.append("unproven_historical_publication")
        if evidence.vendor_reference is not None:
            values = [published, normalize("first_available_at")]
        elif evidence.latency_assumption is not None:
            flags.append("declared_vendor_latency_assumption")
            if record.first_available_at is not None:
                reasons.append("unproven_vendor_time_with_latency_assumption")
            try:
                estimated = published + timedelta(seconds=evidence.latency_assumption.seconds) if published is not None else None
            except OverflowError:
                reasons.append("latency_assumption_outside_datetime_range")
                estimated = None
            values = [published, estimated]
        else:
            reasons.append("missing_historical_vendor_evidence_or_latency_assumption")
            values = [published, None]
    candidate = max(values) if all(value is not None for value in values) else None
    if record.revision_time is not None:
        revision = normalize("revision_time")
        if candidate is not None and revision is not None and revision > candidate:
            reasons.append("revision_later_than_declared_availability")
    # Processing cannot finish before ingestion. Dates represent intervals, so
    # only exact observed timestamps support this ordering check.
    if isinstance(record.ingested_at, datetime) and isinstance(record.processed_at, datetime):
        if record.processed_at < record.ingested_at:
            reasons.append("processing_before_ingestion")
    if record.valid_from is not None and record.valid_to is not None:
        if _validity_time(record.valid_from) >= _validity_time(record.valid_to):
            reasons.append("invalid_validity_interval")
    return Availability(record.record_id, record.availability_basis, "QUARANTINED" if reasons else "AVAILABLE",
                        None if reasons else candidate, tuple(dict.fromkeys(reasons)), tuple(flags),
                        evidence.latency_assumption is not None, calendar.calendar_hash)


def eligible_at(record: RawRecord, calendar: ExchangeCalendar, *, cutoff: datetime, event_reference: datetime | date) -> Availability:
    cutoff = _instant(cutoff)
    if not isinstance(event_reference, datetime) and type(event_reference) is not date:
        raise RecordError("Event reference must be an aware timestamp or a date")
    reference = _validity_time(event_reference)
    result = availability(record, calendar)
    if result.status == "QUARANTINED":
        return result
    status = "ADMISSIBLE"
    reasons = ()
    if result.available_at > cutoff:
        status, reasons = "NOT_YET_AVAILABLE", ("availability_after_cutoff",)
    elif reference < _validity_time(record.valid_from) or (record.valid_to is not None and reference >= _validity_time(record.valid_to)):
        status, reasons = "OUTSIDE_VALIDITY", ("event_reference_outside_half_open_validity",)
    return Availability(result.record_id, result.basis, status, result.available_at, reasons, result.quality_flags,
                        result.uses_latency_assumption, result.calendar_hash)


def parse_record(raw: bytes) -> RawRecord:
    return RawRecord.from_mapping(parse_json(raw))

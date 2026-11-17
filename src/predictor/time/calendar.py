"""XNYS regular equity sessions with pinned rules, UTC storage and local display.

This adapter is a current-rule calendar reconstruction, not evidence that a
future closure was known historically, and not a broker order permission gate.
"""

from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import hashlib
from importlib.resources import files
import io
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import tzdata

from predictor.access import AccessPolicy
from predictor.contracts.protocol import load_protocol, parse_json


ADAPTER_VERSION = "xnys-forecast-clock-1"
BACKEND_VERSION = "4.13.2"
TZDATA_VERSION = "2026.5"
DEFAULT_START = "2007-01-01"
DEFAULT_END = "2028-12-31"
UTC = timezone.utc


class CalendarError(ValueError):
    """Invalid session, clock, locked version or unsupported coverage."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _date(value):
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise CalendarError("Session date must be a date or YYYY-MM-DD; never a timestamp")


def _time(value):
    if not isinstance(value, str):
        raise CalendarError("Clock must be HH:MM:SS")
    try:
        parsed = time.fromisoformat(value)
        if parsed.tzinfo is None and parsed.isoformat() == value and not parsed.microsecond:
            return parsed
    except ValueError:
        pass
    raise CalendarError("Clock must be HH:MM:SS without timezone or fractional seconds")


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CalendarError("Timestamp must be timezone-aware")
    return value.astimezone(UTC)


def utc_text(value):
    return _aware(value).isoformat().replace("+00:00", "Z")


@lru_cache(maxsize=3)
def _zone_bytes(name):
    return files("tzdata.zoneinfo").joinpath(*name.split("/")).read_bytes()


@lru_cache(maxsize=3)
def pinned_zone(name):
    if name not in {"America/New_York", "Asia/Hong_Kong", "UTC"}:
        raise CalendarError("Unsupported clock timezone")
    if tzdata.__version__ != TZDATA_VERSION:
        raise CalendarError("Timezone package differs from the pinned version")
    return ZoneInfo.from_file(io.BytesIO(_zone_bytes(name)), key=name)


@dataclass(frozen=True)
class Session:
    day: date
    open_utc: datetime
    close_utc: datetime


@dataclass(frozen=True)
class SessionClock:
    session_date: date
    cutoff_utc: datetime
    publish_target_utc: datetime
    order_submit_target_utc: datetime
    open_utc: datetime
    close_utc: datetime
    early_close: bool
    calendar_hash: str

    def record(self):
        result = {"session_date": self.session_date.isoformat(), "early_close": self.early_close,
                  "calendar_hash": self.calendar_hash, "storage_timezone": "UTC", "display_timezone": "Asia/Hong_Kong"}
        for name in ("cutoff", "publish_target", "order_submit_target", "open", "close"):
            value = getattr(self, name + "_utc")
            result[name + "_utc"] = utc_text(value)
            result[name + "_hong_kong"] = value.astimezone(pinned_zone("Asia/Hong_Kong")).isoformat()
        return result


@lru_cache(maxsize=4)
def _sessions(start, end):
    if xcals.__version__ != BACKEND_VERSION:
        raise CalendarError("Calendar backend differs from the pinned version")
    calendar = xcals.get_calendar("XNYS", start=start.isoformat(), end=end.isoformat())
    return tuple(Session(label.date(), _aware(row["open"].to_pydatetime()), _aware(row["close"].to_pydatetime()))
                 for label, row in calendar.schedule.iterrows())


@lru_cache(maxsize=1)
def _backend_sources_hash():
    package = Path(xcals.__file__).resolve().parent
    return _hash([{ "path": p.relative_to(package).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in sorted(package.rglob("*.py"))])


class ExchangeCalendar:
    def __init__(self, start=DEFAULT_START, end=DEFAULT_END):
        self.start, self.end = _date(start), _date(end)
        if self.start >= self.end:
            raise CalendarError("Calendar coverage must have start before end")
        self.sessions = _sessions(self.start, self.end)
        if not self.sessions:
            raise CalendarError("Calendar coverage has no sessions")
        self.days = tuple(s.day for s in self.sessions)
        self.manifest = {
            "schema_version": 1, "adapter_version": ADAPTER_VERSION, "exchange": "XNYS",
            "provider": f"exchange_calendars:{BACKEND_VERSION}:XNYS",
            "coverage_start": self.start.isoformat(), "coverage_end": self.end.isoformat(),
            "backend_source_sha256": _backend_sources_hash(),
            "tzdata_version": TZDATA_VERSION, "iana_version": tzdata.IANA_VERSION,
            "timezone_sha256": {name: hashlib.sha256(_zone_bytes(name)).hexdigest()
                                for name in ("America/New_York", "Asia/Hong_Kong", "UTC")},
            "sessions_count": len(self.sessions),
            "schedule_sha256": _hash([[s.day.isoformat(), utc_text(s.open_utc), utc_text(s.close_utc)] for s in self.sessions]),
        }
        # Resolve all pinned zones before accepting a manifest.
        for name in self.manifest["timezone_sha256"]:
            pinned_zone(name)
        self.calendar_hash = _hash(self.manifest)

    def lock(self):
        return {**self.manifest, "calendar_hash": self.calendar_hash}

    def _within(self, day):
        day = _date(day)
        if not self.start <= day <= self.end:
            raise CalendarError("Date exceeds locked calendar coverage; a new lock is required")
        return day

    def is_session(self, day):
        day = self._within(day)
        index = bisect_left(self.days, day)
        return index < len(self.days) and self.days[index] == day

    def resolve(self, day, *, direction="none"):
        day = self._within(day)
        if direction not in {"none", "next", "previous"}:
            raise CalendarError("Session direction must be none, next or previous")
        index = bisect_left(self.days, day)
        if index < len(self.days) and self.days[index] == day:
            return day
        if direction == "none":
            raise CalendarError("Date is not a trading session; explicit resolution required")
        index = index if direction == "next" else index - 1
        if not 0 <= index < len(self.days):
            raise CalendarError("No admissible session within locked coverage")
        return self.days[index]

    def shift(self, session, count):
        if type(count) is not int:
            raise CalendarError("Session offset must be an integer, never boolean")
        day = self.resolve(session)
        index = bisect_left(self.days, day) + count
        if not 0 <= index < len(self.days):
            raise CalendarError("Session offset exceeds locked calendar coverage")
        return self.days[index]

    def exchange_instant(self, session, clock):
        day = self.resolve(session)
        return datetime.combine(day, _time(clock), pinned_zone("America/New_York")).astimezone(UTC)

    def clock(self, session):
        day = self.resolve(session)
        record = self.sessions[bisect_left(self.days, day)]
        return SessionClock(day, self.exchange_instant(day, "08:45:00"), self.exchange_instant(day, "09:00:00"),
                            self.exchange_instant(day, "09:20:00"), record.open_utc, record.close_utc,
                            record.close_utc.astimezone(pinned_zone("America/New_York")).time() < time(16), self.calendar_hash)

    def forecast_for(self, available_at):
        """First scheduled cutoff at or after availability, including equality."""
        instant = _aware(available_at)
        day = self._within(instant.astimezone(pinned_zone("America/New_York")).date())
        session = self.resolve(day, direction="next")
        if instant > self.clock(session).cutoff_utc:
            session = self.shift(session, 1)
        return session

    def auction_window(self, session, *, broker_deadline_local=None, venue_deadline_local=None,
                       verified_latency_buffer_seconds=None):
        clock = self.clock(session)
        missing = [key for key, value in {
            "broker_deadline_local": broker_deadline_local, "venue_deadline_local": venue_deadline_local,
            "verified_latency_buffer_seconds": verified_latency_buffer_seconds}.items() if value is None]
        deadlines = [self.exchange_instant(session, value) for value in (broker_deadline_local, venue_deadline_local) if value is not None]
        if any(value <= clock.publish_target_utc or value > clock.open_utc for value in deadlines):
            raise CalendarError("Auction deadline must follow publication and not exceed regular opening")
        buffer = verified_latency_buffer_seconds
        if buffer is not None and (type(buffer) not in {int, float} or not math.isfinite(buffer) or buffer < 0):
            raise CalendarError("Latency buffer must be finite nonnegative seconds, never boolean")
        effective = None
        if not missing:
            # Compare before timedelta conversion to reject excessively large values safely.
            latest_buffer = (min(deadlines) - clock.order_submit_target_utc).total_seconds()
            if buffer > latest_buffer:
                raise CalendarError("09:20 target is later than the buffered auction deadline")
            effective = min(deadlines) - timedelta(seconds=buffer)
        return {"configuration_ready": not missing, "missing_parameters": missing,
                "effective_deadline_utc": utc_text(effective) if effective else None,
                "order_submit_target_utc": utc_text(clock.order_submit_target_utc), "can_submit_order": False,
                "remaining_verification_tasks": ["S09.01", "S10.04", "S12.01"], "scope": "deadline_configuration_only"}


def load_calendar(root: Path, lock_path="configs/calendar.lock.json", protocol_path="configs/protocol.calendar.json"):
    policy = AccessPolicy(root)
    lock = parse_json(policy.authorize(lock_path, operation="config_read").read_bytes())
    if not isinstance(lock, dict) or set(lock) != {
        "schema_version", "adapter_version", "exchange", "provider", "coverage_start", "coverage_end",
        "backend_source_sha256", "tzdata_version", "iana_version", "timezone_sha256", "sessions_count", "schedule_sha256", "calendar_hash"}:
        raise CalendarError("Malformed calendar lock")
    calendar = ExchangeCalendar(lock["coverage_start"], lock["coverage_end"])
    # Canonical JSON comparison distinguishes bool from integer metadata.
    if _hash(lock) != _hash(calendar.lock()):
        raise CalendarError("Calendar lock differs from installed rules, timezone data or schedule")
    config = load_protocol(root, protocol_path, policy=policy)
    clock = config["protocol"]["clock"]
    if clock["calendar_provider"] != calendar.manifest["provider"] or clock["calendar_hash"] != calendar.calendar_hash:
        raise CalendarError("Working protocol does not bind this calendar version/hash")
    return calendar, config

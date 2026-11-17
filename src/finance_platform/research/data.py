"""Development-only bars with explicit interval and availability semantics."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from finance_platform.core.contracts import (Instrument, AssetClass, DataScope, instant,
                                            parse_instant, parse_decimal, fingerprint, payload)
from predictor.access import AccessPolicy


class ResearchError(ValueError):
    def __init__(self,code):
        super().__init__(code)
        self.code=code


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    start: datetime
    end: datetime
    available_at: datetime
    close: Decimal
    volume: Decimal
    frequency: str
    tradable: bool = True

    def __post_init__(self):
        for name in ("start", "end", "available_at"):
            object.__setattr__(self, name, instant(getattr(self, name)))
        if not self.start < self.end <= self.available_at or self.frequency not in {"1d", "1m"}:
            raise ResearchError("BAR_CLOCK_INVALID")
        for name in ("close", "volume"):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite() or value < 0:
                raise ResearchError("BAR_UNIT_INVALID")
        if self.close <= 0 or type(self.tradable) is not bool:
            raise ResearchError("BAR_PRICE_OR_STATUS_INVALID")
        if self.frequency == "1m" and self.end - self.start != timedelta(minutes=1):
            raise ResearchError("MINUTE_INTERVAL_INVALID")

    def view(self):
        return {"instrument_id": self.instrument_id, "close": str(self.close), "volume": str(self.volume),
                "available_at": payload(self.available_at), "bar_start": payload(self.start), "bar_end": payload(self.end)}

    @classmethod
    def from_mapping(cls, raw):
        if not isinstance(raw, dict) or set(raw) != {"instrument_id", "start", "end", "available_at", "close", "volume", "frequency", "tradable"}:
            raise ResearchError("BAR_FIELDS_INVALID")
        data = dict(raw)
        for key in ("start", "end", "available_at"):
            data[key] = parse_instant(data[key])
        for key in ("close", "volume"):
            data[key] = parse_decimal(data[key])
        return cls(**data)


@dataclass(frozen=True)
class DevelopmentSample:
    instruments: tuple[Instrument, ...]
    bars: tuple[Bar, ...]
    scope: DataScope = DataScope.SYNTHETIC
    source_id: str = "platform-synthetic-v1"
    license_id: str = "synthetic-development-only"
    ticks: tuple = ()

    def __post_init__(self):
        if type(self.scope) is not DataScope or not self.bars or len(self.bars) > 100_000 or not self.instruments:
            raise ResearchError("DEVELOPMENT_SAMPLE_REQUIRED")
        ids = {x.instrument_id for x in self.instruments}
        if len(ids) != len(self.instruments) or any(x.instrument_id not in ids for x in self.bars):
            raise ResearchError("INSTRUMENT_MAPPING_INVALID")
        keys = {(x.instrument_id, x.start, x.end) for x in self.bars}
        if len(keys) != len(self.bars):
            raise ResearchError("DUPLICATE_BAR")
        if not self.ticks or any(x.instrument_id not in ids for x in self.ticks) or len({x.event_id for x in self.ticks}) != len(self.ticks):
            raise ResearchError("EXECUTION_EVENTS_REQUIRED_OR_DUPLICATE")
        # A revised/late bar is a separate snapshot, never silently latest-wins here.
        for instrument in self.instruments:
            subset = sorted((x for x in self.bars if x.instrument_id == instrument.instrument_id), key=lambda x: x.start)
            if any(a.end > b.start for a, b in zip(subset, subset[1:])):
                raise ResearchError("OVERLAPPING_BARS")
            if any(x.start < instrument.ticker_valid_from or (instrument.ticker_valid_to and x.end > instrument.ticker_valid_to) for x in subset):
                raise ResearchError("HISTORICAL_IDENTITY_INVALID")

    @property
    def sha256(self):
        return fingerprint(self)

    @classmethod
    def from_mapping(cls, raw):
        if not isinstance(raw, dict) or set(raw) != {"instruments", "bars", "scope", "source_id", "license_id", "ticks"}:
            raise ResearchError("SAMPLE_FIELDS_INVALID")
        return cls(tuple(Instrument.from_mapping(x) for x in raw["instruments"]),
                   tuple(Bar.from_mapping(x) for x in raw["bars"]), DataScope(raw["scope"]),
                   raw["source_id"], raw["license_id"], tuple(Tick.from_mapping(x) for x in raw["ticks"]))

    @classmethod
    def read(cls, root, path):
        # Permission audit commits before any data read; protected partitions fail closed.
        return cls.from_mapping(json.loads(AccessPolicy(root).read_bytes(path)))


@dataclass(frozen=True)
class Tick:
    event_id: str
    instrument_id: str
    event_at: datetime
    available_at: datetime
    price: Decimal
    volume: Decimal
    tradable: bool = True

    def __post_init__(self):
        for key in ("event_at", "available_at"):
            object.__setattr__(self, key, instant(getattr(self, key)))
        if self.available_at < self.event_at or type(self.price) is not Decimal or not self.price.is_finite() or self.price <= 0 or type(self.volume) is not Decimal or not self.volume.is_finite() or self.volume < 0 or type(self.tradable) is not bool:
            raise ResearchError("EXECUTION_TICK_INVALID")

    @classmethod
    def from_mapping(cls, raw):
        if set(raw) != {"event_id", "instrument_id", "event_at", "available_at", "price", "volume", "tradable"}:
            raise ResearchError("EXECUTION_TICK_FIELDS_INVALID")
        data = dict(raw)
        for key in ("event_at", "available_at"):
            data[key] = parse_instant(data[key])
        for key in ("price", "volume"):
            data[key] = parse_decimal(data[key])
        return cls(**data)


def synthetic_sample(frequency="1d", count=8, *, two_assets=False):
    """No market reads; deterministic prices deliberately oscillate to exercise orders."""
    from predictor.time.calendar import ExchangeCalendar
    calendar = ExchangeCalendar("2026-10-01", "2026-10-20")
    instruments = tuple(Instrument("SYN-"+str(i), "SYN"+str(i), AssetClass.US_EQUITY,
                         "XNYS", "USD", Decimal("0.01"), Decimal("1"), False, "XNYS", "T+1",
                         datetime(2020, 1, 1, tzinfo=timezone.utc)) for i in range(2 if two_assets else 1))
    bars, ticks = [], []
    closes = ["10", "11", "12", "9", "8", "13", "14", "10"]
    for i in range(count):
        if frequency == "1d":
            session = calendar.sessions[i]
            start, end = session.open_utc, session.close_utc
        elif frequency == "1m":
            start = calendar.sessions[0].open_utc + timedelta(minutes=i)
            end = start + timedelta(minutes=1)
        else:
            raise ResearchError("FREQUENCY_UNSUPPORTED")
        for j, instrument in enumerate(instruments):
            bars.append(Bar(instrument.instrument_id, start, end, end, Decimal(closes[i % 8])+j,
                            Decimal("1000"), frequency))
            # Explicit independent synthetic prints, never daily OHLC volume at open.
            opening_price = Decimal(closes[(i-1) % 8])+j
            for k, at, price in ((0, start, opening_price), (1, start+timedelta(seconds=30), opening_price),
                                  (2, end, Decimal(closes[i % 8])+j)):
                if frequency == "1m" and i > 0 and k == 0:
                    continue
                ticks.append(Tick(f"tick-{i}-{j}-{k}", instrument.instrument_id, at, at, price, Decimal("100")))
    return DevelopmentSample(instruments, tuple(bars), ticks=tuple(ticks))


def sample_from_version(value):
    if isinstance(value, dict) and value.get("schema_version") in {"research-input-2","research-input-3"}:
        fields={"schema_version", "sample", "origin"}|({"execution","source_snapshot"} if value["schema_version"]=="research-input-3" else set())
        if set(value) != fields or not isinstance(value["origin"], dict):
            raise ResearchError("RESEARCH_INPUT_ENVELOPE_INVALID")
        if value["schema_version"]=="research-input-3" and (not isinstance(value["execution"],dict) or value["execution"].get("model")!="independent-source-prints-1"):
            raise ResearchError("RESEARCH_EXECUTION_MODEL_INVALID")
        return DevelopmentSample.from_mapping(value["sample"])
    return DevelopmentSample.from_mapping(value)

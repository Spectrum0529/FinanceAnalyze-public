"""Frozen, scoped regular-way equity settlement schedule, not a broker funding clock."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from finance_platform.core.contracts import instant
from .accounting import TradingError

PROFILE = "USD-CASH-T1-DTCC-SCHEDULE-2024-2026-v2"
T1_EFFECTIVE_FROM = date(2024, 5, 28)
SUPPORTED_YEARS = (2024, 2025, 2026)
# Annual DTC settlement notices: 19404-23, 20972-24, 23036-25.
# Limited DTC services on Good Friday/observed July 3 are NOT CNS settlement:
# NSCC a9562 (2025), DTC 23891-26 and 24351-26/NSCC a9778 (2026).
# Columbus/Veterans dates have explicitly no DTC settlement, not inferred from
# federal observance. No Saturday-Friday/Sunday-Monday rule is extrapolated.
_CLOSED = {
    2024: "01-01 01-15 02-19 03-29 05-27 06-19 07-04 09-02 10-14 11-11 11-28 12-25",
    2025: "01-01 01-20 02-17 04-18 05-26 06-19 07-04 09-01 10-13 11-11 11-27 12-25",
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 10-12 11-11 11-26 12-25",
}
CLOSED_DAYS = frozenset(date.fromisoformat(f"{year}-{day}") for year, days in _CLOSED.items() for day in days.split())


def declaration():
    return {"profile": PROFILE, "convention": "T+1", "t1_effective_from": "2024-05-28",
            "supported_years": list(SUPPORTED_YEARS), "last_supported_trade_date": "2026-12-30",
            "scope": "REGULAR_WAY_US_EQUITY_ETF_CNS_SCHEDULE",
            "release_clock": "SIMULATION_ASSUMPTION_09_30_AMERICA_NEW_YORK",
            "evidence": "OFFICIAL_INDEXED_NOTICES_ORIGINAL_DOWNLOADS_UNAVAILABLE",
            "actual_broker_funds": "NOT_VERIFIED", "unscheduled_disruptions": "RECONCILIATION_REQUIRED"}


def release_at(at: datetime) -> datetime:
    """Next scheduled weekday excluding the explicit annual CNS closure dates.

    09:30 New York is a declared simulation cash-release assumption. It is not
    proof of broker settled cash. DTC/NSCC stayed open for settlement on Jan 9,
    2025 despite the exchange closure (21333-25); do not intersect with XNYS.
    Unscheduled clearing disruption or broker availability requires reconciliation.
    """
    day = instant(at).astimezone(ZoneInfo("America/New_York")).date()
    if day < T1_EFFECTIVE_FROM:
        raise TradingError("HISTORICAL_SETTLEMENT_UNSUPPORTED")
    if day.year not in SUPPORTED_YEARS:
        raise TradingError("SETTLEMENT_PROFILE_OUT_OF_RANGE")
    candidate = day + timedelta(days=1)
    while True:
        if candidate.year not in SUPPORTED_YEARS:
            raise TradingError("SETTLEMENT_PROFILE_OUT_OF_RANGE")
        if candidate.weekday() < 5 and candidate not in CLOSED_DAYS:
            return datetime.combine(candidate, time(9, 30), ZoneInfo("America/New_York")).astimezone(ZoneInfo("UTC"))
        candidate += timedelta(days=1)

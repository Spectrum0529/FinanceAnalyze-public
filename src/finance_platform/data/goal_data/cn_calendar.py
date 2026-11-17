"""Bounded SZSE calendars transcribed from the exchange's annual notices."""
from datetime import date, timedelta

from finance_platform.data.contracts import DataError

SZSE_CLOSURES = {
    2024: (
        "SZSE:2023-409+2024-0201:XSHE",
        (("01-01", "01-01"), ("02-09", "02-17"), ("04-04", "04-06"),
         ("05-01", "05-05"), ("06-10", "06-10"), ("09-15", "09-17"), ("10-01", "10-07")),
    ),
    2025: (
        "SZSE:2024-413:XSHE",
        (("01-01", "01-01"), ("01-28", "02-04"), ("04-04", "04-06"),
         ("05-01", "05-05"), ("05-31", "06-02"), ("10-01", "10-08")),
    ),
    2026: (
        "SZSE:2025-481+2026-0206+2026-0917:XSHE",
        (("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"), ("05-01", "05-05"),
         ("06-19", "06-21"), ("09-25", "09-27"), ("10-01", "10-07")),
    ),
}


def shenzhen_sessions(start, end):
    days = []
    sources = []
    for year in range(start.year, end.year + 1):
        if year not in SZSE_CLOSURES:
            raise DataError("GOAL_DATA_CALENDAR_UNAVAILABLE")
        source, closures = SZSE_CLOSURES[year]
        sources.append(source)
        lower, upper = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        closed = [(date.fromisoformat(f"{year}-{a}"), date.fromisoformat(f"{year}-{b}")) for a, b in closures]
        for offset in range((upper - lower).days + 1):
            current = lower + timedelta(days=offset)
            if current.weekday() < 5 and not any(a <= current <= b for a, b in closed):
                days.append(current.isoformat())
    return days, ";".join(sources)

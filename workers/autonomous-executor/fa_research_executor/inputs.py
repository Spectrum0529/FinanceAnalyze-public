"""Project verified value-only exports into native numeric engine rows."""

from .autonomous_receipts import ReceiptRejected, checked_package, checked_packages
from .protocol import Rejected, require


def ohlcv_frame(rows):
    """Bind price fields by name, independently of wire/dict order."""
    import pandas as pd

    columns = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    expected = {"at", *columns}
    require(all(type(row) is dict and row.keys() == expected for row in rows), "OHLCV_FRAME_FIELDS_INVALID")
    frame = pd.DataFrame(rows).set_index("at")[list(columns)]
    frame.index = pd.to_datetime(frame.index)
    return frame.rename(columns=columns)


def _project(body):
    rows = {}
    for row in body["rows"]:
        rows.setdefault(row["ticker"], []).append({
            "at": row["session_date"],
            **{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")},
        })
    for records in rows.values():
        records.sort(key=lambda row: row["at"])
    return rows


def authorized_projection(package, *, engineering_only=False):
    try:
        body, _, receipt = checked_package(package, engineering_only=engineering_only)
    except ReceiptRejected as error:
        raise Rejected(str(error)) from error
    return _project(body), receipt


def merge_packages(packages, *, engineering_only=False, allow_cn=False):
    try:
        verified = checked_packages(packages, engineering_only=engineering_only, allow_cn=allow_cn)
    except ReceiptRejected as error:
        raise Rejected(str(error)) from error
    combined, receipts = {}, []
    for body, receipt in verified:
        combined.update(_project(body))
        receipts.append(receipt)
    return combined, receipts

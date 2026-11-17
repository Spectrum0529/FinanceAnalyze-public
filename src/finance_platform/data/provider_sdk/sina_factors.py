"""Strict literal d/f/c parsing and versioned Decimal qfq; never eval/remote JS."""
import ast
from bisect import bisect_right
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re
import time

from finance_platform.core.contracts import fingerprint
from finance_platform.data.contracts import DataError
from .contracts import number
from .history_contracts import SINA_PRICE_POLICY, QUANTITY_BASIS, sina_symbol

TRANSFORM = "SINA_QFQ_DECIMAL_EXACT_AFFINE_96_V1"
FACTOR_BYTES = 512*1024
FACTOR_ROWS = 4096


def parse_factors(raw, symbol):
    deadline = time.monotonic()+2
    def check():
        if time.monotonic() >= deadline: raise DataError("SINA_FACTOR_PARSE_TIMEOUT")
    if not sina_symbol(symbol) or symbol in {"CIEN", "AI"}:
        raise DataError("SINA_KNOWN_FACTOR_QUALITY_UNSUPPORTED")
    if type(raw) is not bytes or not 0 < len(raw) <= FACTOR_BYTES:
        raise DataError("SINA_FACTOR_SIZE_INVALID")
    try:
        text = raw.decode("ascii")
    except UnicodeError:
        raise DataError("SINA_FACTOR_STRUCTURE_INVALID") from None
    # Identifier has no identity authority. Exact ticker identity is the fixed
    # HTTPS endpoint, paired with independently bound price/identity evidence.
    # Whole assignment only, data literal only; no executable JS accepted.
    matched = re.fullmatch(r'[ \t\r\n]*(?:var[ \t\r\n]+)?[A-Za-z_$][A-Za-z0-9_$]{0,95}[ \t\r\n]*=[ \t\r\n]*(\{.*\})[ \t\r\n]*;?[ \t\r\n]*', text, re.S)
    if matched is None:
        raise DataError("SINA_FACTOR_ASSIGNMENT_INVALID")
    literal = matched[1]
    lines = literal.splitlines(keepends=True)
    def numeric_segment(node):
        if node.lineno != node.end_lineno: return None
        return lines[node.lineno-1][node.col_offset:node.end_col_offset]
    try:
        tree = ast.parse(literal, mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        raise DataError("SINA_FACTOR_STRUCTURE_INVALID") from None
    count = 0
    def convert(node, depth=0):
        nonlocal count
        check()
        count += 1
        if count > 30000 or depth > 12: raise DataError("SINA_FACTOR_STRUCTURE_INVALID")
        if isinstance(node, ast.Dict):
            value = {}
            for key, item in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or type(key.value) is not str or key.value in value: raise DataError("SINA_FACTOR_STRUCTURE_INVALID")
                value[key.value] = convert(item, depth+1)
            return value
        if isinstance(node, ast.List): return [convert(v, depth+1) for v in node.elts]
        if isinstance(node, ast.Constant) and type(node.value) is str and len(node.value) <= 96: return node.value
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            token = numeric_segment(node)
            if token and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", token) and len(token) <= 96: return token
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) and isinstance(node.operand, ast.Constant) and type(node.operand.value) in {int, float}:
            token = numeric_segment(node)
            if token and re.fullmatch(r"[+-][0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", token) and len(token) <= 96: return token
        raise DataError("SINA_FACTOR_STRUCTURE_INVALID")
    value = convert(tree.body)
    if type(value) is not dict or set(value) not in ({"data"}, {"data", "total"}) or type(value["data"]) is not list or not 0 < len(value["data"]) <= FACTOR_ROWS:
        raise DataError("SINA_FACTOR_STRUCTURE_INVALID")
    if "total" in value and (not str(value["total"]).isdigit() or int(value["total"]) != len(value["data"])):
        raise DataError("SINA_FACTOR_STRUCTURE_INVALID")
    rows, seen = [], set()
    for i, item in enumerate(value["data"]):
        check()
        if type(item) is not dict or set(item) != {"d", "f", "c"}: raise DataError("SINA_FACTOR_STRUCTURE_INVALID")
        try:
            label = item["d"]
            day = date.fromisoformat(label)
            if day.isoformat() != label or label < "1900-01-01" or day > datetime.now(timezone.utc).date() or label in seen: raise ValueError
            seen.add(label)
            f, c = Decimal(item["f"]), Decimal(item["c"])
            if any(not v.is_finite() or len(v.as_tuple().digits) > 24 or not -18 <= v.as_tuple().exponent <= 6 or abs(v) >= Decimal("1e18") for v in (f, c)) or f <= 0: raise ValueError
        except (ValueError, TypeError, ArithmeticError):
            raise DataError("SINA_FACTOR_VALUE_INVALID") from None
        rows.append({"date": label, "f": format(f, "f"), "c": format(c, "f"), "original_row": i,
                     "original_lexemes": {"d": item["d"], "f": item["f"], "c": item["c"]}})
    rows.sort(key=lambda r: r["date"])
    check()
    if Decimal(rows[-1]["f"]) != 1 or Decimal(rows[-1]["c"]) != 0:
        raise DataError("SINA_FACTOR_ANCHOR_UNSUPPORTED")
    return rows


def adjust_rows(rows, factors, *, factor_entity, price_entity):
    if not factors: raise DataError("SINA_FACTOR_EMPTY")
    dates = [f["date"] for f in factors]
    proof = {"price_policy": SINA_PRICE_POLICY, "transform": TRANSFORM, "factor_chain_sha256": fingerprint(factors),
             "factor_entity_sha256": factor_entity["entity_sha256"], "price_entity_sha256": price_entity["entity_sha256"],
             "factor_rows": len(factors), "anchor_date": factors[-1]["date"], "anchor_f": factors[-1]["f"], "anchor_c": factors[-1]["c"],
             "interval_rule": "ASCENDING_FACTOR_FORWARD_PROPAGATION_FIRST_INTERVAL_BACK_EXTENDED",
             "precision": "DECIMAL_96_NO_QUANTIZATION_NOT_SDK_FLOAT_ROUND4", "atomic_server_snapshot_claim": False,
             "price_received_at": price_entity["observed_received_at"], "factor_received_at": factor_entity["observed_received_at"]}
    result = []
    for original in rows:
        pos = max(0, bisect_right(dates, original["session_date"])-1)
        factor = factors[pos]
        before = {k: original[k] for k in ("open", "high", "low", "close")}
        try:
            with localcontext() as ctx:
                ctx.prec = 96
                after = {k: number(format(Decimal(v)*Decimal(factor["f"])+Decimal(factor["c"]), "f"), minimum=Decimal("0.000000000001")) for k, v in before.items()}
        except (DataError, ArithmeticError):
            raise DataError("SINA_QFQ_PRICE_INVALID") from None
        if Decimal(after["low"]) > min(Decimal(after["open"]), Decimal(after["close"])) or Decimal(after["high"]) < max(Decimal(after["open"]), Decimal(after["close"])):
            raise DataError("SINA_QFQ_OHLC_INVALID")
        result.append({**original, **after, "source_unadjusted_ohlc": before, "price_policy": SINA_PRICE_POLICY,
                       "quantity_basis": QUANTITY_BASIS, "price_unit": "USD_per_adjusted_simulation_unit",
                       "provider_basis_claim": "SINA_QFQ_AFFINE_EX_POST_PROVIDER_FACTORS_NOT_TOTAL_RETURN",
                       "source_adjustment_request": {"daily": "unadjusted", "factor": "qfq", "local_transform": TRANSFORM},
                       "basis_verification": "PROVIDER_FACTORS_STRUCTURALLY_VALID_ECONOMIC_CORRECTNESS_UNVERIFIED",
                       "observed_received_at": max(price_entity["observed_received_at"], factor_entity["observed_received_at"]),
                       "source_daily_received_at": price_entity["observed_received_at"], "source_factor_received_at": factor_entity["observed_received_at"],
                       "factor_locator": {"entity": 1, "original_row": factor["original_row"], "date": factor["date"], "f": factor["f"], "c": factor["c"],
                                          "factor_chain_sha256": proof["factor_chain_sha256"], "first_interval_back_extended": original["session_date"] < dates[0]},
                       "sdk_snapshot_basis": "UNADJUSTED_BEFORE_LOCAL_DECIMAL_QFQ_TRANSFORM",
                       "sdk_transform_flags": [*original["sdk_transform_flags"], "SINA_QFQ_AFFINE_DECIMAL_EX_POST_NOT_PIT", "NO_ADDITIONAL_SPLITS_OR_CASH_DIVIDENDS"]})
    return result, proof

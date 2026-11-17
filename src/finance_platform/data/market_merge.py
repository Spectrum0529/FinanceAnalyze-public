"""Compare immutable market snapshots before forming an observed union."""
from decimal import Decimal

from .contracts import DataError

MAX_MERGE_ROWS = 10_000
POLICIES = frozenset({"reject", "prefer_first", "prefer_second", "use_verification"})
PRICE_FIELDS = ("open", "high", "low", "close", "volume")


def _key(row):
    return row["ticker"], row["session_date"]


def _prices(row):
    return tuple(None if row[key] is None else Decimal(row[key]) for key in PRICE_FIELDS)


def merge_rows(first, second, conflict_policy="reject", *, verification=None):
    if conflict_policy not in POLICIES:
        raise DataError("MARKET_MERGE_POLICY_INVALID")
    profile = first["profile"]
    if profile != second["profile"]:
        raise DataError("MARKET_MERGE_INCOMPATIBLE")
    left = {_key(row): row for row in first["rows"]}
    right = {_key(row): row for row in second["rows"]}
    if not left or not right or len(left) != len(first["rows"]) or len(right) != len(second["rows"]):
        raise DataError("MARKET_MERGE_INPUT_INVALID")
    keys = sorted(left.keys() | right.keys())
    if len(keys) > MAX_MERGE_ROWS:
        raise DataError("MARKET_MERGE_LIMIT")
    common = left.keys() & right.keys()
    different = {key for key in common if _prices(left[key]) != _prices(right[key])}
    raw = profile["adjustment"] == "raw"
    reason, requires_verification = None, False
    same_vintage = len(set(first["vintages"]) | set(second["vintages"])) == 1
    yahoo = profile["provider"] == "yfinance" and profile["adjustment"] == "split_adjusted"
    policies = ["reject", "prefer_first", "prefer_second"] if raw else ["reject"]
    canonical = None
    verification_changes = []
    verification_added = 0
    verification_missing = []
    verification_matches = 0
    if verification is not None:
        if (not yahoo or profile != verification["profile"] or not verification["action_evidence"]
                or len(verification["vintages"]) != 1):
            raise DataError("MARKET_MERGE_INCOMPATIBLE_VERIFICATION")
        canonical = {_key(row): row for row in verification["rows"]}
        if not canonical:
            raise DataError("MARKET_MERGE_VERIFICATION_COVERAGE")
        if len(canonical) != len(verification["rows"]) or len(canonical) > MAX_MERGE_ROWS:
            raise DataError("MARKET_MERGE_LIMIT")
        verification_missing = sorted(set(keys) - canonical.keys())
        verification_changes = [
            {"ticker": key[0], "session_date": key[1], "input": index,
             "old": previous[key], "verified": canonical[key]}
            for index, previous in enumerate((left, right))
            for key in sorted(previous.keys() & canonical.keys())
            if _prices(previous[key]) != _prices(canonical[key])
        ]
        verification_added = len(canonical.keys() - set(keys))
        verification_matches = sum(len(previous.keys() & canonical.keys()) for previous in (left, right)) - len(verification_changes)
        policies.append("use_verification")
        if verification_missing:
            reason = "MARKET_MERGE_VERIFICATION_COVERAGE"
        elif (verification_changes or verification_added) and conflict_policy != "use_verification":
            reason = "MARKET_MERGE_VERIFICATION_CHANGED"
        keys = sorted(canonical)
    elif not raw:
        if not same_vintage:
            reason = "MARKET_MERGE_ADJUSTMENT_UNVERIFIED"
            requires_verification = yahoo
    if conflict_policy not in policies and reason is None:
        reason = "MARKET_MERGE_POLICY_INVALID"
    if different and conflict_policy == "reject" and reason is None and canonical is None:
        reason = "MARKET_MERGE_CONFLICT"
    summary = {
        "profile": profile, "rows": len(keys), "duplicates": len(common) - len(different),
        "conflict_count": len(different),
        "conflicts": [{"ticker": key[0], "session_date": key[1], "first": left[key], "second": right[key]}
                      for key in sorted(different)[:20]],
        "can_merge": reason is None, "reason_code": reason,
        "allowed_policies": policies, "requires_verification": requires_verification,
        "verification_changed_rows": len(verification_changes),
        "verification_matching_rows": verification_matches,
        "verification_conflicts": verification_changes[:20],
        "verification_added_rows": verification_added,
        "verification_missing_rows": len(verification_missing),
        "verification_missing": [{"ticker": ticker, "session_date": day}
                                 for ticker, day in verification_missing[:20]],
        "coverage": {"start": min(day for _, day in keys), "end": max(day for _, day in keys),
                     "observed_only": True, "missing_sessions": "NOT_INFERRED"},
    }
    if reason is not None:
        return summary, None
    if canonical is not None:
        return summary, [canonical[key] for key in keys]
    preferred, other = (right, left) if conflict_policy == "prefer_second" else (left, right)
    return summary, [preferred[key] if key in preferred else other[key] for key in keys]

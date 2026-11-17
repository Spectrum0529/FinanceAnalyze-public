"""Goal data uses the existing Massive client, journal and page validator."""
from datetime import datetime, timezone
import time

from finance_platform.core.contracts import canonical
from finance_platform.data.contracts import DataError
from finance_platform.data.massive_control import PAGE_VALIDATION
from finance_platform.data.massive_daily import security_metadata, stream_daily
from finance_platform.data.simple_chunks import DatasetChunks

ASSET_TYPES = {"US_EQUITY": {"CS"}, "US_ETF": {"ETF"}}


def massive_bound(units):
    # Three physical attempts per identity or aggregate unit. Pagination draws
    # from this same family allowance, including retries and failed attempts.
    return 3 * (len({unit.symbol for unit in units}) + len(units))


def batches(library, control, make_client, close_client, context, connection_id, req, units,
            *, deadline, maximum, charge, remaining_bytes, scope):
    owner = context.run.owner_id
    remaining = min(1800, deadline - time.time())
    if remaining <= 0:
        raise DataError("GOAL_DATA_DEADLINE_EXCEEDED")
    request = dict(connection_id=connection_id, market=req.market, asset=req.asset,
                   adjustment=req.adjustment, end=req.end, units=[vars(unit) for unit in units])
    family = control.begin(owner, request, context, maximum=maximum)
    client = make_client(context, library.connection(owner, connection_id))

    try:
        control.bind(client, family, context, wall_seconds=remaining, charge_response=charge,
                     remaining_bytes=remaining_bytes)
        identity = {}
        for ticker in sorted({unit.symbol for unit in units}):
            context.require_active()
            overview = security_metadata(client, [ticker], req.end, context=context,
                                         expected_types=ASSET_TYPES[req.asset])
            if overview["metadata"]["currency"] != req.currency:
                raise DataError("IMPORT_CURRENCY_MISMATCH")
            identity[ticker] = overview["securities"][0]
            control.save_unit(family, "overview:" + ticker, overview, context)
        chunks = DatasetChunks(library.policy, owner, binding=context.storage_binding)
        for unit in units:
            key = unit.symbol + ":" + unit.start + ":" + unit.end
            saved = stream_daily(client, [unit.symbol], unit.start, unit.end,
                                 adjusted=req.adjustment == "split_adjusted", context=context,
                                 chunks=chunks, scope=scope, allow_empty=True)
            if client.client.require_complete_aggregates() != PAGE_VALIDATION:
                raise DataError("MASSIVE_RESPONSE_INVALID")
            saved["page_validation"] = PAGE_VALIDATION
            saved["observed_received_at"] = datetime.now(timezone.utc).isoformat()
            control.save_unit(family, key, saved, context)
            rows = list(chunks.stream(saved, start=unit.start, end=unit.end)) if saved["row_count"] else []
            evidence = {"family": family, "unit": key, "identity": identity[unit.symbol], "manifest": saved}
            yield dict(rows=rows, scope=scope, source_bytes=canonical(evidence).encode(),
                       sdk={"provider": "massive", "interface": "RESTClient.list_aggs/day"},
                       received_at=saved["observed_received_at"])
    finally:
        close_client(client)

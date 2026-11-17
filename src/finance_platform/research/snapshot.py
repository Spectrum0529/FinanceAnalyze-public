"""Imported bars inform decisions; only independent source prints execute orders."""
from finance_platform.core.contracts import Instrument,DataScope,parse_instant,parse_decimal,fingerprint
from .data import Bar,Tick,DevelopmentSample,ResearchError
from .actions import dispatch_at


EXECUTION_MODEL="independent-source-prints-1"


def snapshot_sample(snapshot,dataset,source):
    identities=tuple(Instrument.from_mapping(x) for x in dataset["identities"])
    selected=set(snapshot["instrument_ids"])
    instruments=tuple(i for i in identities if i.instrument_id in selected)
    bars,ticks,records,actions=[],[],[],[]
    for row in snapshot["records"]:
        value,meta=row["values"],row["metadata"]
        if row["kind"]=="bar":
            bars.append(Bar(row["instrument_id"],parse_instant(meta["effective_at"]),parse_instant(value["bar_end"]),
                        parse_instant(meta["available_at"]),parse_decimal(value["close"]),parse_decimal(value["volume"]),
                        {"1Day":"1d","1Min":"1m"}[value["frequency"]],value["trading_status"]=="TRADABLE"))
        elif row["kind"]=="tick":
            if meta["revision"]!=1:
                raise ResearchError("EXECUTION_TICK_REVISION_UNSUPPORTED")
            if (value.get("event_type","TRADE")!="TRADE" or value["price_unit"]!="USD_per_share" or value["volume_unit"]!="shares" or value["currency"]!="USD"
                    or value["trading_status"] not in {"TRADABLE","HALTED"}):
                raise ResearchError("EXECUTION_TICK_UNIT_OR_STATUS_INVALID")
            ticks.append(Tick("tick-"+fingerprint({"id":meta["record_id"],"revision":meta["revision"],"hash":meta["content_hash"]})[:40],
                     row["instrument_id"],parse_instant(meta["effective_at"]),parse_instant(meta["available_at"]),
                     parse_decimal(value["price"]),parse_decimal(value["volume"]),value["trading_status"]=="TRADABLE"))
            records.append({"record_id":meta["record_id"],"revision":meta["revision"],"content_hash":meta["content_hash"],
                            "event_id":value.get("event_id",meta["record_id"]),"availability_basis":value.get("availability_basis","OBSERVED_RECEIPT_ONLY"),
                            "availability_reference":value.get("availability_reference","metadata.received_at")})
        elif row["kind"]=="action":
            dispatch_at(row)
            actions.append(row)
    if not bars:
        raise ResearchError("PRICE_BARS_REQUIRED")
    if not ticks:
        raise ResearchError("EXECUTION_TICKS_REQUIRED")
    if {b.instrument_id for b in bars}-{t.instrument_id for t in ticks}:
        raise ResearchError("EXECUTION_TICKS_MISSING_INSTRUMENT")
    sample=DevelopmentSample(instruments,tuple(sorted(bars,key=lambda b:(b.available_at,b.start,b.instrument_id))),
                  DataScope(snapshot["scope"]),dataset["source_id"],source["license"]["license_id"],
                  tuple(sorted(ticks,key=lambda t:(t.available_at,t.event_at,t.instrument_id,t.event_id))))
    return sample,{"model":EXECUTION_MODEL,"adapter_version":"3","tick_records":records,"actions":actions,
                  "capacity":"independent_print_volume_only","decision_clock":"bar_available_at",
                  "fill_clock":"observed_print_exchange_time_strictly_after_order_effective_at"}

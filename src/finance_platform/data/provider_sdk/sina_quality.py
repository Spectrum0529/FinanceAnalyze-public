"""Versioned whole-bar quarantine; preserves unfiltered parent/source identity."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
import math
import re
from pathlib import Path
from finance_platform.core.contracts import fingerprint,parse_instant
from finance_platform.data.contracts import DataError
from .history_contracts import HISTORY_PROFILE,SINA_QFQ_INTERFACE

POLICY_ID="SINA_FINITE_OHLC_BAR_QUARANTINE_V1"
POLICY={"id":POLICY_ID,"version":1,"mode":"WHOLE_BAR_QUARANTINE","max_quarantined_rows":1,
    "allowed_reasons":["LOW_ABOVE_BODY_MIN","HIGH_BELOW_BODY_MAX"],"all_other_failures":"STOP"}
POLICY_HASH=fingerprint(POLICY)
PUBLIC_POLICY={**POLICY,"content_hash":POLICY_HASH}
SEMANTICS={"execution_gap_policy":"PREVIOUS_ACCEPTED_CLOSE_NEXT_ACCEPTED_OPEN_DEFER_INTENT",
    "rolling_window_basis":"ACCEPTED_OBSERVATIONS","valuation_gap_policy":"NO_MARK_ON_QUARANTINED_SESSION",
    "drawdown_basis":"MAX_DRAWDOWN_OVER_ACCEPTED_OBSERVATIONS","completeness":"UNVERIFIED_PROVIDER_RESPONSE_WITH_EXPLICIT_QUARANTINE"}
FIELDS=("open","high","low","close","volume","amount")
MAX_REPORT_BYTES=8192

def enabled(call):
    if "quality_policy_id"not in call and "quality_policy_hash"not in call:return False
    if call.get("quality_policy_id")!=POLICY_ID or call.get("quality_policy_hash")!=POLICY_HASH or call.get("provider")!="akshare" or call.get("profile")!=HISTORY_PROFILE or call.get("interface")!=SINA_QFQ_INTERFACE:bad("SINA_QUALITY_POLICY_INVALID")
    return True

def bad(code="SINA_QUALITY_INVALID"):raise DataError(code)
def digest(value):
    if type(value)is not str or not re.fullmatch(r"[0-9a-f]{64}",value):bad()
    return value
def day(value):
    if type(value)is not str or len(value)!=10:bad()
    try:
        if date.fromisoformat(value).isoformat()!=value:bad()
    except ValueError:bad()
    return value
def value_text(value):
    if type(value) not in {int,Decimal} or not Decimal(value).is_finite() or not abs(Decimal(value))<Decimal("1e24"):bad()
    return str(value)
def classify(rows,check=lambda:None):
    if type(rows)is not list or not 1<=len(rows)<=40000:bad("SINA_FULL_HISTORY_LIMIT")
    quarantine=[];previous=""
    for index,row in enumerate(rows):
        if index%64==0:check()
        if type(row)is not dict or not set(FIELDS)|{"date"}<=set(row) or set(row)-set(FIELDS)-{"date","prevclose"}:bad("SINA_SCHEMA")
        label=day(row["date"])
        if label<=previous:bad("SINA_DATE")
        previous=label
        for key in FIELDS+(("prevclose",) if "prevclose"in row else ()):
            value_text(row[key])
        numbers={k:float(row[k]) for k in FIELDS}
        if any(numbers[k]<1e-12 for k in ("open","high","low","close")) or not numbers["volume"].is_integer() or abs(numbers["volume"])>9007199254740991 or numbers["volume"]<0 or numbers["amount"]<0:bad("SINA_FINANCIAL")
        reasons=[]
        if numbers["low"]>min(numbers["open"],numbers["close"]):reasons.append("LOW_ABOVE_BODY_MIN")
        if numbers["high"]<max(numbers["open"],numbers["close"]):reasons.append("HIGH_BELOW_BODY_MAX")
        if reasons:
            if len(quarantine)>=POLICY["max_quarantined_rows"]:bad("SINA_QUARANTINE_LIMIT")
            needed={"open","close"}|({"low"} if "LOW_ABOVE_BODY_MIN"in reasons else set())|({"high"} if "HIGH_BELOW_BODY_MAX"in reasons else set())
            values={k:value_text(row[k]) for k in sorted(needed)}
            original={"date":label,**{k:value_text(row[k]) for k in FIELDS+(("prevclose",) if "prevclose"in row else ())}}
            quarantine.append({"source_index":index,"session_date":label,"reasons":reasons,"values":values,"source_row_hash":fingerprint(original)})
    check()
    return quarantine

def parent_ref(entity,proof,sdk):
    parent={"schema_version":"sina-decoded-parent-ref-1","price_entity_sha256":digest(entity["entity_sha256"]),
        "decoded_json_sha256":digest(proof["decoded_json_sha256"]),"decoder_sha256":digest(sdk["decoder_sha256"]),
        "wrapper_sha256":digest(sdk["wrapper_sha256"]),"observed_received_at":entity["observed_received_at"]}
    try:parse_instant(parent["observed_received_at"])
    except (ValueError,TypeError):bad()
    return {**parent,"content_hash":fingerprint(parent)}

def quarantine_hash(parent,entries):
    return fingerprint({"schema_version":"sina-quarantine-list-1","quality_policy_hash":POLICY_HASH,"parent_observation_hash":parent["content_hash"],"entries":entries})
def base_part(report):
    return {k:v for k,v in report.items() if k not in {"report_hash","selected_coverage","gap_dates","selection_hash"}}
def check_base(report):
    if type(report)is not dict or set(base_part(report))!={"schema_version","quality_policy","parent_observation_ref","quarantine_hash","source_coverage","quarantine",*SEMANTICS}:bad()
    if report["schema_version"]!="sina-history-quality-1" or report["quality_policy"]!=PUBLIC_POLICY or any(report[k]!=v for k,v in SEMANTICS.items()):bad()
    if type(report["quality_policy"])is not dict or any(type(report["quality_policy"][k])is not int for k in ("version","max_quarantined_rows")):bad()
    parent=report["parent_observation_ref"]
    if type(parent)is not dict or set(parent)!={"schema_version","content_hash","price_entity_sha256","decoded_json_sha256","decoder_sha256","wrapper_sha256","observed_received_at"} or parent["schema_version"]!="sina-decoded-parent-ref-1":bad()
    for k in ("content_hash","price_entity_sha256","decoded_json_sha256","decoder_sha256","wrapper_sha256"):digest(parent[k])
    try:parse_instant(parent["observed_received_at"])
    except (ValueError,TypeError):bad()
    if parent["content_hash"]!=fingerprint({k:v for k,v in parent.items() if k!="content_hash"}):bad()
    entries=report["quarantine"]
    if type(entries)is not list or len(entries)>1:bad()
    source=report["source_coverage"]
    if type(source)is not dict or set(source)!={"first_date","last_date","accepted_first_date","accepted_last_date","total_rows","accepted_rows","quarantined_rows"}:bad()
    for k in ("first_date","last_date","accepted_first_date","accepted_last_date"):day(source[k])
    if not source["first_date"]<=source["accepted_first_date"]<=source["accepted_last_date"]<=source["last_date"]:bad()
    if any(type(source[k])is not int for k in ("total_rows","accepted_rows","quarantined_rows")) or not 1<=source["total_rows"]<=40000 or source["quarantined_rows"]!=len(entries) or source["accepted_rows"]+len(entries)!=source["total_rows"] or source["accepted_rows"]<1:bad()
    for e in entries:
        if type(e)is not dict or set(e)!={"source_index","session_date","reasons","values","source_row_hash"} or type(e["source_index"])is not int or not 0<=e["source_index"]<source["total_rows"]:bad()
        day(e["session_date"]);digest(e["source_row_hash"])
        if not source["first_date"]<=e["session_date"]<=source["last_date"] or e["reasons"] not in [["LOW_ABOVE_BODY_MIN"],["HIGH_BELOW_BODY_MAX"],["LOW_ABOVE_BODY_MIN","HIGH_BELOW_BODY_MAX"]]:bad()
        needed={"open","close"}|({"low"} if "LOW_ABOVE_BODY_MIN"in e["reasons"] else set())|({"high"} if "HIGH_BELOW_BODY_MAX"in e["reasons"] else set())
        if type(e["values"])is not dict or set(e["values"])!=needed:bad()
        try:
            if any(type(v)is not str or len(v)>96 or not Decimal(v).is_finite() or not Decimal("1e-12")<=Decimal(v)<Decimal("1e24") for v in e["values"].values()):bad()
            vals={k:float(v) for k,v in e["values"].items()}
        except (ValueError,ArithmeticError):bad()
        if ("LOW_ABOVE_BODY_MIN"in e["reasons"] and not vals["low"]>min(vals["open"],vals["close"])) or ("HIGH_BELOW_BODY_MAX"in e["reasons"] and not vals["high"]<max(vals["open"],vals["close"])):bad()
    if any(e["session_date"] in {source["accepted_first_date"],source["accepted_last_date"]} for e in entries):bad()
    if report["quarantine_hash"]!=quarantine_hash(parent,entries) or report["report_hash"]!=fingerprint(base_part(report)):bad()
    return report

def selected(report,start,end,rows):
    check_base(report);day(start);day(end)
    if not start<end or type(rows)is not list or not rows:bad("HISTORY_WINDOW_NO_OBSERVED_ROWS")
    gap_dates=[e["session_date"] for e in report["quarantine"] if start<=e["session_date"]<end]
    for row in rows:
        if type(row)is not dict or type(row.get("raw_row_locator"))is not dict or "full_history_row"not in row["raw_row_locator"] or "session_date"not in row:bad()
        day(row["session_date"])
    indices=[r["raw_row_locator"]["full_history_row"] for r in rows]
    dates=[r["session_date"] for r in rows]
    excluded={e["source_index"] for e in report["quarantine"]}
    if indices!=sorted(set(indices)) or any(type(i)is not int or not 0<=i<report["source_coverage"]["total_rows"] or i in excluded for i in indices) or dates!=sorted(set(dates)) or any(not start<=d<end or d in gap_dates for d in dates):bad()
    result={**deepcopy(base_part(report)),"report_hash":report["report_hash"],"selected_coverage":{"first_date":dates[0],"last_date":dates[-1],"total_rows":len(rows)+len(gap_dates),"accepted_rows":len(rows),"quarantined_rows":len(gap_dates)},"gap_dates":gap_dates,
        "selection_hash":fingerprint({"schema_version":"sina-quality-selection-1","report_hash":report["report_hash"],"requested_range":{"start":start,"end":end},"accepted_source_indices":indices})}
    if len(json.dumps(result,ensure_ascii=True,separators=(",",":")).encode())>MAX_REPORT_BYTES:bad("SINA_QUALITY_LIMIT")
    return result

def capture(rows,quarantine,indices,call,entity,proof,sdk):
    excluded={e["source_index"] for e in quarantine}
    accepted=[i for i in range(len(rows)) if i not in excluded]
    if not accepted:bad("SINA_NO_ACCEPTED_ROWS")
    parent=parent_ref(entity,proof,sdk)
    base={"schema_version":"sina-history-quality-1","quality_policy":PUBLIC_POLICY,"parent_observation_ref":parent,"quarantine_hash":quarantine_hash(parent,quarantine),
        "source_coverage":{"first_date":rows[0]["date"],"last_date":rows[-1]["date"],"accepted_first_date":rows[accepted[0]]["date"],"accepted_last_date":rows[accepted[-1]]["date"],"total_rows":len(rows),"accepted_rows":len(accepted),"quarantined_rows":len(quarantine)},"quarantine":quarantine,**SEMANTICS}
    base["report_hash"]=fingerprint(base)
    selected_rows=[{"session_date":rows[i]["date"],"raw_row_locator":{"full_history_row":i}} for i in indices]
    return selected(base,call["start"],call["end"],selected_rows)

def validate_selection(report,start,end,rows):
    """Public FA015 helper over the exact acquired view, no source-data reads."""
    actual=selected(report,start,end,rows)
    try:
        if fingerprint(actual)!=fingerprint(report):bad("SINA_QUALITY_SELECTION_MISMATCH")
    except (ValueError,TypeError):bad("SINA_QUALITY_SELECTION_MISMATCH")
    return actual

def validate_capture(report,call,manifest,artifacts,rows):
    if not enabled(call):bad("SINA_QUALITY_POLICY_INVALID")
    validate_selection(report,call["start"],call["end"],rows)
    sdk=manifest["sdk_ref"];parent=report["parent_observation_ref"];source=report["source_coverage"]
    prices=[e for e in manifest["entities"] if e.get("role")=="sina_us_full_history"]
    if len(prices)!=1:bad()
    entity=prices[0]
    if entity.get("origin")!="finance.sina.com.cn" or entity.get("path")!="/staticdata/us/"+call["provider_symbol"] or entity.get("assignment_validated") is not True:bad()
    raw=artifacts[entity["artifact"]]
    if hashlib.sha256(raw).hexdigest()!=parent["price_entity_sha256"] or entity["entity_sha256"]!=parent["price_entity_sha256"] or parent["observed_received_at"]!=entity["observed_received_at"]:bad()
    expected_wrapper=hashlib.sha256(Path(__file__).with_name("sina_decoder_quality.js").read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    pins=json.loads(Path(__file__).with_name("pins.json").read_text(encoding="utf-8"))
    if parent["wrapper_sha256"]!=expected_wrapper or expected_wrapper!=pins["sina"]["quality_wrapper_sha256"] or sdk["wrapper_sha256"]!=expected_wrapper or parent["decoder_sha256"]!=pins["sina"]["decoder_sha256"] or sdk["decoder_sha256"]!=parent["decoder_sha256"]:bad()
    if parent["decoded_json_sha256"]!=sdk["decoded_json_sha256"] or source["total_rows"]!=sdk["full_history_rows"] or source["first_date"]!=sdk["full_history_start"] or source["last_date"]!=sdk["full_history_end"]:bad()
    indices=[r["raw_row_locator"]["full_history_row"] for r in rows]
    if indices!=sdk["selected_full_history_indices"] or len(indices)!=sdk["selected_window_rows"]:bad()
    return report

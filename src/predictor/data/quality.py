"""Coverage diagnostics. Observations never become tradable labels or filled prices."""
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
from html import escape
import json


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def present(value):
    return value is not None and value != ""


def write_development(policy, path, raw):
    """Existing AccessPolicy authorizes paths; it has no operation='write'."""
    target = policy.authorize(path)
    if not target.is_relative_to(policy.partitions['development']):
        raise ValueError('Detailed source output must remain in development')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb') as out:
        out.write(raw)


def duplicate_details(rows):
    """Rows are (native key, original bytes, locator); retain all extra observations."""
    first, details = {}, []
    for key, raw, locator in rows:
        hashed = digest(raw)
        if key in first:
            original_hash, original_locator = first[key]
            details.append({"category": "DUPLICATE_PRIMARY_KEY", "conflicting": hashed != original_hash,
                "locator": locator, "first_locator": original_locator, "row_hash": hashed,
                "first_row_hash": original_hash, "key_hash": digest(str(key).encode("utf-8"))})
        else:
            first[key] = hashed, locator
    return details


def volume_state(payload, *, halt_evidence=False, expected_member=False):
    if payload is None:
        return "SUPPLIER_MISSING_CONFIRMED" if expected_member else "UNOBSERVED_EXPECTED_UNIVERSE_UNKNOWN"
    if payload.get("volume") is None:
        return "MISSING_VOLUME_NOT_ZERO"
    if payload["volume"] == 0:
        return "HALT_CONFIRMED_BY_DATED_EVIDENCE" if halt_evidence else "SOURCE_EXCHANGE_NO_TRADE_REASON_UNKNOWN"
    return "TRADED_ON_SOURCE_EXCHANGE"


def unit_conflicts(rows):
    """Same semantic field with contradictory declared units; unknown scale is separate."""
    units = defaultdict(set)
    for row in rows:
        for field in ("price_unit", "volume_unit"):
            if present(row.get(field)):
                units[field].add(row[field])
    return [{"category": "UNIT_CONFLICT", "field": f, "units": sorted(v)}
            for f, v in units.items() if len(v) > 1]


def price_jumps(sources, threshold):
    """Native continuity candidates only. Skip missing identity/price/volume or date gaps."""
    details, comparisons, turnover = [], 0, []
    ordered = sorted(sources, key=lambda s: s["date"])
    for previous, current in zip(ordered, ordered[1:]):
        def keyed(source):
            result = {}
            for number, record in enumerate(source["records"], 1):
                p = record["payload"]
                if p.get("symbol") and p.get("cusip") and p.get("listing_market_code"):
                    key = (p["symbol"], p["cusip"], p["listing_market_code"])
                    if key in result:
                        raise ValueError("Ambiguous native key in price diagnostic")
                    result[key] = number, record
            return result
        old, new = keyed(previous), keyed(current)
        turnover.append({"from": previous["date"], "to": current["date"], "common_native_keys": len(old.keys() & new.keys()),
            "only_previous": len(old.keys() - new.keys()), "only_current": len(new.keys() - old.keys()),
            "classification": "UNEXPLAINED_NATIVE_MEMBERSHIP_TURNOVER_NOT_SUPPLIER_MISSING_OR_DELISTING"})
        if not current.get("adjacent_to_previous", False):
            continue
        for key in sorted(old.keys() & new.keys()):
            old_number, old_record = old[key]
            number, record = new[key]
            a, b = old_record["payload"], record["payload"]
            first, last = a["prices"].get("last_usd"), b["prices"].get("last_usd")
            if not present(first) or not present(last) or Decimal(first) <= 0 or Decimal(last) <= 0:
                continue
            if volume_state(a) != "TRADED_ON_SOURCE_EXCHANGE" or volume_state(b) != "TRADED_ON_SOURCE_EXCHANGE":
                continue
            comparisons += 1
            change = abs(Decimal(last) / Decimal(first) - 1)
            if change >= Decimal(threshold):
                details.append({"category": "PRICE_JUMP_CANDIDATE", "source_hash": current["hash"],
                    "date": current["date"], "row_number": number, "row_hash": record["metadata"]["content_hash"],
                    "previous_source_hash": previous["hash"], "previous_row_number": old_number,
                    "previous_row_hash": old_record["metadata"]["content_hash"], "native_key_hash": digest(str(key).encode()),
                    "absolute_fraction_change": str(change), "cause": "UNRESOLVED_ACTION_ERROR_OR_NATIVE_IDENTITY_CHANGE",
                    "formal_return_eligible": False})
    return details, comparisons, turnover


def field_value(name, source, record):
    """Explicit semantic mapping, never promote local placeholders to permanent identity."""
    group, field = name.split(".", 1)
    p, m, kind = record["payload"], record["metadata"], source["kind"]
    if group == "lineage":
        if field == "security_id":
            return p.get("source_security_id") or p.get("security_id")
        if field in {"request_parameters", "raw_response_hash", "response_received_at", "source_schema_version"}:
            return {"request_parameters": source["capture"], "raw_response_hash": source["hash"],
                "response_received_at": source["manifest"]["response_received_at"],
                "source_schema_version": p.get("source_schema") or "NYSE_prices_2.3"}[field]
        if field in {"license_evidence_reference", "availability_evidence"}:
            return None  # Empty evidence containers and license IDs are not evidence of rights/availability.
        return m.get(field)
    if group == "identity":
        mapping = {"security_id": "source_security_id" if kind == "master" else "security_id", "issuer_id": "issuer_id",
            "ticker": "symbol", "security_name": "security_name", "primary_listing_mic": "listing_market",
            "security_type": "security_type"}
        if field == "primary_listing_mic" and kind == "master":
            return p.get("primary_market")  # Native market label, not a certified MIC mapping.
        return p.get(mapping.get(field, "__missing__"))
    if group == "daily" and kind == "prices":
        if field.startswith("raw_"):
            return p["prices"].get({"raw_open": "open_usd", "raw_high": "high_usd", "raw_low": "low_usd", "raw_close": "last_usd"}[field])
        return {"session_date": p.get("session"), "volume": p.get("volume"),
                "trading_status": volume_state(p)}.get(field)
    if group == "actions" and kind in {"ticker", "info", "distribution"}:
        mapping = {"action_id": "native_event_id", "action_type": "event_type", "effective_date": "event_date",
            "record_date": "record_date", "payment_date": "pay_date", "cash_distribution": "cash_amount", "cash_currency": "cash_currency"}
        if field == "ex_date" and kind == "distribution":
            return p.get("event_date")
        if field == "split_ratio":
            return (p.get("pre_split_shares"), p.get("post_split_shares")) if p.get("pre_split_shares") and p.get("post_split_shares") else None
        if field == "source_document_reference":
            return source["hash"]
        return p.get(mapping.get(field, "__missing__"))
    if group == "terminal" and field == "last_trade_date" and kind == "prices":
        return p.get("last_trade_reference_date")  # Last source-exchange trade reference, not delisting final trade.
    if group == "industry" and field == "industry_code" and kind == "master":
        return source["master_fields"][record["source_row_number"] - 1][97] or None
    return None


def applicability(group, kind):
    return group == "lineage" or (group == "identity" and kind in {"master", "prices"}) or (
        group == "daily" and kind == "prices") or (group == "actions" and kind in {"ticker", "info", "distribution"}) or (
        group == "terminal" and kind in {"prices", "ticker", "info"}) or (group == "industry" and kind == "master") or (
        group == "auction" and kind == "prices")


def analyze(sources, requirements, config, calendar_control):
    details, summary = [], []
    for source in sources:
        records, kind = source["records"], source["kind"]
        statuses, actions = Counter(), Counter()
        fields = {}
        strata = {dimension: {} for dimension in ['status', 'listing_market', 'industry_icb_snapshot']}
        for requirement in requirements["fields"]:
            if not applicability(requirement["group"], kind):
                continue
            count = sum(present(field_value(requirement["id"], source, r)) for r in records)
            fields[requirement["id"]] = {"denominator": len(records), "observed": count, "missing": len(records) - count,
                "observed_fraction": count / len(records) if records else None, "research_usable": 0}
        for number, record in enumerate(records, 1):
            p = record["payload"]
            detail_start = len(details)
            locator = {"source_hash": source["hash"], "date": source["date"], "row_number": number,
                "row_hash": record["metadata"]["content_hash"], "row_number_basis": "normalized_parser_order"}
            if kind == "prices":
                row_status = volume_state(p)
                statuses[row_status] += 1
                if p.get("volume") is not None and p["volume"] > config["suspect_volume_threshold"]:
                    details.append({**locator, "category": "SUSPECT_VOLUME_MAGNITUDE", "resolution": "SCALE_UNVERIFIED_NO_DIVISOR_APPLIED"})
                values = p["prices"]
                if all(present(values.get(f)) for f in ("open_usd", "high_usd", "low_usd", "last_usd")):
                    o, h, l, c = (Decimal(values[f]) for f in ("open_usd", "high_usd", "low_usd", "last_usd"))
                    if h < l or (p.get("volume") is not None and p["volume"] > 0 and (h < max(o, c) or l > min(o, c))):
                        details.append({**locator, "category": "OHLC_RANGE_ANOMALY", "formal_label_eligible": False})
            elif kind == "master":
                native = source["master_fields"][record["source_row_number"] - 1]
                condition = native[config["master_status_fields"]["halt_condition"] - 1]
                row_status = "SNAPSHOT_HALT_CONDITION_" + condition if condition else "SNAPSHOT_HALT_BLANK_NOT_HISTORICAL_LISTING_STATUS"
                statuses[row_status] += 1
            else:
                actions[p["event_type"]] += 1
                row_status = "NOTICE_" + p["event_type"]
                statuses[row_status] += 1
                if p["event_type"] in config["terminal_candidate_types"]:
                    details.append({**locator, "category": "TERMINAL_RECOVERY_MISSING", "event_type": p["event_type"],
                        "recovery_status": "UNKNOWN_NOT_ZERO", "candidate_only_not_confirmed_delisting": True})
                if p.get("notice_clock_quality") == "NONSTANDARD_24H_WITH_PM_SOURCE_LITERAL":
                    details.append({**locator, "category": "NONSTANDARD_PUBLICATION_CLOCK", "timezone": None})
            dimension_values = {'status': row_status,
                'listing_market': p.get('listing_market_code') or p.get('primary_market') or 'UNKNOWN',
                'industry_icb_snapshot': (native[97] or 'MISSING') if kind == 'master' else 'UNKNOWN_NO_DATED_JOIN'}
            observations = {f: present(field_value(f, source, record)) for f in fields}
            row_anomalies = Counter(d['category'] for d in details[detail_start:])
            for dimension, key in dimension_values.items():
                item = strata[dimension].setdefault(key, {'rows': 0, 'fields': {f: {'observed': 0, 'missing': 0} for f in fields},
                    'historical_first_available_missing': 0, 'historical_latency_seconds': None, 'row_anomaly_counts': {}})
                item['rows'] += 1
                item['historical_first_available_missing'] += not present(record['metadata'].get('first_available_at'))
                for f, observed in observations.items():
                    item['fields'][f]['observed' if observed else 'missing'] += 1
                for category, count in row_anomalies.items():
                    item['row_anomaly_counts'][category] = item['row_anomaly_counts'].get(category, 0) + count
        duplicates = duplicate_details(source["native_rows"])
        details.extend({**d, "source_hash": source["hash"], "date": source["date"]} for d in duplicates)
        conflicts = unit_conflicts([r["payload"] for r in records])
        details.extend({**d, "source_hash": source["hash"], "affected_rows": len(records)} for d in conflicts)
        if kind == "prices":
            details.append({"category": "VOLUME_SCALE_UNVERIFIED", "source_hash": source["hash"],
                "date": source["date"], "affected_rows": len(records), "unit_conflict_confirmed": False})
        industry = {}
        status_dimensions = {}
        if kind == "master":
            for label, column in config["master_industry_fields"].items():
                counts = Counter(row[column - 1] or "MISSING" for row in source["master_fields"] if len(row) == 157)
                industry[label] = dict(sorted(counts.items()))
            for label, column in config["master_status_fields"].items():
                status_dimensions[label] = dict(sorted(Counter(row[column - 1] or "MISSING" for row in source["master_fields"] if len(row) == 157).items()))
        reception = datetime.fromisoformat(source["manifest"]["response_received_at"].replace("Z", "+00:00"))
        summary.append({"kind": kind, "source_id": records[0]["metadata"]["source_id"], "capture": source["capture"],
            "source_hash": source["hash"], "date": source["date"], "physical_rows": source["physical_rows"],
            "normalized_rows": len(records), "duplicate_primary_keys": len(duplicates),
            "conflicting_primary_keys": sum(d["conflicting"] for d in duplicates), "fields": fields,
            "status_strata": dict(sorted(statuses.items())), "snapshot_status_dimensions": status_dimensions,
            "stratified_quality": strata,
            "event_types": dict(sorted(actions.items())), "snapshot_industry_strata": industry,
            "listing_market_strata": dict(sorted(Counter(r["payload"].get("listing_market_code") or r["payload"].get("primary_market") or "UNKNOWN" for r in records).items())),
            "historical_first_available_missing": sum(not present(r["metadata"].get("first_available_at")) for r in records),
            "published_exact_count": sum(r["metadata"]["timestamp_quality"].get("published_at") == "exact" for r in records),
            "published_date_only_count": sum(r["metadata"]["timestamp_quality"].get("published_at") == "date_only" for r in records),
            "delay": {"historical_delivery_latency_seconds": None, "reason": "NO_EXACT_PUBLICATION_AND_HISTORICAL_FIRST_AVAILABILITY_PAIR",
                "sample_retrieval_age_calendar_days": (reception.date() - date.fromisoformat(source["date"])).days,
                "retrieval_age_is_delivery_latency": False}, "unit_conflicts": len(conflicts), "formal_research_eligible": False})
    jumps, comparisons, turnover = price_jumps([s for s in sources if s["kind"] == "prices"], config["price_jump_fraction"])
    details.extend(jumps)
    field_summary = []
    for requirement in requirements["fields"]:
        name = requirement["id"]
        selected = [s["fields"][name] for s in summary if name in s["fields"]]
        if requirement["group"] == "calendar":
            observed = calendar_control["covered_sessions"]
            denominator = observed
            status = "LOCKED_SCHEDULE_CONTROL_NOT_ACQUIRED_MARKET_RECORD"
        else:
            observed = sum(s["observed"] for s in selected)
            denominator = sum(s["denominator"] for s in selected)
            status = "OBSERVED_ONLY_UNCERTIFIED" if observed else "NOT_OBSERVED"
        field_summary.append({"id": name, "group": requirement["group"], "denominator": denominator,
            "observed": observed, "missing": denominator - observed, "status": status, "research_usable": 0,
            "certified_history_start": None, "certified_history_end": None})
    dates = sorted({s["date"] for s in sources})
    price_dates = sorted({s["date"] for s in sources if s["kind"] == "prices"})
    counts = Counter(d["category"] for d in details)
    categories = ["PRICE_JUMP_CANDIDATE", "DUPLICATE_PRIMARY_KEY", "UNIT_CONFLICT", "TERMINAL_RECOVERY_MISSING",
        "SUSPECT_VOLUME_MAGNITUDE", "OHLC_RANGE_ANOMALY", "NONSTANDARD_PUBLICATION_CLOCK", "VOLUME_SCALE_UNVERIFIED"]
    blockers = ["OFFICIAL_PRIMARY_LISTING_AUCTION_ABSENT", "DATED_PERMANENT_IDENTITY_AND_ISSUER_LINKAGE_ABSENT",
        "VOLUME_SCALE_UNVERIFIED", "HISTORICAL_AVAILABILITY_AND_REVISIONS_UNVERIFIED", "TERMINAL_RECOVERY_UNRESOLVED",
        "HISTORICAL_INDUSTRY_AND_TARGET_MEMBERSHIP_ABSENT", "BROADER_RIGHTS_UNVERIFIED", "EXPECTED_UNIVERSE_UNKNOWN"]
    return {"task_id": "data-quality-report", "scope": "DEVELOPMENT_SAMPLE_DIAGNOSTICS", "schema_version": 1,
        "new_downloads": 0, "protected_partition_reads": 0, "forward_fill": False, "formal_research_eligible": False,
        "data_certification": "UNVERIFIED", "model_evaluation": "NOT_RUN", "sources": summary,
        "coverage": {"observed_file_dates": dates, "price_session_dates": price_dates, "price_sessions": len(price_dates),
            "observed_envelope": [dates[0], dates[-1]], "continuous_market_history_claimed": False,
            "certified_research_start": None, "certified_research_end": None, "expected_universe_denominator": None,
            "supplier_missing_confirmed": None, "market_halts_confirmed_for_price_sessions": None,
            "industry_join_to_prices": "BLOCKED_DIFFERENT_SNAPSHOT_DATE_NO_VALIDITY_HISTORY"},
        "calendar_control": calendar_control, "field_coverage": field_summary,
        "requirements_gap": {"required_fields": len(field_summary), "fields_with_sample_observations": sum(f["observed"] > 0 and f["group"] != "calendar" for f in field_summary),
            "schedule_control_fields": sum(f["group"] == "calendar" for f in field_summary),
            "fields_without_sample_observations": sum(f["observed"] == 0 for f in field_summary),
            "certified_research_fields": 0, "certified_research_missing_fields": len(field_summary), "blocking_gates": blockers},
        "anomaly_counts": {c: counts[c] for c in categories}, "anomaly_details": details,
        "price_jump_check": {"threshold_fraction": config["price_jump_fraction"], "comparison_count": comparisons,
            "basis": config["price_jump_basis"], "formal_returns": False}, "native_membership_turnover": turnover,
        "notes": ["Physical rows are not distinct securities or a historical universe.",
            "Zero source-exchange volume is not a confirmed market halt; missing volume is not zero.",
            "No inferred delisting, terminal zero, adjusted price, issuer identity, auction or historical availability.",
            "Publication dates/narrative clocks do not supply an exact historical latency.",
            "Industry and halt fields describe only the master snapshot; no cross-date backfill.",
            "Sample observations include date-only/native-label/local lineage values; observed is not certified."]}


def render_html(report):
    """Self-contained inert HTML. Every provider-controlled value is escaped."""
    def table(headers, rows):
        return '<table><thead><tr>' + ''.join('<th>' + escape(str(h)) + '</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
            '<tr>' + ''.join('<td>' + escape(str(v)) + '</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table>'
    gap = report["requirements_gap"]
    sources = table(["来源", "文件/交易日", "原始行", "解析行", "状态分层", "延迟"], [
        [s["kind"], s["date"], s["physical_rows"], s["normalized_rows"], json.dumps(s["status_strata"], ensure_ascii=False), "历史延迟未知"] for s in report["sources"]]).replace('<table>', '<table class="source-summary">', 1)
    fields = table(["需求字段", "适用样本行/控制日", "有观测", "缺失", "研究可用", "状态"], [
        [f["id"], f["denominator"], f["observed"], f["missing"], 0, f["status"]] for f in report["field_coverage"]])
    anomalies = table(["类别", "明细数"], report["anomaly_counts"].items())
    details = table(["类别", "日期", "定位与依据"], [[d["category"], d.get("date", ""), json.dumps(d, ensure_ascii=False)] for d in report["anomaly_details"]])
    industry = table(["快照日期", "分类体系", "快照分布"], [[s["date"], label, json.dumps(counts, ensure_ascii=False)] for s in report["sources"] for label, counts in s["snapshot_industry_strata"].items()])
    strata = ''.join('<details><summary>' + escape(s['kind'] + ' / ' + s['date'] + ' / ' + dimension) + '</summary>' + table(
        ['分组', '行数', '历史时点缺失', '行异常计数', '字段有观测/缺失'], [[key, item['rows'], item['historical_first_available_missing'],
        json.dumps(item['row_anomaly_counts'], ensure_ascii=False), json.dumps(item['fields'], ensure_ascii=False)] for key, item in values.items()]) + '</details>'
        for s in report['sources'] for dimension, values in s['stratified_quality'].items())
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据覆盖与质量报告</title><style>
body{font:15px/1.65 system-ui,sans-serif;background:#f4f6fa;color:#17233b;margin:0}main{max-width:1280px;margin:32px auto;padding:0 24px}h1{font-size:30px}h2{margin-top:36px}p{max-width:1000px}.cards{display:flex;gap:14px;flex-wrap:wrap}.card{padding:18px;background:white;border:1px solid #d5dce7;border-radius:10px;min-width:180px}.card b{display:block;font-size:28px;color:#194e87}.warning{padding:16px;border-left:5px solid #b55316;background:#fff2df}table{border-collapse:collapse;width:100%;background:white;font-size:13px}th,td{text-align:left;vertical-align:top;padding:9px;border:1px solid #dce2eb;overflow-wrap:anywhere}th{background:#e4ebf5}td:last-child{max-width:600px}.source-summary th,.source-summary td{min-width:64px}.source-summary td:nth-child(-n+4){white-space:nowrap}.source-summary td:nth-child(2){min-width:96px}.source-summary td:nth-child(5){min-width:180px}details{margin:18px 0}summary{cursor:pointer;font-weight:600}footer{margin:40px 0;color:#536176}</style><main>
<h1>数据覆盖与质量报告</h1><p>仅评价本次输入数据。原始行保持原样，明细使用文件哈希、行号与行哈希定位；不进行预测评估。</p>
<div class="warning">研究数据认证：UNVERIFIED。当前价格数据包含''' + escape(str(report["coverage"]["price_sessions"])) + '''个交易日；不同来源可能覆盖不同日期，文件日期范围不表示连续历史。零成交量原因未知，不填补价格、不推断停牌或退市回收为零。</div>
<div class="cards">''' + ''.join('<div class="card">' + escape(label) + '<b>' + str(value) + '</b></div>' for label, value in [
        ("来源文件", len(report["sources"])), ("实际原始行", sum(s["physical_rows"] for s in report["sources"])),
        ("价格交易日", report["coverage"]["price_sessions"]), ("已认证研究字段 / 需求", "0 / " + str(gap["required_fields"]))]) + '''</div>
<h2>真实覆盖与限制</h2><p>实际文件日期：''' + escape(', '.join(report["coverage"]["observed_file_dates"])) + '''。可认证研究起止日期均未知；证券全集分母未知，供应商漏数率不可计算。日历锁仅是交易时刻控制，不计作取得行情。</p>
<h2>按来源、日期及状态</h2>''' + sources + '''<h2>产业快照分层</h2><p>仅按证券主档当日分类；缺分类单独计数。缺版本与有效期，禁止回填到价格样本。</p>''' + industry + '''
<h2>分组缺失及异常</h2><p>展开状态、原生上市市场与主档当日 ICB 产业分组，逐字段查看观测/缺失。分组历史延迟均未知；重复/全文件单位诊断另见明细。</p>''' + strata + '''
<h2>需求差距</h2><p>观测不等于认证；日期、原生市场标签、本地版本/接收时间只能作为有限观测。所有正式研究字段仍须通过许可、身份和历史时点等门槛。</p>''' + fields + '''
<h2>异常与明细</h2><p>价格突变为原生键连续性诊断候选，不能解释为正式收益。单位冲突与量纲未验证分别报告；合并/清算仅列为终止回收待核候选。</p>''' + anomalies + '''<details><summary>展开全部异常定位明细</summary>''' + details + '''</details>
<h2>正式用途阻断项</h2>''' + '<ul>' + ''.join('<li>' + escape(b) + '</li>' for b in gap["blocking_gates"]) + '''</ul><footer>生成依据：本次输入数据和质量规则。质量检查通过不表示获准用于正式研究、覆盖全部历史或模型可交易。</footer></main></html>'''

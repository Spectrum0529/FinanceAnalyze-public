"""Readonly authorized research inputs/results for official native TA tools.

No provider acquisition, OHLC synthesis, financial metric calculation or model
orchestration. The child receives a bounded primitive bundle, never database
paths. Parent reauthorizes the same bundle before publication.
"""
from contextlib import closing, contextmanager, ExitStack
from datetime import datetime, time, timezone
import json
import threading
from finance_platform.core.contracts import VersionRef, payload, fingerprint, parse_instant
from .data import ResearchError
from . import tradingagents_workflow as native
from . import native_results


def cutoff(day):
    return datetime.combine(datetime.fromisoformat(day).date(),time.max,tzinfo=timezone.utc)


def _supported_daily_engine(value):
    """Read saved old results without treating them as newly calculated reports."""
    engine = value.get("engine")
    costs = value.get("cost_model")
    if type(engine) is not dict or type(costs) is not dict:
        return False
    calculation = value.get("calculation_version")
    if (value.get("family") != "daily-simulation-result-3"
            or engine.get("adapter_version") != calculation):
        return False
    identity = (engine.get("name"), engine.get("version"), calculation,
                value.get("engine_version"), costs.get("version"))
    supported = (
        ("financeanalyze.daily", "1.0.0", "financeanalyze-daily-calculation-1",
         "financeanalyze-daily-1", "financeanalyze-daily-costs-1"),
        ("backtesting.py", "0.6.6", "backtesting-py-adapter-1",
         "backtesting-py-0.6.6-daily-1", "backtesting-py-costs-1"),
        ("backtesting.py", "0.6.6", "backtesting-py-adapter-2",
         "backtesting-py-0.6.6-daily-1", "backtesting-py-costs-1"),
        ("backtesting.py", "0.6.6", "backtesting-py-adapter-3",
         "backtesting-py-0.6.6-daily-2", "backtesting-py-costs-1"),
    )
    return identity in supported


def evidence_scope_projection(value,summary):
    scope=value.get("data_scope",value.get("scope","UNVERIFIED"))
    if type(scope)is not str:raise ResearchError("TA_EVIDENCE_SCOPE_INVALID")
    return {"scope":scope,"summary":{**summary,"scope":scope,"source_limitations":{key:value[key] for key in
        ("scope","data_scope","undefined_metrics","assumptions","price_policy") if key in value}}}


def result_refs(value):
    if type(value)is not list or len(value)>4:raise ResearchError("TA_EVIDENCE_REFERENCES_INVALID")
    seen=set()
    for item in value:
        if type(item)is not dict or set(item)!={"kind","run_id"} or item["kind"] not in {"backtesting","qlib"}:
            raise ResearchError("TA_EVIDENCE_REFERENCES_INVALID")
        native.binding_id(item["run_id"])
        key=(item["kind"],item["run_id"])
        if key in seen:raise ResearchError("TA_EVIDENCE_REFERENCES_INVALID")
        seen.add(key)
    return value


class DebateInputBridge:
    def __init__(self,research):
        self.research=research;self.runtime=research.runtime;self.database=research.database
        self._daily=None;self._gateway=None

    def _strict_resource(self,principal,reference):
        native.reference(reference)
        with closing(self.database.connect()) as con:
            row=con.execute("SELECT owner_id,kind FROM resource_versions WHERE resource_id=? AND version=?",
                (reference["resource_id"],reference["version"])).fetchone()
        if row is None or row["owner_id"]!=principal.user_id or row["kind"]!="data":
            raise ResearchError("TA_DATA_OWNER_OR_KIND_INVALID")
        self.runtime.versions.get(principal,VersionRef(**reference))

    def _evidence(self,principal,ref,as_of,ticker):
        if ref["kind"]=="qlib":
            value=self.research.result(principal,ref["run_id"])
            if value.get("family")!="qlib-workflow-result-1":raise ResearchError("TA_EVIDENCE_ENGINE_MISMATCH")
            grant=self.research.inputs.validate_qlib(principal,value["export_reference"])
            dates=[row["datetime"] for row in value["predictions"]]
            dates += [row["datetime"] for row in value["portfolio_report"]]
            dates += grant["data"]["calendar"]
            if not dates or max(dates)>as_of:raise ResearchError("TA_FUTURE_EVIDENCE_REJECTED")
            if ticker not in {row["instrument"] for row in value["predictions"]}:raise ResearchError("TA_EVIDENCE_SYMBOL_MISMATCH")
            summary={"metrics":value["metrics"],"undefined_metrics":value["undefined_metrics"],
                "units":"Qlib US native backtest, see exact workflow/source semantics","coverage_start":min(dates),"coverage_end":max(dates),
                "coverage_basis":"Full referenced source exposure, including warmup and maturity buffer; metrics remain original native result"}
            source={"export_reference":value["export_reference"],"native_manifest_hash":value["native_manifest_hash"]}
        else:
            if self._daily is None:
                # Reuse the normal module's already-constructed service. A
                # core version resolver can run inside its writer transaction;
                # never run another module's migrations in that boundary.
                self._gateway=getattr(self.runtime,"debate_history_gateway",None)
                self._daily=getattr(self.runtime,"debate_daily_result_store",None)
                if self._gateway is None or self._daily is None:raise ResearchError("TA_BACKTEST_BRIDGE_UNAVAILABLE")
            value=self._daily.result(principal,ref["run_id"])
            if not _supported_daily_engine(value):raise ResearchError("TA_EVIDENCE_ENGINE_MISMATCH")
            if value.get("ticker")!=ticker:raise ResearchError("TA_EVIDENCE_SYMBOL_MISMATCH")
            end=value.get("coverage",{}).get("actual_last")
            if type(end)is not str or end>as_of:raise ResearchError("TA_FUTURE_EVIDENCE_REJECTED")
            history=value.get("history_reference")
            if type(history)is not dict:raise ResearchError("TA_EVIDENCE_SOURCE_AUTHORIZATION_REQUIRED")
            # Native saved history API rechecks source/archive/AccessPolicy and
            # current result pin. No SDKRunner or acquisition is called.
            checked=self._gateway.read_pinned(principal.user_id,history["history_id"],history["version"],ref["run_id"],kind="result")
            if checked["history_content_hash"]!=history["history_content_hash"]:
                raise ResearchError("TA_EVIDENCE_SOURCE_CHANGED")
            if any(row["session_date"]>as_of for row in checked["rows"]):raise ResearchError("TA_FUTURE_EVIDENCE_REJECTED")
            summary={"report":value["report"],"engine":value["engine"],"coverage_start":min(row["session_date"] for row in checked["rows"]),
                "coverage_end":max(row["session_date"] for row in checked["rows"]),"price_policy":value.get("price_policy"),
                "coverage_basis":"Full authorized frozen history exposure; saved report coverage remains explicit",
                "units":"Adjusted simulation units; fees use the saved report's declared basis; source volume may remain UNVERIFIED"}
            source={"history_reference":history}
        scoped=evidence_scope_projection(value,summary);source_scope=scoped["scope"];summary=scoped["summary"]
        # Summary is original saved metrics/reports. No new engine or return
        # computation; truncate by rejection, never by silently dropping data.
        if len(json.dumps(summary,ensure_ascii=False,allow_nan=False).encode())>65536:
            raise ResearchError("TA_EVIDENCE_SUMMARY_TOO_LARGE")
        return {**ref,"scope":source_scope,"result_hash":value.get("stored_result_hash",native_results.digest(value)),
            "projection_hash":native_results.digest(value),"source":source,"summary":summary}

    def authorize(self,principal,request):
        self.runtime.require_owner(principal,principal.user_id)
        reference=request["data_reference"];self._strict_resource(principal,reference)
        raw,sample,_=self.research.inputs.validate(principal,reference)
        instruments=[item for item in sample.instruments if item.ticker==request["ticker"]]
        if len(instruments)!=1:raise ResearchError("TA_INPUT_SYMBOL_UNAVAILABLE")
        instrument=instruments[0];end=cutoff(request["as_of"])
        bars=[bar for bar in sample.bars if bar.instrument_id==instrument.instrument_id]
        if not bars or len(bars)>5000:raise ResearchError("TA_INPUT_ROWS_UNAVAILABLE")
        # Reject future exposure rather than filtering it out after it was
        # supplied. A separately authorized historical view must be frozen first.
        if any(bar.available_at>end or bar.end>end for bar in bars):raise ResearchError("TA_FUTURE_INPUT_REJECTED")
        if raw.get("source_snapshot") is not None:
            snapshot_hash=fingerprint(raw["source_snapshot"])
        else:
            snapshot_hash=fingerprint(raw.get("sample",payload(sample)))
        rows=[bar.view() for bar in bars]
        selected_hash=fingerprint({"instrument":payload(instrument),"bars":rows})
        evidence=[self._evidence(principal,ref,request["as_of"],request["ticker"]) for ref in result_refs(request["evidence_results"])]
        value={"owner_id":principal.user_id,"data_reference":reference,"snapshot_content_hash":snapshot_hash,
            "selected_view_hash":selected_hash,"scope":sample.scope.value,"as_of":request["as_of"],
            "ticker":request["ticker"],"instrument":payload(instrument),"bars":rows,"evidence_results":evidence,
            "price_fields":"CLOSE_VOLUME_ONLY_NO_OHLC_OR_INDICATOR_IMPUTATION",
            "availability":"Source available_at <= end of as_of UTC date; no additional market/PIT certification"}
        if len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())>524288:raise ResearchError("TA_INPUT_BUNDLE_TOO_LARGE")
        return value


class DebateReadonlyPorts:
    """Child-side data endpoints around official ToolNode and tool schemas.

    model_ports is a server-selected actual model adapter (or TEST-file-only
    offline adapter in a separately released test). No client selects it.
    """
    def __init__(self,bundle,model_ports,*,active,assert_effects):
        self.bundle=bundle;self.model_ports=model_ports;self.active=active;self.assert_effects=assert_effects
        self.fixture_sha256=getattr(model_ports,"fixture_sha256",None)
        self.profile_sha256=getattr(model_ports,"profile_sha256",None)
        self.tools_manifest_hash=native_results.digest(bundle)
        self.native_callbacks=getattr(model_ports,"native_callbacks",())
        self.data_scope=bundle["scope"]
        self.evidence_scopes=sorted({item["scope"] for item in bundle["evidence_results"]})
        self.used=[];self.sequence=0;self.lock=threading.Lock()

    def authorize_binding(self,value):
        self.active()
        if any(value[key]!=self.bundle[key] for key in ("owner_id","ticker","as_of","data_reference","snapshot_content_hash","selected_view_hash")):
            raise ResearchError("TA_CHILD_INPUT_BINDING_MISMATCH")
        self.model_ports.authorize_binding(value)

    def assert_no_external_attempts(self):self.assert_effects()

    def _window(self,method,args,kwargs):
        names=("symbol","start_date","end_date") if method=="get_stock_data" else ("symbol","curr_date","look_back_days")
        if len(args)>len(names) or set(kwargs)-set(names):raise ResearchError("TA_TOOL_WINDOW_INVALID")
        values=dict(zip(names,args))
        if set(values)&set(kwargs):raise ResearchError("TA_TOOL_WINDOW_INVALID")
        values.update(kwargs)
        if values.get("symbol")!=self.bundle["ticker"]:raise ResearchError("TA_TOOL_SYMBOL_MISMATCH")
        def day(value):
            try:
                if type(value)is not str or datetime.fromisoformat(value).date().isoformat()!=value:raise ValueError
            except (TypeError,ValueError):raise ResearchError("TA_TOOL_WINDOW_INVALID") from None
            return value
        if method=="get_stock_data":
            if set(values)!=set(names):raise ResearchError("TA_TOOL_WINDOW_INVALID")
            start,end=day(values["start_date"]),day(values["end_date"])
            if start>end:raise ResearchError("TA_TOOL_WINDOW_INVALID")
            maximum=None
        else:
            if not {"symbol","curr_date"}<=set(values):raise ResearchError("TA_TOOL_WINDOW_INVALID")
            start,end=None,day(values["curr_date"]);maximum=values.get("look_back_days",30)
            if type(maximum)is not int or not 1<=maximum<=5000:raise ResearchError("TA_TOOL_WINDOW_INVALID")
        if end>self.bundle["as_of"]:raise ResearchError("TA_FUTURE_TOOL_REQUEST_REJECTED")
        requested={"ticker":values["symbol"],"start":start,"end":end,"end_inclusive":True,"maximum_recent_rows":maximum}
        selected=[row for row in self.bundle["bars"] if (start is None or parse_instant(row["bar_end"]).date().isoformat()>=start)
            and parse_instant(row["bar_end"]).date().isoformat()<=end and parse_instant(row["available_at"])<=cutoff(end)]
        if maximum is not None:selected=selected[-maximum:]
        return requested,selected

    def read(self,method,*args,**kwargs):
        self.active();self.budget.snapshot()
        with self.lock:
            self.sequence+=1;serial=self.sequence
        if serial>32:raise ResearchError("TA_DATA_ENTRY_BUDGET_EXCEEDED")
        self.budget.reserve("tool","authorized-data-entry-"+str(serial))
        result={"scope":self.bundle["scope"],"data_reference":self.bundle["data_reference"],
            "snapshot_content_hash":self.bundle["snapshot_content_hash"],"selected_view_hash":self.bundle["selected_view_hash"],
            "as_of":self.bundle["as_of"],"price_fields":self.bundle["price_fields"]}
        audit={"sequence":serial,"method":method,"bundle_hash":self.tools_manifest_hash,"data_scope":self.bundle["scope"]}
        if method in {"get_stock_data","get_verified_market_snapshot"}:
            try:window,rows=self._window(method,args,kwargs)
            except ResearchError as error:
                with self.lock:self.used.append({**audit,"status":"REJECTED","reason_code":error.code})
                raise
            evidence=[]
            for item in self.bundle["evidence_results"]:
                end=item["summary"].get("coverage_end");start=item["summary"].get("coverage_start")
                # Never present full-result metrics as a newly calculated
                # subwindow. Missing/foreign coverage stays out of this view.
                if type(end)is str and end<=window["end"] and (window["start"] is None or type(start)is str and start>=window["start"]):
                    evidence.append(item)
            days=[parse_instant(row["bar_end"]).date().isoformat() for row in rows]
            coverage={"first":min(days) if days else None,"last":max(days) if days else None,"rows":len(rows)}
            view_hash=native_results.digest({"authorization_view_hash":self.bundle["selected_view_hash"],"request":window,"bars":rows})
            result.update(requested_window=window,coverage=coverage,tool_view_hash=view_hash,bars=rows,saved_research_evidence=evidence)
            audit.update(requested_window=window,coverage=coverage,tool_view_hash=view_hash,status="OK" if rows else "INSUFFICIENT_EVIDENCE")
            if not rows:result.update(status="INSUFFICIENT_EVIDENCE",message="当前获准输入在该工具请求窗口内没有可用数据。")
        else:
            result.update(status="INSUFFICIENT_EVIDENCE",message="当前获准输入未提供此类新闻、财务、社交或指标数据。")
            audit.update(status="INSUFFICIENT_EVIDENCE")
        with self.lock:self.used.append(audit)
        self.active();return json.dumps(result,ensure_ascii=False,allow_nan=False)

    @contextmanager
    def scope(self,native_module,config,value,budget):
        from unittest.mock import patch
        from tradingagents.agents import tools
        from tradingagents.agents.analysts import sentiment_analyst
        from tradingagents.memory import settlement
        self.budget=budget
        def identity(symbol):
            if symbol!=self.bundle["ticker"]:raise ResearchError("TA_TOOL_SYMBOL_MISMATCH")
            return {"quote_type":self.bundle["instrument"]["asset_class"],"exchange":self.bundle["instrument"]["mic"]}
        def no_settlement(*args,**kwargs):raise ResearchError("TA_SETTLEMENT_DATA_NOT_AUTHORIZED")
        with ExitStack() as stack:
            stack.enter_context(self.model_ports.scope(native_module,config,value,budget))
            stack.enter_context(patch.object(native_module,"resolve_instrument_identity",identity))
            stack.enter_context(patch.object(tools,"route_to_vendor",self.read))
            stack.enter_context(patch.object(tools,"build_verified_market_snapshot",lambda *a,**k:self.read("get_verified_market_snapshot",*a,**k)))
            stack.enter_context(patch.object(sentiment_analyst,"fetch_stocktwits_messages",lambda *a,**k:self.read("stocktwits")))
            stack.enter_context(patch.object(sentiment_analyst,"fetch_reddit_posts",lambda *a,**k:self.read("reddit")))
            stack.enter_context(patch.object(sentiment_analyst,"jev_screen",lambda ticker:None))
            stack.enter_context(patch.object(settlement,"get_closes",no_settlement))
            yield
        self.assert_effects()

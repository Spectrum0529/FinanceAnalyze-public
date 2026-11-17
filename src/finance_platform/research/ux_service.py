"""Same owner-scoped service for API/MCP; existing queue and fenced save."""
from finance_platform.core.pending_writes import require_open_key
from .payload_storage import persist as store_payload, hydrate as load_payload, task_binding
from contextlib import closing
from hashlib import sha256
import json
from finance_platform.core.auth import AccountDelegation, Principal
from finance_platform.core.contracts import VersionRef,RunMode,identifier,payload
from finance_platform.core.errors import PlatformError
from finance_platform.core.mcp_data_policy import ReportDataPolicy, local_only
from finance_platform.core.result_lifecycle import active_result, table_exists, visible_clause
from finance_platform.core.security import safe_json,now_text
from .portfolio import digest,fail,benchmark,benchmark_key,request,FAMILY
from .task_plans import plan_task
from .research_cache import plan_eligible, run_plan, validate_policy
from . import benchmark_proxies, sdk_daily
from finance_platform.strategies.contracts import StrategyError
from .daily import DailyError, ENGINE

MIGRATIONS=("""
CREATE TABLE research_ux_results(run_id TEXT PRIMARY KEY REFERENCES runs(run_id),job_id TEXT UNIQUE NOT NULL REFERENCES jobs(job_id),owner_id TEXT NOT NULL REFERENCES users(user_id),family TEXT NOT NULL,result_json TEXT NOT NULL,result_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE research_benchmark_cache(owner_id TEXT NOT NULL REFERENCES users(user_id),cache_key TEXT NOT NULL,result_json TEXT NOT NULL,result_hash TEXT NOT NULL,PRIMARY KEY(owner_id,cache_key));
""","""
CREATE TABLE research_ux_resume_commands(owner_id TEXT NOT NULL REFERENCES users(user_id),idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,state TEXT NOT NULL,job_id TEXT,PRIMARY KEY(owner_id,idempotency_key));
""")


class ResearchTaskService:
    def __init__(self,runtime,*,datasets=None,model_profiles=None,credential_resolver=None,credential_store=None,transports=None):
        self.runtime=runtime;self.database=runtime.database
        if datasets is None:
            from finance_platform.data.simple_datasets import DatasetLibrary
            from predictor.access import AccessPolicy
            datasets=DatasetLibrary(runtime.database,AccessPolicy(runtime.root))
        self.datasets=datasets
        from finance_platform.strategies.service import StrategyService
        self.strategies=StrategyService(runtime)
        self.model_profiles=model_profiles or getattr(runtime,"model_profiles",None)
        self.credential_resolver=credential_resolver;self.transports=transports
        self.credential_store=credential_store
        self.database.apply_module_migrations("research_ux",MIGRATIONS)
        self.database.apply_module_migrations('research_search_evidence',('CREATE TABLE research_search_evidence(sequence INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES jobs(job_id),owner_id TEXT NOT NULL REFERENCES users(user_id),body TEXT NOT NULL,sha256 TEXT NOT NULL);',))
        from finance_platform.core.database import immutable
        with self.database.transaction()as con:immutable(con,'research_search_evidence',('sequence',))

    def principal(self,owner):
        with closing(self.database.connect())as con:row=con.execute("SELECT user_id,username,role,enabled FROM users WHERE user_id=?",(owner,)).fetchone()
        if not row or not row["enabled"]:raise PlatformError("FORBIDDEN",403)
        return Principal(row["user_id"],row["username"],row["role"])

    def catalog(self,principal):
        self.runtime.require_owner(principal,principal.user_id)
        from .strategy_search import TOOLS,limits
        return {"strategies":["buy_hold","sma20","sma_cross_5_20"],"factors":["return_1","momentum_20","sma20_distance"],
            "sdk_instances":sdk_daily.catalog(self.strategies,principal.user_id),
            "sdk_capabilities":{**sdk_daily.CAPABILITIES,"description":"SDK 日线回测限一只证券，最多 256 个交易日，支持整数目标数量及 0/1 信号。"},
            "benchmark_options":benchmark_proxies.catalog(),
            "task_kinds":["dataset_summary","factor_validation","strategy_validation"],"engine":f"{ENGINE['name']}-{ENGINE['version']}",
            "allocation":"总资金等额分仓，未用资金保留为现金，不进行跨证券调仓。","dates_optional":True,
            "limits":{"max_securities":250,"max_rows":1000000,"integer_shares":True},
            'strategy_search':{'workflow':'strategy_search','tools':TOOLS,'limits':limits(),'maximum_limits':{'max_trials':12,'max_model_calls':32,'wall_seconds':120,'max_engine_runs':600,'max_result_bytes':25165824},'max_rows':100000,'max_securities':250,'minimum_training_sessions':80,'minimum_validation_sessions':40,'split':'70/30 chronological','providers':['openai','deepseek'],'engine_budget':'Actual per-symbol invocations, failures included. Need at least2 per symbol for one train+validation.','effect':{'external':True,'destructive':False,'writes_saved_reports':True}},
            "resume":{"dataset_tasks":"无中途断点；新任务明确从头运行。","research.debate":"沿用已有已验证checkpoint续跑。"}}

    @staticmethod
    def reference(dataset):return {k:dataset[k]for k in("dataset_id","version","content_hash")}

    def plan(self,principal,body):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        policy = validate_policy(body.get("cache_policy", "use"))
        if "cache_policy" in body:
            body = {name: value for name, value in body.items() if name != "cache_policy"}
        if isinstance(principal,AccountDelegation):
            source_ids=[body.get("dataset_id")]
            inputs=body.get("parameters")
            if isinstance(inputs,dict) and inputs.get("benchmark_id"):
                source_ids.append(inputs["benchmark_id"])
            records = [self.datasets.get(principal.user_id,ident,selectable=True) for ident in source_ids]
            self._require_mcp_inputs(principal,[self.reference(record) for record in records])
        dataset=self.datasets.get(principal.user_id,body.get("dataset_id"),include_rows=True,selectable=True)
        mode=body.get("execution_mode")or("role_team"if body.get("team_snapshot_id")else"single_assistant"if body.get("model_profiles")else"tools")
        import re
        prompt=body.get('prompt','')
        text=prompt.casefold()if type(prompt)is str else''
        no_search=any(w in text for w in('不搜索','不要搜索','无需搜索','不进行搜索','不做搜索','不要优化','不优化','不进行优化','无需优化','不需要优化','不需要搜索','不找策略','仅回测','只回测','固定策略','单策略','do not search',"don't search",'do not optimize',"don't optimize",'do not optimise',"don't optimise",'no search','without search','single strategy','only backtest','fixed strategy'))
        negated_execution=re.search(r"\b(?:do\s+not|don['’]t|must\s+not|never|without|no|not)\b[^.!?;\n]{0,48}\b(?:search\w*|optimi[sz]\w*|find\w*|discover\w*)\b",text)
        explanatory=re.search(r'(?:解释|说明|介绍|定义|讲解|科普|什么是|如何理解).{0,48}(?:策略|参数|搜索|优化)|(?:策略|搜索|优化).{0,24}(?:含义|定义|概念|原理|是什么)|\b(?:explain|define|describe|definition|meaning|what\s+is|how\s+(?:does|to))\b.{0,64}\b(?:strateg\w*|search\w*|optimi[sz]\w*)',text)
        search_intent=re.search(r'(?:寻找|搜索|挖掘|优化).{0,16}(?:策略|参数)|(?:策略|参数).{0,8}(?:搜索|优化)|(?:比较|对比|验证|测试).{0,8}(?:多个|多种|不同|候选).{0,8}策略|(?:search|find|discover|optimi[sz]e).{0,24}(?:strateg|parameter)|(?:compare|test|evaluate).{0,16}(?:multiple|several|candidate|different).{0,16}strateg',text)
        searching=body.get('workflow')=='strategy_search'or(body.get('workflow')is None and mode!='tools'and not(no_search or negated_execution or explanatory)and search_intent is not None)
        if body.get('workflow')not in(None,'strategy_search','analysis'):fail('SEARCH_WORKFLOW_INVALID')
        if dataset.get("scope") == "licensed_local":
            if mode != "tools" and (mode != "single_assistant" or searching or body.get("evidence")):
                raise PlatformError("WRDS_LOCAL_MODEL_TOOL_REQUIRED", 422)
            if isinstance(body.get("parameters"), dict) and body["parameters"].get("benchmark_id"):
                raise PlatformError("WRDS_BENCHMARK_UNSUPPORTED", 422)
        if mode=='tools'and searching:
            from .strategy_search import candidate,limits
            if set(body)-{'dataset_id','prompt','execution_mode','workflow','search_limits','candidates','parameters'}:fail('SEARCH_REQUEST_INVALID')
            bounds=limits(body.get('search_limits'));values=body.get('candidates')
            if type(values)is not list or not 1<=len(values)<=bounds['max_trials']:fail('SEARCH_CANDIDATE_LIMIT')
            plan={'family':'supported-research-plan-1','status':'READY','kind':'strategy_search','prompt':body.get('prompt','受控参数策略搜索'),'parameters':body.get('parameters',{}),
                'candidates':[candidate(v)for v in values],'search_limits':bounds,'dataset_reference':self.reference(dataset)}
        elif mode=="tools":plan=plan_task({k:v for k,v in body.items()if k not in{'execution_mode','workflow'}},dataset)
        else:
            if type(body)is not dict or set(body)-{"prompt","dataset_id","execution_mode","model_profiles","team_snapshot_id","parameters","evidence","as_of","workflow","search_limits"}:fail("AGENT_TASK_REQUEST_INVALID")
            if mode not in {"single_assistant","model_panel","role_team"}:fail("AGENT_EXECUTION_MODE_INVALID")
            if type(body.get("prompt"))is not str or not 1<=len(body["prompt"])<=2000:fail("TASK_PROMPT_INVALID")
            selected=[];snapshot_id=body.get("team_snapshot_id")
            if mode=="role_team":
                if body.get("model_profiles")or not snapshot_id:fail("AGENT_TEAM_SELECTION_REQUIRED")
                snapshot=self.runtime.agent_teams.get_snapshot(principal,snapshot_id)
                for role,config in snapshot["resolved_roles"].items():
                    selected.append({"profile_id":"team:"+snapshot_id+":"+role,"revision":1,"role":role,"execution_target":config.get("execution_target"),
                        "configuration":{"model":config["model"],"prompt":config["prompt"],"context":"\n".join([config.get("shared_context",""),config.get("private_context","")]),
                        "budgets":config.get("budgets")or snapshot["configuration"]["budgets"]}})
            else:
                if snapshot_id or self.model_profiles is None:fail("AGENT_MODEL_LIBRARY_REQUIRED")
                refs=body.get("model_profiles")
                if type(refs)is not list or not 1<=len(refs)<=12 or mode=="single_assistant"and len(refs)!=1:fail("AGENT_MODEL_SELECTION_INVALID")
                seen=set()
                for ref in refs:
                    if type(ref)is not dict or set(ref)!={"profile_id","revision"}:fail("AGENT_MODEL_REFERENCE_INVALID")
                    identity=(ref["profile_id"],ref["revision"])
                    if identity in seen:fail("AGENT_DUPLICATE_MODEL_SELECTION")
                    seen.add(identity)
                    selected.append(self.model_profiles.prepare_execution_profile(principal,ref["profile_id"],ref["revision"]))
            if any(profile["configuration"]["model"]["provider"]=="offline"for profile in selected):
                return {"status":"NEEDS_INPUT","reason":"离线确定工具请选工具模式；单助手和模型小组需要已配置的真实SDK或本地Ollama。模型接线可用固定transport离线验收，不将固定假回答作为研究。"}
            if any(type(profile.get("execution_target"))is not dict for profile in selected):fail("AGENT_MODEL_TARGET_UNBOUND_RESAVE_CONFIGURATION")
            parameters=body.get("parameters",{})
            if type(parameters)is not dict or set(parameters)-{"initial_cash","fee_per_share","minimum_fee","spread_bps","start_date","end_date"}:fail("AGENT_TOOL_PARAMETERS_INVALID")
            plan={"family":"supported-research-plan-1","status":"READY","kind":"model_research","execution_mode":mode,"prompt":body["prompt"],"model_profiles":selected,
                "parameters":parameters,"dataset_reference":self.reference(dataset),"tools":["model_structured_plan","bounded_local_tools","model_report"],
                "planner":"OFFICIAL_LANGGRAPH_MODEL_TOOL_WORKFLOW_1","requires_paid_model":"PROVIDER_DEPENDENT_USER_EXECUTION_ONLY","market_claim":"NONE"}
            from finance_platform.core.contracts import parse_instant
            from datetime import datetime,timezone
            docs=body.get("evidence",[])
            if type(docs)is not list or len(docs)>16:fail("AGENT_EVIDENCE_INVALID")
            normalized=[];ids=set()
            for doc in docs:
                if type(doc)is not dict or set(doc)-{"id","source","published_at","available_at","text"}or not {"id","source","text"}<=set(doc):fail("AGENT_EVIDENCE_INVALID")
                if any(type(doc[k])is not str for k in("id","source","text"))or not 1<=len(doc["text"])<=16000 or not 1<=len(doc["source"])<=1000 or doc["id"]in ids:fail("AGENT_EVIDENCE_INVALID")
                ids.add(doc["id"])
                for name in("published_at","available_at"):
                    if doc.get(name)is not None and parse_instant(doc[name])>datetime.now(timezone.utc):fail("AGENT_EVIDENCE_FUTURE_TIMESTAMP")
                if doc.get("available_at")and doc.get("published_at")and parse_instant(doc["available_at"])<parse_instant(doc["published_at"]):fail("AGENT_EVIDENCE_TIME_ORDER_INVALID")
                normalized.append({**doc,**{k:parse_instant(doc[k]).astimezone(timezone.utc).isoformat()if doc.get(k)else None for k in("published_at","available_at")}})
            if sum(len(doc["text"])for doc in normalized)>60000:fail("AGENT_EVIDENCE_TOO_LARGE")
            plan["evidence"]=normalized
            if searching:
                from .strategy_search import limits
                if any(p['configuration']['model']['provider']not in{'openai','deepseek'}for p in selected):fail('SEARCH_PROVIDER_TOOL_RUNTIME_UNSUPPORTED')
                plan.update(workflow='strategy_search',search_limits=limits(body.get('search_limits')))
            if body.get("as_of"):
                instant=parse_instant(body["as_of"])
                if instant>datetime.now(timezone.utc):fail("AGENT_EVIDENCE_FUTURE_AS_OF")
                plan["as_of"]=instant.astimezone(timezone.utc).isoformat()
            plan["plan_hash"]=digest(plan)
        if plan["status"]!="READY":return plan
        if policy == "bypass":
            if not plan_eligible(plan["kind"], plan["parameters"]):
                fail("RESEARCH_CACHE_POLICY_UNSUPPORTED")
            plan["cache_policy"] = policy
        data_value={"family":"saved-dataset-reference-1",**self.reference(dataset)}
        data_ref=self.runtime.versions.freeze(principal,"task-data:"+digest(principal.user_id)[:16]+":"+digest(data_value),"1","data",data_value)
        plan["data_reference"]=payload(data_ref)
        if plan["kind"]=="model_research":
            model_value={"family":"model-tool-research-config-1","profiles":plan["model_profiles"],"mode":plan["execution_mode"]}
            model_ref=self.runtime.versions.freeze(principal,"task-models:"+digest(principal.user_id)[:16]+":"+digest(model_value),"1","model",model_value)
            plan["model_reference"]=payload(model_ref)
        benchmark_id=plan.get("parameters",{}).get("benchmark_id")
        if benchmark_id:
            bench=self.datasets.get(principal.user_id,benchmark_id,include_rows=True,selectable=True)
            if bench.get("kind")!="benchmark":fail("BENCHMARK_DATASET_REQUIRED")
            plan["benchmark_reference"]=self.reference(bench)
            plan["plan_hash"]=digest({k:v for k,v in plan.items()if k!="plan_hash"})
        return self._freeze_plan(principal,plan)

    def _freeze_plan(self,principal,plan):
        plan["plan_hash"]=digest({k:v for k,v in plan.items()if k!="plan_hash"})
        ref=self.runtime.versions.freeze(principal,"research-plan:"+digest(principal.user_id)[:16]+":"+plan["plan_hash"],"1","config",plan)
        return {**plan,"plan_id":ref.resource_id,"plan_reference":payload(ref)}

    def _portfolio_plan(self,principal,submitted,policy):
        parameters={k:v for k,v in submitted.items() if k not in {"instance_id","benchmarks"}}
        sdk="instance_id" in submitted
        if ("strategy" in submitted)==sdk:
            fail("PORTFOLIO_STRATEGY_SELECTION_INVALID")
        items=benchmark_proxies.selections(submitted["benchmarks"]) if "benchmarks" in submitted else []
        if (items or sdk) and set(parameters)&{"benchmark_id","benchmark_mode","contributions","dividends"}:
            fail("BENCHMARK_SELECTION_CONFLICT")
        dataset=self.datasets.get(principal.user_id,parameters.get("dataset_id"),selectable=True)
        references=[]
        selected_records=[(item,self.datasets.get(principal.user_id,item["dataset_id"],selectable=True)) for item in items]
        if isinstance(principal,AccountDelegation):
            self._require_mcp_inputs(principal,[self.reference(dataset),
                                              *(self.reference(record) for _,record in selected_records)])
        if dataset.get("scope") not in {"public","synthetic"}:
            fail("PORTFOLIO_COMPARISON_SCOPE_UNSUPPORTED")
        for item, selected in selected_records:
            benchmark_proxies.identity(selected,item["id"])
            references.append({"id":item["id"],"dataset_reference":self.reference(selected)})
        main_reference=self.reference(dataset)
        if sdk:
            parameters["strategy"]="sdk"
            binding,_,_=sdk_daily.binding(self.strategies,principal.user_id,submitted["instance_id"])
        dataset=self.datasets.get(principal.user_id,parameters.get("dataset_id"),include_rows=True,selectable=True)
        if self.reference(dataset)!=main_reference:
            fail("TASK_DATASET_REFERENCE_MISMATCH")
        normalized,_=request(parameters,dataset,sdk=sdk)
        new_plan={
            "family":"supported-research-plan-1","status":"READY","kind":"strategy_validation",
            "prompt":"验证保存日线策略","parameters":normalized,"dataset_reference":self.reference(dataset),
            "request_hash":digest({"parameters":submitted,"cache_policy":policy}),
            "tools":["strategy_validation"],"requires_paid_model":False,"market_claim":"NONE",
        }
        if references:
            new_plan["benchmark_references"]=references
        data_value={"family":"saved-dataset-reference-1",**new_plan["dataset_reference"]}
        data=self.runtime.versions.freeze(principal,"task-data:"+digest(principal.user_id)[:16]+":"+digest(data_value),"1","data",data_value)
        new_plan["data_reference"]=payload(data)
        if sdk:
            value={"family":"sdk-daily-strategy-binding-1",**binding}
            sdk_ref=self.runtime.versions.freeze(principal,"sdk-daily:"+digest(value),"1","strategy",value)
            new_plan["sdk_strategy"]=payload(sdk_ref)
        elif policy=="bypass":
            new_plan["cache_policy"]=policy
        return self._freeze_plan(principal,new_plan)

    @staticmethod
    def _dataset_references(plan):
        refs=[plan["dataset_reference"]]
        if "benchmark_reference" in plan:
            refs.append(plan["benchmark_reference"])
        refs.extend(item["dataset_reference"] for item in plan.get("benchmark_references",[]))
        return refs

    def _sdk_reference(self,principal,plan,*,connection=None):
        value=self.runtime.versions.get(principal,VersionRef(**plan["sdk_strategy"]))
        actual,_,_=sdk_daily.binding(self.strategies,principal.user_id,value["instance_id"],connection=connection,
                                    verify_package=connection is None)
        if value!={"family":"sdk-daily-strategy-binding-1",**actual}:
            fail("SDK_DAILY_STRATEGY_BINDING_MISMATCH")
        return actual

    def _plan(self,principal,reference):
        try:ref=VersionRef(**reference)
        except (TypeError,ValueError):fail("TASK_PLAN_REFERENCE_INVALID")
        plan=self.runtime.versions.get(principal,ref)
        if plan.get("family")!="supported-research-plan-1"or plan.get("status")!="READY"or digest({k:v for k,v in plan.items()if k!="plan_hash"})!=plan.get("plan_hash"):fail("TASK_PLAN_BINDING_MISMATCH")
        return plan

    def _require_mcp_plan(self, principal, plan):
        benchmark = plan.get("benchmark_reference")
        benchmark_id = plan.get("parameters", {}).get("benchmark_id")
        if benchmark_id and (type(benchmark) is not dict or benchmark.get("dataset_id") != benchmark_id):
            raise local_only()
        return self._require_mcp_inputs(principal,self._dataset_references(plan))

    def _require_mcp_inputs(self, principal, references):
        owner = principal.user_id
        policy = ReportDataPolicy(self.datasets, owner)
        pending = list(references)
        checked = set()
        while pending:
            ref = pending.pop()
            try:
                if ref.get("source") == "history":
                    self._require_mcp_history(owner, ref)
                    continue
                if "source" in ref:
                    if ref["source"] != "library":
                        raise ValueError
                    ref = {"dataset_id":ref["id"],"version":ref["version"],"content_hash":ref["content_hash"]}
                policy.require_reference(ref)
                identity = (ref["dataset_id"], ref["version"], ref["content_hash"])
                if identity in checked:
                    continue
                checked.add(identity)
                record = self.datasets.get(owner, ref["dataset_id"])
                ancestry = record.get("market_provenance")
                if ancestry is not None:
                    if type(ancestry) is not dict or type(ancestry.get("inputs",[])) is not list:
                        raise ValueError
                    pending.extend(ancestry.get("inputs",[]))
                    if ancestry.get("verification_reference") is not None:
                        pending.append(ancestry["verification_reference"])
            except (AttributeError, KeyError, TypeError, ValueError):
                raise local_only() from None
        return policy

    def _require_mcp_history(self, owner, ref):
        from finance_platform.core.contracts import fingerprint
        from finance_platform.data.history_store import view_hash
        if type(ref.get("version")) is not int or ref["version"]<1:
            raise local_only()
        with closing(self.database.connect()) as con:
            if not table_exists(con,"data_history_versions"):
                raise local_only()
            row = con.execute("""SELECT body,content_hash FROM data_history_versions
                                 WHERE owner_id=? AND history_id=? AND version=?""",
                              (owner,ref["id"],ref["version"])).fetchone()
        if row is None:
            raise local_only()
        value = json.loads(row["body"])
        if (fingerprint(value)!=row["content_hash"] or value.get("provider") not in {"yfinance","akshare"}
                or view_hash(row["content_hash"],value["requested_range"],0,value["actual_coverage"]["row_count"])!=ref["content_hash"]):
            raise local_only()

    def require_mcp_report(self, principal, run_id, report):
        with closing(self.database.connect()) as con:
            row = con.execute("""
                SELECT j.parameters_json,j.parameters_hash FROM research_ux_results r
                JOIN jobs j ON j.job_id=r.job_id AND j.run_id=r.run_id AND j.owner_id=r.owner_id
                WHERE r.run_id=? AND r.owner_id=? AND j.handler='research.dataset_task'
                    AND j.status='SUCCEEDED'""", (run_id, principal.user_id)).fetchone()
        try:
            if row is None or sha256(row["parameters_json"].encode()).hexdigest() != row["parameters_hash"]:
                raise ValueError
            parameters = json.loads(row["parameters_json"])
            plan = self._plan(principal, parameters["plan_reference"])
            if parameters.get("acceptance_id") != "accepted-plan:" + plan["plan_hash"]:
                raise ValueError
            policy = self._require_mcp_plan(principal, plan)
            if (type(report) is not dict or report.get("run_id") != run_id
                    or report.get("plan_hash") != plan["plan_hash"]
                    or report.get("dataset_reference") != plan["dataset_reference"]):
                raise ValueError
            if "benchmark_references" in plan:
                observed = [{"id": value["id"], "dataset_reference": value["dataset_reference"]}
                            for value in report["benchmarks"]]
                if observed != plan["benchmark_references"]:
                    raise ValueError
            if "benchmark_reference" in plan:
                if report["benchmark"]["dataset_reference"] != plan["benchmark_reference"]:
                    raise ValueError
            if "sdk_strategy" in plan:
                reference = plan["sdk_strategy"]
                binding = self.runtime.versions.get(principal, VersionRef(**reference))
                expected = {"family": "sdk-daily-strategy-binding-1", **report["strategy_reference"]}
                if (report.get("strategy_version_reference") != reference or binding != expected
                        or binding["owner_id"] != principal.user_id):
                    raise ValueError
            policy.require_document(report)
        except (KeyError, TypeError, ValueError):
            raise local_only() from None

    def start(self,principal,body):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        if type(body)is not dict or set(body)!={"plan_reference","idempotency_key"}or not body["idempotency_key"]:fail("TASK_START_INVALID")
        plan=self._plan(principal,body["plan_reference"])
        parameters={"plan_reference":body["plan_reference"],"acceptance_id":"accepted-plan:"+plan["plan_hash"]}
        # Return an identical accepted submission even after its dataset was
        # trashed. A different request reusing the key still conflicts.
        delegated = isinstance(principal, AccountDelegation)
        if delegated:
            self._require_mcp_plan(principal, plan)
        with self.database.transaction() if delegated else closing(self.database.connect()) as con:
            if delegated:
                principal.check_approval(con)
            require_open_key(con, principal.user_id, "jobs", body["idempotency_key"])
            old=con.execute("SELECT j.job_id,j.parameters_json,j.handler FROM job_keys k JOIN jobs j USING(job_id) WHERE k.owner_id=? AND k.idempotency_key=?",(principal.user_id,body["idempotency_key"])).fetchone()
        if old:
            if old["handler"]!="research.dataset_task"or json.loads(old["parameters_json"])!=parameters:raise PlatformError("CONFLICT",409)
            return self.runtime.jobs.get(principal,old["job_id"])
        # Accept once at the explicit start boundary. Core's resolver is a
        # read-only exact-reference check inside its existing transaction.
        def check_acceptance(con):
            if delegated:
                principal.check_approval(con)
            require_open_key(con, principal.user_id, "jobs", body["idempotency_key"])

        for ref in self._dataset_references(plan):
            current=self.datasets.get(principal.user_id,ref["dataset_id"],selectable=True)
            if self.reference(current)!=ref:fail("TASK_DATASET_REFERENCE_MISMATCH")
            self.datasets.accept_for_run(principal.user_id, ref, parameters["acceptance_id"],
                                         check_approval=check_acceptance)
        job=self.runtime.jobs.submit(principal,"research.dataset_task",parameters,mode=RunMode.BACKTEST,idempotency_key=body["idempotency_key"])
        for ref in self._dataset_references(plan):
            self.datasets.pin(principal.user_id,ref,job["run_id"])
        return job

    def submit_portfolio(self,principal,body):
        if type(body)is not dict or "idempotency_key"not in body:fail("PORTFOLIO_IDEMPOTENCY_REQUIRED")
        key=body["idempotency_key"]
        try:
            identifier(key)
            identifier(body.get("dataset_id"))
        except ValueError:
            fail("PORTFOLIO_REQUEST_INVALID")
        policy=validate_policy(body.get("cache_policy","use"))
        parameters={k:v for k,v in body.items()if k not in {"idempotency_key","cache_policy"}}
        self.runtime.require_owner(principal,principal.user_id,write=True)
        delegated = isinstance(principal, AccountDelegation)
        extended=bool(parameters.keys()&{"instance_id","benchmarks"})
        with self.database.transaction() if delegated else closing(self.database.connect()) as con:
            if delegated:
                principal.check_approval(con)
            require_open_key(con, principal.user_id, "jobs", key)
            old=con.execute("SELECT j.job_id,j.parameters_json,j.handler FROM job_keys k JOIN jobs j USING(job_id) WHERE k.owner_id=? AND k.idempotency_key=?",(principal.user_id,key)).fetchone()
        if old:
            if old["handler"]!="research.dataset_task":
                raise PlatformError("CONFLICT",409)
            old_plan=self._plan(principal,json.loads(old["parameters_json"])["plan_reference"])
            expected=digest({"parameters":parameters,"cache_policy":policy})
            mismatched=(old_plan.get("request_hash")!=expected if extended else
                        "request_hash" in old_plan or old_plan.get("cache_policy","use")!=policy
                        or any(old_plan["parameters"].get(k)!=v for k,v in parameters.items()))
            if old_plan["kind"]!="strategy_validation" or mismatched:
                raise PlatformError("CONFLICT",409)
            if delegated:
                self._require_mcp_plan(principal, old_plan)
            return self.runtime.jobs.get(principal,old["job_id"])
        if extended:
            planned=self._portfolio_plan(principal,parameters,policy)
            return self.start(principal,{"plan_reference":planned["plan_reference"],"idempotency_key":key})
        planned=self.plan(principal,{"prompt":"验证数据集策略","dataset_id":parameters.get("dataset_id"),"kind":"strategy_validation",
                                    "strategy":parameters.get("strategy"),"parameters":parameters,"cache_policy":policy})
        if planned.get("status")!="READY":
            fail("PORTFOLIO_STRATEGY_SELECTION_INVALID")
        return self.start(principal,{"plan_reference":planned["plan_reference"],"idempotency_key":key})

    def resolve(self,principal,parameters):
        if type(parameters)is not dict or set(parameters)!={"plan_reference","acceptance_id"}:fail("TASK_JOB_PARAMETERS_INVALID")
        plan=self._plan(principal,parameters["plan_reference"])
        if parameters["acceptance_id"]!="accepted-plan:"+plan["plan_hash"]:fail("TASK_ACCEPTANCE_MISMATCH")
        if isinstance(principal,AccountDelegation):
            self._require_mcp_plan(principal,plan)
        if "sdk_strategy" in plan:
            self._sdk_reference(principal,plan)
        for ref in self._dataset_references(plan):
            self.datasets.read_accepted(principal.user_id,ref,parameters["acceptance_id"])
        for item in plan.get("benchmark_references",[]):
            record=self.datasets.get(principal.user_id,item["dataset_reference"]["dataset_id"])
            benchmark_proxies.identity(record,item["id"])
        # Freeze compact source lineage, never large rows in Core metadata.
        # This resolver is read-only: no nested VersionService transaction.
        actual=self.runtime.versions.get(principal,VersionRef(**plan["data_reference"]))
        if actual!={"family":"saved-dataset-reference-1",**plan["dataset_reference"]}:fail("TASK_DATA_LINEAGE_MISMATCH")
        if "sdk_strategy" in plan:
            return {"data":plan["data_reference"],"strategy":plan["sdk_strategy"],
                    "model_reason":"Trusted strategy SDK on saved daily observations; no model"}
        if plan["kind"]=="model_research":
            model=self.runtime.versions.get(principal,VersionRef(**plan["model_reference"]))
            if model!={"family":"model-tool-research-config-1","profiles":plan["model_profiles"],"mode":plan["execution_mode"]}:fail("TASK_MODEL_LINEAGE_MISMATCH")
            return {"data":plan["data_reference"],"model":plan["model_reference"],"model_reason":None}
        return {"data":plan["data_reference"],"model_reason":"Deterministic supported tools; no model inference"}

    def execute(self,context,parameters):
        from .portfolio import PortfolioError
        try:return self._execute(context,parameters)
        except (PortfolioError,StrategyError,DailyError) as error:raise PlatformError(error.code,422)from None

    def retain_search_evidence(self,context,value):
        encoded=json.dumps(value,ensure_ascii=False,allow_nan=False)
        if len(encoded.encode())>24*1024**2:fail('SEARCH_RESULT_BYTES_EXCEEDED')
        stored_evidence=store_payload(self.runtime, task_binding(context), "artifacts", encoded)
        with self.database.transaction()as con:
            context.assert_fenced(con)
            con.execute('INSERT INTO research_search_evidence(job_id,owner_id,body,sha256)VALUES(?,?,?,?)',(context.job_id,context.run.owner_id,stored_evidence,digest(value)))

    def _search_access(self, principal, job_id):
        self.runtime.jobs.get(principal, job_id)
        if not isinstance(principal, AccountDelegation):
            return None
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT parameters_json,parameters_hash FROM jobs
                                 WHERE job_id=? AND owner_id=? AND handler='research.dataset_task'""",
                              (job_id, principal.user_id)).fetchone()
        if row is None:
            raise PlatformError("NOT_FOUND", 404)
        try:
            raw = row["parameters_json"]
            if sha256(raw.encode()).hexdigest() != row["parameters_hash"]:
                raise ValueError
            parameters = json.loads(raw)
            plan = self._plan(principal, parameters["plan_reference"])
            if parameters["acceptance_id"] != "accepted-plan:" + plan["plan_hash"]:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise local_only() from None
        if plan["kind"] != "strategy_search" and plan.get("workflow") != "strategy_search":
            raise PlatformError("NOT_FOUND", 404)
        return self._require_mcp_plan(principal, plan)

    def _search_document(self, principal, row, policy):
        value = load_payload(self.runtime, principal.user_id, "artifacts", row["body"])
        if digest(value) != row["sha256"]:
            fail("SEARCH_EVIDENCE_HASH_MISMATCH")
        if policy is not None:
            policy.require_document(value)
        return value

    def search_events(self,principal,job_id,limit=20,offset=0):
        policy = self._search_access(principal, job_id)
        if type(limit)is not int or not 1<=limit<=50 or type(offset)is not int or not 0<=offset<=10000:raise PlatformError('INVALID_INPUT',422)
        from .search_evidence_views import ACCOUNT_VIEW_BASIS, account_view, summary
        with closing(self.database.connect())as con:
            total=con.execute('SELECT count(*)FROM research_search_evidence WHERE owner_id=? AND job_id=?',(principal.user_id,job_id)).fetchone()[0]
            rows=con.execute('SELECT sequence,body,sha256 FROM research_search_evidence WHERE owner_id=? AND job_id=? ORDER BY sequence LIMIT ? OFFSET ?',(principal.user_id,job_id,limit,offset)).fetchall()
        values=[]
        for row in rows:
            value = self._search_document(principal, row, policy)
            facts = summary(value)
            identity = row["sha256"] if policy is None else digest(account_view(value, facts))
            values.append({'sequence':row['sequence'],'evidence':facts,'sha256':identity,'view':'summary','detail_url':'/api/v1/research/tasks/'+job_id+'/search-evidence/'+str(row['sequence'])+'/series'})
        result = {'family':'strategy-search-evidence-1','job_id':job_id,'view':'summary','total':total,'limit':limit,'offset':offset,'next_offset':offset+len(values)if offset+len(values)<total else None,'items':values,'not_a_success_report':True,'resume_supported':False,'original_evidence_retained':True}
        if policy is not None:
            result["view_basis"] = ACCOUNT_VIEW_BASIS
        return result

    def search_event_section(self,principal,job_id,sequence,section='',limit=500,offset=0,expected_sha256=None):
        policy = self._search_access(principal, job_id)
        if type(sequence)is not int or sequence<1:raise PlatformError('INVALID_INPUT',422)
        from .search_evidence_views import ACCOUNT_VIEW_BASIS, account_view, summary
        service=self
        class ScopedIndex:
            def read(self,actor,ident):
                if actor.user_id!=principal.user_id:raise PlatformError('FORBIDDEN',403)
                with closing(service.database.connect())as con:row=con.execute('SELECT body,sha256 FROM research_search_evidence WHERE owner_id=? AND job_id=? AND sequence=?',(actor.user_id,job_id,sequence)).fetchone()
                if row is None:raise PlatformError('NOT_FOUND',404)
                value = service._search_document(actor, row, policy)
                return value if policy is None else account_view(value, summary(value))
            def _source(self,ident):raise PlatformError('RESULT_SECTION_UNAVAILABLE',404)
        from finance_platform.core.result_views import ResultViews
        result=ResultViews(ScopedIndex()).section(principal,'search-evidence:'+str(sequence),section,limit,offset,expected_sha256)
        if policy is not None:
            result["view_basis"] = ACCOUNT_VIEW_BASIS
        return {**result,'job_id':job_id,'sequence':sequence,'not_a_success_report':True}

    def _execute(self,context,parameters):
        principal=self.principal(context.run.owner_id)
        if self.runtime.versions.get(principal,context.run.config)!=parameters:fail("TASK_RUN_CONFIG_MISMATCH")
        bindings=self.resolve(principal,parameters);plan=self._plan(principal,parameters["plan_reference"])
        if (payload(context.run.data)!=bindings["data"]
                or (None if context.run.model is None else payload(context.run.model))!=bindings.get("model")
                or ("strategy" in bindings and payload(context.run.strategy)!=bindings["strategy"])):
            fail("TASK_RUN_LINEAGE_MISMATCH")
        dataset=self.datasets.read_accepted(principal.user_id,plan["dataset_reference"],parameters["acceptance_id"])
        from .adjusted_portfolio import read_accepted,acquisition,verify_views,basis_key
        source_record=dataset
        def native_reader(start,end):return read_accepted(self.datasets,principal.user_id,plan['dataset_reference'],parameters['acceptance_id'],source_record,start=start,end=end)
        if acquisition(dataset):dataset=native_reader(plan.get('parameters',{}).get('start_date'),plan.get('parameters',{}).get('end_date'))
        verify_views(dataset)
        retained_bytes=0
        def retain(value):
            nonlocal retained_bytes
            encoded=json.dumps(value,ensure_ascii=False,allow_nan=False)
            size=len(encoded.encode());cap=plan.get('search_limits',{}).get('max_result_bytes',8388608)
            if retained_bytes+size>cap:fail('SEARCH_RESULT_BYTES_EXCEEDED')
            self.retain_search_evidence(context,value)
            retained_bytes+=size
        observation_bytes=0;observation_events_dropped=0
        def observe(value):
            nonlocal observation_bytes,observation_events_dropped
            # Optional bounded facts use the existing fenced evidence store;
            # original reservation/result counters remain unchanged.
            try:
                event={'event':'MODEL_CALL_OBSERVATION','model_observation':value}
                size=len(json.dumps(event,ensure_ascii=False,allow_nan=False).encode())
                if size>8192 or observation_bytes+size>261120:
                    observation_events_dropped+=1;return
                self.retain_search_evidence(context,event)
                observation_bytes+=size
            except Exception:
                observation_events_dropped+=1
        context.require_active();context.checkpoint(5,{"stage":"EXECUTING_SUPPORTED_TOOLS"})
        disposition = "ineligible"
        if plan["kind"]=="model_research":
            if dataset.get("scope") == "licensed_local" and (plan["execution_mode"] != "single_assistant" or plan.get("workflow") == "strategy_search" or plan.get("evidence")):
                raise PlatformError("WRDS_LOCAL_MODEL_TOOL_REQUIRED", 422)
            if plan.get('workflow')=='strategy_search':
                from .search_agent import execute_search as execute_agent
            else:from .agent_research import execute_agent
            result=execute_agent(dataset,plan,active=context.require_active,progress=lambda n:context.checkpoint(n,{"stage":"MODEL_TOOL_RESEARCH"}),
                secret_resolver=lambda choice,*,target:self._secret(principal,choice,target=target),target_resolver=lambda choice,*,expected_target:self._target(principal,choice,expected_target=expected_target),transports=self.transports,observe=observe,
                **({'native_reader':native_reader if acquisition(dataset)else None,'retain':retain}if plan.get('workflow')=='strategy_search'else{}))
        elif plan['kind']=='strategy_search':
            from .strategy_search import run_search
            result=run_search(dataset,plan['parameters'],plan['candidates'],search_limits=plan['search_limits'],active=context.require_active,progress=lambda n:context.checkpoint(n,{'stage':'STRATEGY_SEARCH'}),native_reader=native_reader if acquisition(dataset)else None,retain=retain)
        elif "sdk_strategy" in plan:
            from .portfolio import execute
            sdk_ref=self._sdk_reference(principal,plan)
            result=execute(dataset,plan["parameters"],active=context.require_active,
                           progress=lambda n:context.checkpoint(n,{"stage":"SIMULATING_DATASET_SDK"}),
                           sdk_factory=lambda ticker:sdk_daily.DailyDecisions(
                               self.strategies,sdk_ref,dataset,ticker,context.require_active))
            result["strategy_reference"]=sdk_ref
            result["strategy_version_reference"]=plan["sdk_strategy"]
        else:
            result, disposition = run_plan(self.runtime,context,dataset,plan,
                                           lambda n:context.checkpoint(n,{"stage":"SIMULATING_DATASET"}))
        if "benchmark_reference"in plan:
            bench=self.datasets.read_accepted(principal.user_id,plan["benchmark_reference"],parameters["acceptance_id"])
            spec=plan["parameters"]
            bench=read_accepted(self.datasets,principal.user_id,plan['benchmark_reference'],parameters['acceptance_id'],bench,start=spec.get('start_date'),end=spec.get('end_date'))
            verify_views(bench)
            cache_key=benchmark_key(bench,spec)
            with closing(self.database.connect())as con:
                row=con.execute("SELECT result_json,result_hash FROM research_benchmark_cache WHERE owner_id=? AND cache_key=?",(principal.user_id,cache_key)).fetchone()
            if row:
                candidate=load_payload(self.runtime, principal.user_id, "reports", row["result_json"])
                if digest(candidate)!=row["result_hash"]or candidate.get("cache_key")!=cache_key:fail("BENCHMARK_CACHE_INTEGRITY_FAILED")
            else:candidate=benchmark(bench,spec,active=context.require_active)
            stored_candidate=store_payload(self.runtime, task_binding(context), "reports", safe_json(candidate)) if row is None else None
            with self.database.transaction()as con:
                context.assert_fenced(con)
                row=con.execute("SELECT result_json,result_hash FROM research_benchmark_cache WHERE owner_id=? AND cache_key=?",(principal.user_id,cache_key)).fetchone()
                if row:
                    stored=load_payload(self.runtime, principal.user_id, "reports", row["result_json"])
                    if digest(stored)!=row["result_hash"]or stored!=candidate:fail("BENCHMARK_CACHE_INTEGRITY_FAILED")
                    candidate=stored
                else:con.execute("INSERT INTO research_benchmark_cache VALUES(?,?,?,?)",(principal.user_id,cache_key,stored_candidate,digest(candidate)))
            from copy import deepcopy
            result["benchmark"]=deepcopy(candidate)
            if basis_key(dataset)!=basis_key(bench):result['benchmark'].update(comparable=False,comparability_reason='NATIVE_PRICE_POLICY_OR_FROZEN_ANCHOR_BASIS_MISMATCH')
        if plan["kind"]=="strategy_validation" and result["family"]==FAMILY:
            strategy=result.get("strategy_reference")
            if strategy:
                strategy_summary={"kind":"sdk","id":strategy["strategy_id"],"label":f"{strategy['strategy_id']} · v{strategy['version']}",
                                  "version":strategy["version"],"instance_id":strategy["instance_id"]}
            else:
                sid=plan["parameters"]["strategy"]
                fallback={"buy_hold":"买入持有","sma20":"20 日均线","sma_cross_5_20":"5/20 日均线交叉"}
                strategy_summary={"kind":"builtin","id":sid,"label":fallback[sid]}
            securities=dataset["universe"]["tickers"]
            caption=securities[0] if len(securities)==1 else f"{len(securities)} 只证券"
            result.update(title=f"{caption} · {strategy_summary['label']}",strategy_summary=strategy_summary,
                          benchmarks=[],
                          currency=dataset.get("metadata",{}).get("currency"),
                          price_adjustment=dataset.get("metadata",{}).get("adjustment"))
            for selected in plan.get("benchmark_references",[]):
                context.require_active()
                ref=selected["dataset_reference"]
                record=self.datasets.read_accepted(principal.user_id,ref,parameters["acceptance_id"])
                # Verify the accepted original interval. Strategy dates may
                # fall outside this ETF's coverage; compare reports the gap.
                record=read_accepted(self.datasets,principal.user_id,ref,parameters["acceptance_id"],record)
                verify_views(record)
                result["benchmarks"].append(benchmark_proxies.compare(
                    record,selected["id"],plan["parameters"],result,active=context.require_active))
        if acquisition(dataset):
            result.setdefault('display_name','按复权价格模拟/研究')
            result.update(quantity_basis='ADJUSTED_SIMULATION_UNITS',native_acquisition=acquisition(dataset),native_caveat='固定快照的复权锚及源填充可能含后见信息；非历史实际股数/实际成交容量，不含额外现金分红，不证明PIT或无前视。')
        result.update(run_id=context.run.run_id,job_id=context.job_id,plan_hash=plan["plan_hash"],
                      dataset_reference=plan["dataset_reference"],task_kind=plan["kind"],cache_disposition=disposition)
        if "model_reference"in plan:result.update(model_reference=plan["model_reference"],model_profiles=[{k:p.get(k)for k in("profile_id","revision","sha256","role")}for p in plan["model_profiles"]])
        coverage=None
        if plan['kind']=='model_research':
            coverage={'scope':'THIS_RUN_ONLY','aggregate_complete':False,'result_rows_omitted':False,
                      'evidence_events_dropped':observation_events_dropped,'unknown_usage_possible':True}
            result['model_observations_status']=coverage
        encoded=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        def exceeds_original_cap(value):
            size=len(value.encode())
            return ((retained_bytes and retained_bytes+size>plan.get('search_limits',{}).get('max_result_bytes',8388608))
                    or size>32*1024**2)
        if coverage is not None and exceeds_original_cap(encoded):
            # Drop optional call facts, record incomplete coverage, then apply
            # original limits to the original result if even its note won't fit.
            coverage['result_rows_omitted']=True
            result.pop('model_observations',None)
            encoded=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            if exceeds_original_cap(encoded):
                result.pop('model_observations_status',None)
                encoded=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        if coverage is not None:
            try:self.retain_search_evidence(context,{'event':'MODEL_OBSERVATION_COVERAGE','coverage':coverage})
            except Exception:pass # absent coverage is UNKNOWN, never full totals
        if retained_bytes and retained_bytes+len(encoded.encode())>plan.get('search_limits',{}).get('max_result_bytes',8388608):fail('SEARCH_RESULT_BYTES_EXCEEDED')
        if len(encoded.encode())>32*1024**2:fail("TASK_RESULT_TOO_LARGE")
        context.require_active();context.checkpoint(95,{"stage":"SAVING_RESULT"})
        stored_report=store_payload(self.runtime, task_binding(context), "reports", encoded)
        with self.database.transaction()as con:
            context.assert_fenced(con);self.principal(principal.user_id)
            if "sdk_strategy" in plan:
                self._sdk_reference(principal,plan,connection=con)
            con.execute("INSERT INTO research_ux_results VALUES(?,?,?,?,?,?,?)",(context.run.run_id,context.job_id,principal.user_id,result["family"],stored_report,digest(result),now_text()))
        summary={k:result[k] for k in ("title","strategy_summary","currency") if k in result}
        return {"run_id":context.run.run_id,"family":result["family"],"result_url":"/api/v1/research/portfolio/results/"+context.run.run_id,
                "cache_disposition":disposition,**summary}

    def result(self,principal,run_id):
        self.runtime.require_owner(principal,principal.user_id)
        with closing(self.database.connect())as con:
            row=con.execute("SELECT r.* FROM research_ux_results r JOIN jobs j ON j.job_id=r.job_id WHERE r.owner_id=? AND r.run_id=? AND j.status='SUCCEEDED'",(principal.user_id,run_id)).fetchone()
            if not row or not active_result(con, principal.user_id, "tasks:"+run_id):
                raise PlatformError("NOT_FOUND",404)
        result=load_payload(self.runtime, principal.user_id, "reports", row["result_json"])
        if digest(result)!=row["result_hash"]:fail("TASK_RESULT_INTEGRITY_FAILED")
        return result

    def list_results(self,principal,limit=50,offset=0):
        self.runtime.require_owner(principal,principal.user_id)
        if type(limit)is not int or type(offset)is not int or not 1<=limit<=100 or offset<0:fail("TASK_PAGINATION_INVALID")
        with closing(self.database.connect())as con:
            unified=table_exists(con,"result_lifecycle")
            base=" FROM research_ux_results r JOIN jobs j ON j.job_id=r.job_id AND j.owner_id=r.owner_id"
            title="json_extract(j.result_json,'$.title')"
            if unified:
                base+=" LEFT JOIN result_lifecycle l ON l.owner_id=r.owner_id AND l.result_id='tasks:'||r.run_id"
                title="COALESCE(l.name,"+title+")"
            base+=" WHERE r.owner_id=? AND j.status='SUCCEEDED'"
            if unified:
                base+=" AND "+visible_clause("tasks","r.run_id","r.owner_id")
            currency="json_extract(j.result_json,'$.currency')"
            if isinstance(principal,AccountDelegation):
                title=currency="NULL"
            total=con.execute("SELECT count(*)"+base,(principal.user_id,)).fetchone()[0]
            rows=con.execute(f"SELECT r.run_id,r.job_id,r.family,r.created_at,{title} AS title,{currency} AS currency"+base+
                             " ORDER BY r.created_at DESC,r.run_id LIMIT ? OFFSET ?",(principal.user_id,limit,offset)).fetchall()
        return {"items":[{**dict(row),"result_url":"/api/v1/research/portfolio/results/"+row["run_id"]}for row in rows],
                "total":total,"limit":limit,"offset":offset}

    def status(self,principal,job_id):return self.runtime.jobs.get(principal,job_id)
    def _store(self):
        if self.credential_store is not None:return self.credential_store
        factory=getattr(self.runtime,"provider_credential_store_factory",None)
        if callable(factory):return factory()
        from finance_platform.operations.provider_credentials import ProviderCredentialStore
        registry=getattr(self.runtime,"model_capabilities",None)
        if registry is None:fail("MODEL_TRUSTED_TARGET_REGISTRY_REQUIRED")
        return ProviderCredentialStore(self.runtime.root,target_resolver=registry.resolve_target)
    def _target(self,principal,choice,*,expected_target):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        target=self._store().freeze_target(principal.user_id,choice["provider"],choice.get("endpoint_profile"))
        if expected_target!={key:getattr(target,key)for key in("provider","endpoint_id","base_url","target_sha256","owner_scope")}:fail("MODEL_SAVED_TARGET_CHANGED_RESAVE_CONFIGURATION")
        return target
    def _secret(self,principal,choice,*,target):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        ref=choice.get("secret_ref");provider=choice["provider"];endpoint=choice.get("endpoint_profile")
        if target.provider!=provider or target.endpoint_id!=endpoint:fail("MODEL_FROZEN_TARGET_MISMATCH")
        if self.credential_resolver is not None:return self.credential_resolver(principal.user_id,ref,target=target)
        if type(ref)is str and ref.startswith("credential:FA_"):
            return self._store().resolve_for_provider(principal.user_id,ref,target=target)
        if type(ref)is str and __import__("re").fullmatch(r"env:FA_[A-Z0-9_]{1,80}",ref):
            registry=getattr(self.runtime,"model_capabilities",None)
            if registry is None:fail("MODEL_TRUSTED_TARGET_REGISTRY_REQUIRED")
            registry.validate_target(choice,owner_id=principal.user_id,target=target)
            value=__import__("os").environ.get(ref[4:])
            if value:return value
        fail("MODEL_SECRET_NOT_CONFIGURED")
    def cancel(self,principal,job_id):return self.runtime.jobs.cancel(principal,job_id)
    def resume(self,principal,job_id,body):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        if type(body)is not dict or set(body)!={"idempotency_key"}:fail("TASK_RESUME_IDEMPOTENCY_REQUIRED")
        from finance_platform.core.contracts import identifier
        key=body["idempotency_key"];identifier(key)
        job=self.runtime.jobs.get(principal,job_id)
        if job["handler"]!="research.debate":fail("TASK_CHECKPOINT_RESUME_UNSUPPORTED_START_FRESH")
        stamp=digest({"job_id":job_id,"operation":"verified_ta_resume"})
        with self.database.transaction()as con:
            old=con.execute("SELECT * FROM research_ux_resume_commands WHERE owner_id=? AND idempotency_key=?",(principal.user_id,key)).fetchone()
            if old:
                if old["request_hash"]!=stamp:raise PlatformError("CONFLICT",409)
                if old["state"]=="DONE":return self.runtime.jobs.get(principal,old["job_id"])
                fail("TASK_RESUME_COMMAND_UNCERTAIN_CHECK_JOB_STATUS")
            con.execute("INSERT INTO research_ux_resume_commands VALUES(?,?,?,'PENDING',NULL)",(principal.user_id,key,stamp))
        from .service import ResearchService
        result=ResearchService(self.runtime).debate_service().retry(principal,job_id)
        with self.database.transaction()as con:
            con.execute("UPDATE research_ux_resume_commands SET state='DONE',job_id=? WHERE owner_id=? AND idempotency_key=?",(result["job_id"],principal.user_id,key))
        # A crash between Core retry and this receipt leaves PENDING; a replay
        # explicitly refuses uncertainty rather than creating duplicate work.
        return result


def register_handlers(registry,runtime,*,service=None):
    service=service or ResearchTaskService(runtime)
    def execute(context,parameters):
        from .portfolio import PortfolioError
        try:return service.execute(context,parameters)
        except PortfolioError as error:
            # Only this known input rejection crosses the trusted handler
            # boundary. Never forward arbitrary exception text or .code.
            if type(error)is PortfolioError and error.code=='BENCHMARK_RESERVED_BUDGET_EXCEEDS_INITIAL_CAPITAL':
                raise PlatformError('BENCHMARK_RESERVED_BUDGET_EXCEEDS_INITIAL_CAPITAL',422)from None
            raise
    def validate(parameters):
        if type(parameters)is not dict or set(parameters)!={"plan_reference","acceptance_id"}:fail("TASK_JOB_PARAMETERS_INVALID")
    registry.register("research.dataset_task",execute,validate=validate,recoverable=False,auto_recovery_limit=0,allowed_modes=(RunMode.BACKTEST,),resolve_versions=service.resolve)
    return service

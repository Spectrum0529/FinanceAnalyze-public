"""Immutable ordinal result pages plus recoverable owner management metadata."""
from .payload_storage import persist as store_payload, hydrate as load_payload, task_binding
from contextlib import closing
import json
from decimal import Decimal
import hashlib

from finance_platform.core.auth import AccountDelegation
from finance_platform.core.contracts import fingerprint, identifier
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import safe_json, now_text
from .daily import DailyError, RESULT_FAMILY

PAGED_FAMILIES = {"daily-simulation-result-2", RESULT_FAMILY}
PAGE_SIZE = 100
SERIES = ("equity_curve", "trades", "decisions", "orders", "rejections", "unsubmitted_outputs")
MIGRATIONS = ("""
CREATE TABLE research_daily_result_pages(
 run_id TEXT NOT NULL REFERENCES research_daily_results(run_id), kind TEXT NOT NULL,
 page_index INTEGER NOT NULL, items_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 PRIMARY KEY(run_id,kind,page_index));
CREATE TABLE research_daily_result_management(
 run_id TEXT PRIMARY KEY REFERENCES research_daily_results(run_id), owner_id TEXT NOT NULL REFERENCES users(user_id),
 name TEXT NOT NULL, deleted INTEGER NOT NULL CHECK(deleted IN(0,1)),
 revision INTEGER NOT NULL CHECK(revision>=0), updated_at TEXT NOT NULL);
CREATE TRIGGER research_daily_management_identity BEFORE UPDATE ON research_daily_result_management
 WHEN NEW.run_id!=OLD.run_id OR NEW.owner_id!=OLD.owner_id BEGIN SELECT RAISE(ABORT,'immutable management identity'); END;
CREATE TABLE research_daily_result_actions(
 owner_id TEXT NOT NULL REFERENCES users(user_id), idempotency_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES research_daily_results(run_id),
 response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(owner_id,idempotency_key));
""",)


def overview(curve, maximum=500):
    """Whole-range time buckets retain extrema and endpoints in O(n)."""
    count = len(curve)
    if count <= maximum:
        indices = list(range(count))
    else:
        selected = {0, count-1}
        buckets = (maximum-2)//2
        for bucket in range(buckets):
            start = 1+(count-2)*bucket//buckets
            end = 1+(count-2)*(bucket+1)//buckets
            low = high = start
            for index in range(start+1,end):
                if Decimal(curve[index]["equity"]) < Decimal(curve[low]["equity"]): low=index
                if Decimal(curve[index]["equity"]) >= Decimal(curve[high]["equity"]): high=index
            selected.update((low,high))
        indices = sorted(selected)
    return {"items":[{"ordinal":i,**curve[i]} for i in indices], "source_total":count,
            "sampled":count>maximum,"method":"TIME_BUCKET_MIN_MAX_EQUITY_WITH_ENDPOINTS",
            "maximum_points":maximum,"metrics_basis":"FULL_SERIES",
            "full_range":{"first":None if not curve else curve[0]["at"],"last":None if not curve else curve[-1]["at"]}}


class DailyResultStore:
    def __init__(self, runtime, *, gateway=None):
        self.runtime,self.database=runtime,runtime.database
        self.gateway=gateway
        self.database.apply_module_migrations("research_daily_management2",MIGRATIONS)
        with self.database.transaction() as con:
            for table,keys in (("research_daily_result_pages",("run_id","kind","page_index")),("research_daily_result_actions",("owner_id","idempotency_key"))):
                immutable(con,table,keys)

    @staticmethod
    def page_input(limit,offset):
        if type(limit)is not int or not 1<=limit<=1000 or type(offset)is not int or not 0<=offset<=2147483647:
            raise PlatformError("INVALID_INPUT",422)

    def save(self, context, result):
        """Split large arrays; metadata safe_json bound is never widened."""
        metadata={k:v for k,v in result.items() if k not in SERIES}
        metadata["report"]={k:v for k,v in result["report"].items() if k not in SERIES}
        metadata["final"]={k:v for k,v in result["final"].items() if k!="orders"}
        metadata["overview"]=overview(result["equity_curve"])
        metadata["series_manifest"]={}
        pages=[]
        for kind in SERIES:
            items=result.get(kind,[])
            hashes=[]
            for start in range(0,len(items),PAGE_SIZE):
                context.require_active()
                raw=safe_json(items[start:start+PAGE_SIZE])
                digest=fingerprint(json.loads(raw))
                hashes.append(digest)
                pages.append((context.run.run_id,kind,start//PAGE_SIZE,store_payload(self.runtime, task_binding(context), "reports", raw),digest))
            metadata["series_manifest"][kind]={"total":len(items),"page_size":PAGE_SIZE,"page_hashes":hashes}
        encoded=safe_json(metadata)
        stored_metadata=store_payload(self.runtime, task_binding(context), "reports", encoded)
        with self.database.transaction() as con:
            context.assert_fenced(con)
            self.runtime.require_owner(context.principal,context.run.owner_id,write=True)
            con.execute("INSERT INTO research_daily_results VALUES(?,?,?,?,?,?,?,?,?)",(context.run.run_id,context.run.owner_id,context.job_id,result["ticker"],result["provider"],result["strategy"],stored_metadata,fingerprint(json.loads(encoded)),now_text()))
            con.executemany("INSERT INTO research_daily_result_pages VALUES(?,?,?,?,?)",pages)
            if "history_reference" in result:
                if self.gateway is None:raise DailyError("DAILY_HISTORY_PIN_UNAVAILABLE")
                history=result["history_reference"]
                self.gateway.pin(context.run.owner_id,history["history_id"],history["version"],context.run.run_id,kind="result",connection=con,context=context)
        return metadata

    def _row(self, principal, run_id, *, include_deleted=False, connection=None):
        if type(include_deleted)is not bool:raise PlatformError("INVALID_INPUT",422)
        def get(con):
            row=con.execute("SELECT d.*,j.status,COALESCE(m.name,d.ticker||' · '||d.strategy) AS display_name,COALESCE(m.deleted,0) AS deleted,COALESCE(m.revision,0) AS revision FROM research_daily_results d JOIN jobs j ON j.job_id=d.job_id LEFT JOIN research_daily_result_management m ON m.run_id=d.run_id AND m.owner_id=d.owner_id WHERE d.run_id=? AND d.owner_id=?",(run_id,principal.user_id)).fetchone()
            if row is None or row["status"]!="SUCCEEDED" or (row["deleted"] and not include_deleted):raise PlatformError("NOT_FOUND",404)
            return row
        if connection is not None:return get(connection)
        with closing(self.database.connect()) as con:return get(con)

    def checked(self, row):
        value=load_payload(self.runtime, row["owner_id"], "reports", row["result_json"])
        if fingerprint(value)!=row["result_hash"]:raise DailyError("DAILY_RESULT_HASH_MISMATCH")
        return value

    def result(self, principal, run_id, *, include_deleted=False):
        row=self._row(principal,run_id,include_deleted=include_deleted)
        value=self.checked(row)
        stored_family=value.get("family",value.get("schema_version"))
        if stored_family not in PAGED_FAMILIES:
            # Old full JSON stays byte/hash immutable. Only its read projection
            # changes; exact old arrays remain available through series().
            full=value
            value={k:v for k,v in full.items() if k not in SERIES}
            value["report"]={k:v for k,v in full["report"].items() if k not in SERIES}
            value["final"]={k:v for k,v in full["final"].items() if k!="orders"}
            value["overview"]=overview(full["equity_curve"])
            value["series_manifest"]={k:{"total":len(full.get(k,[])),"legacy_projection":True} for k in SERIES}
        value.update(name=row["display_name"],deleted=bool(row["deleted"]),revision=row["revision"],stored_family=stored_family,
                     view_schema="daily-result-summary-2",stored_result_hash=row["result_hash"])
        return value

    def series(self,principal,run_id,kind="equity_curve",limit=500,offset=0,*,include_deleted=False):
        self.page_input(limit,offset)
        if kind not in SERIES:raise PlatformError("INVALID_INPUT",422)
        row=self._row(principal,run_id,include_deleted=include_deleted)
        value=self.checked(row)
        if value.get("family") not in PAGED_FAMILIES:
            items=value.get(kind,[])
            return {"run_id":run_id,"kind":kind,"items":items[offset:offset+limit],"total":len(items),"limit":limit,"offset":offset}
        manifest=value["series_manifest"][kind]
        total=manifest["total"]
        end=min(offset+limit,total)
        items=[]
        if offset<end:
            first,last=offset//PAGE_SIZE,(end-1)//PAGE_SIZE
            with closing(self.database.connect()) as con:
                pages=con.execute("SELECT * FROM research_daily_result_pages WHERE run_id=? AND kind=? AND page_index BETWEEN ? AND ? ORDER BY page_index",(run_id,kind,first,last)).fetchall()
            if len(pages)!=last-first+1:raise DailyError("DAILY_RESULT_HASH_MISMATCH")
            for index,page in enumerate(pages,first):
                part=load_payload(self.runtime, row["owner_id"], "reports", page["items_json"])
                digest=fingerprint(part)
                expected_length=min(PAGE_SIZE,total-index*PAGE_SIZE)
                if page["page_index"]!=index or len(part)!=expected_length or digest!=page["content_hash"] or digest!=manifest["page_hashes"][index]:raise DailyError("DAILY_RESULT_HASH_MISMATCH")
                items.extend(part)
            items=items[offset-first*PAGE_SIZE:offset-first*PAGE_SIZE+(end-offset)]
        return {"run_id":run_id,"kind":kind,"items":items,"total":total,"limit":limit,"offset":offset}

    def list(self,principal,limit=50,offset=0,state="active"):
        self.runtime.jobs._paging(limit,offset)
        if state not in {"active","deleted","all"}:raise PlatformError("INVALID_INPUT",422)
        deleted="" if state=="all" else " AND COALESCE(m.deleted,0)="+("1" if state=="deleted" else "0")
        with closing(self.database.connect()) as con:
            base="FROM research_daily_results d JOIN jobs j ON j.job_id=d.job_id LEFT JOIN research_daily_result_management m ON m.run_id=d.run_id AND m.owner_id=d.owner_id WHERE d.owner_id=? AND j.status='SUCCEEDED'"+deleted
            total=con.execute("SELECT count(*) "+base,(principal.user_id,)).fetchone()[0]
            rows=con.execute("SELECT d.*,COALESCE(m.name,d.ticker||' · '||d.strategy) AS display_name,COALESCE(m.deleted,0) AS deleted,COALESCE(m.revision,0) AS revision "+base+" ORDER BY d.created_at DESC,d.run_id LIMIT ? OFFSET ?",(principal.user_id,limit,offset)).fetchall()
        items=[]
        for row in rows:
            value=self.checked(row)
            hist=value.get("history_reference",{})
            engine_fields={"family":value.get("family"),"engine":value.get("engine"),"engine_version":value.get("engine_version")}
            items.append({**{k:row[k] for k in ("run_id","job_id","ticker","provider","strategy","result_hash","created_at","revision")},**engine_fields,"name":row["display_name"],"deleted":bool(row["deleted"]),"history_id":hist.get("history_id"),"history_version":hist.get("version"),"row_count":value.get("series_manifest",{}).get("equity_curve",{}).get("total",len(value.get("equity_curve",[]))),"coverage":value.get("coverage",{})})
        return {"items":items,"total":total,"limit":limit,"offset":offset,"state":state}

    def lifecycle(self,principal,run_id,*,action,expected_revision,idempotency_key,name=None):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        try:identifier(idempotency_key)
        except ValueError:raise PlatformError("INVALID_INPUT",422) from None
        if action not in {"rename","delete","restore"} or type(expected_revision)is not int or not 0<=expected_revision<=2147483647:raise PlatformError("INVALID_INPUT",422)
        if action=="rename":
            if type(name)is not str or not 1<=len(name.strip())<=120 or any(ord(c)<32 for c in name):raise PlatformError("INVALID_INPUT",422)
            name=name.strip()
        elif name is not None:raise PlatformError("INVALID_INPUT",422)
        request_hash=fingerprint({"run_id":run_id,"action":action,"name":name,"expected_revision":expected_revision})
        with self.database.transaction() as con:
            if isinstance(principal, AccountDelegation):
                principal.check_approval(con)
            row=self._row(principal,run_id,include_deleted=True,connection=con)
            old=con.execute("SELECT * FROM research_daily_result_actions WHERE owner_id=? AND idempotency_key=?",(principal.user_id,idempotency_key)).fetchone()
            if old:
                if old["request_hash"]!=request_hash:raise PlatformError("CONFLICT",409)
                self._row(principal,run_id,include_deleted=True,connection=con)
                return json.loads(old["response_json"])
            if row["revision"]!=expected_revision:raise PlatformError("CONFLICT",409)
            current_name=name if action=="rename" else row["display_name"]
            deleted=action=="delete" or bool(row["deleted"]) and action!="restore"
            revision=expected_revision+1
            con.execute("INSERT INTO research_daily_result_management VALUES(?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET name=excluded.name,deleted=excluded.deleted,revision=excluded.revision,updated_at=excluded.updated_at",(run_id,principal.user_id,current_name,int(deleted),revision,now_text()))
            response={"run_id":run_id,"name":current_name,"deleted":deleted,"revision":revision}
            con.execute("INSERT INTO research_daily_result_actions VALUES(?,?,?,?,?,?)",(principal.user_id,idempotency_key,request_hash,run_id,safe_json(response),now_text()))
            self.database.audit(principal.user_id,"daily.result_"+action,run_id,{"revision":revision},connection=con)
        return response

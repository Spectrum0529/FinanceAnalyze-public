"""One metadata index over saved reports; read/export stay with their owners."""
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date
from hashlib import sha256
import json
import re
from .auth import AccountDelegation
from .errors import PlatformError
from .mcp_data_policy import ReportDataPolicy, local_only
from .pagination import validate_pagination
from .mcp_grants import permits
from .result_lifecycle import (
    BatchLifecycleCommand, LifecycleCommand, LifecycleTarget, RESULT_ID, SOURCES,
    ResultLifecycle, active_result, table_exists,
)

KINDS={'all','backtest','research','ai','model','imported'}
LABELS = {'backtest': '回测', 'research': '研究', 'ai': 'AI研究', 'model': '模型训练', 'imported': '迁入报告'}
_LABEL_SQL = "CASE kind WHEN 'backtest' THEN '回测' WHEN 'research' THEN '研究' WHEN 'ai' THEN 'AI研究' ELSE '模型训练' END"
NAME_SEARCH_LIMIT = 1000


@dataclass(frozen=True)
class ResultSource:
    name:str
    read:object
    export:object=None
    series:object=None
    mcp_policy:object=None
    inspect:object=None


class UnifiedResultIndex:
    def __init__(self,database,*,research=None,daily=None,tasks=None,jobs=None,autonomous=None,migration=None,report_storage=None):
        self.database=database
        self.sources={s.name:s for s in (research,daily,tasks,jobs,autonomous,migration) if s is not None}
        if set(self.sources)-set(SOURCES):raise PlatformError('INVALID_INPUT')
        self.lifecycle = ResultLifecycle(self)
        self.report_storage = report_storage
        from .result_views import ResultViews
        self.views = ResultViews(self)

    def _metadata(self,con):
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        selects=[]
        if "migration" in self.sources and "onboarding_migrated_objects" in tables:
            selects.append("""SELECT 'migration' AS source,archive_id AS run_id,'' AS job_id,created_at,
                'imported' AS kind,'迁入的回测报告' AS title,owner_id
                FROM onboarding_migrated_objects WHERE kind='report' AND archive_id IS NOT NULL""")
        if 'research' in self.sources and 'research_results'in tables:
            selects.append("""SELECT 'research' AS source,r.run_id,r.job_id,r.created_at,
              CASE WHEN json_extract(r.result_json,'$.family')='tradingagents-native-report-1' THEN 'ai'
                   WHEN json_extract(r.result_json,'$.family')='qlib-workflow-result-1' THEN 'model'
                   WHEN j.handler LIKE '%factor%' OR j.handler LIKE '%search%' THEN 'research'
                   WHEN j.handler LIKE '%backtest%' OR j.handler LIKE '%portfolio%' THEN 'backtest'
                   ELSE 'research' END AS kind,
              COALESCE(json_extract(r.result_json,'$.title'),json_extract(r.result_json,'$.ticker'),j.handler) AS title,
              r.owner_id FROM research_results r JOIN jobs j ON j.job_id=r.job_id AND j.owner_id=r.owner_id
              WHERE j.status='SUCCEEDED'""")
        if 'daily' in self.sources and 'research_daily_results'in tables:
            selects.append("""SELECT 'daily' AS source,d.run_id,d.job_id,d.created_at,'backtest' AS kind,
              d.ticker||' · '||d.strategy AS title,d.owner_id FROM research_daily_results d
              JOIN jobs j ON j.job_id=d.job_id AND j.owner_id=d.owner_id WHERE j.status='SUCCEEDED'""")
        if 'tasks' in self.sources and 'research_ux_results'in tables:
            selects.append("""SELECT 'tasks' AS source,u.run_id,u.job_id,u.created_at,
              CASE WHEN u.family='dataset-portfolio-result-1' THEN 'backtest'
                   WHEN u.family='model-tool-research-result-1' THEN 'ai' ELSE 'research' END AS kind,
              COALESCE(
                CASE WHEN json_valid(u.result_json) THEN
                  CASE WHEN json_type(u.result_json,'$.title')='text'
                         AND length(json_extract(u.result_json,'$.title')) <= 256
                         AND instr(json_extract(u.result_json,'$.title'),char(0))=0
                       THEN NULLIF(trim(json_extract(u.result_json,'$.title'),char(9,10,11,12,13,32)), '')
                  END
                END,
                CASE WHEN u.family='dataset-portfolio-result-1' THEN '数据集策略回测'
                     WHEN u.family='model-tool-research-result-1' THEN '模型与实际工具报告'
                     WHEN u.family='factor-validation-result-1' THEN '因子验证' ELSE '数据集研究' END
              ) AS title,
              u.owner_id FROM research_ux_results u JOIN jobs j ON j.job_id=u.job_id AND j.owner_id=u.owner_id
              WHERE j.status='SUCCEEDED'""")
        if 'autonomous' in self.sources and {'autonomous_sessions','autonomous_attempts','autonomous_artifacts','jobs'}<=tables:
            selects.append("""SELECT 'autonomous' AS source,a.attempt_id AS run_id,a.job_id,a.updated_at AS created_at,
              'backtest' AS kind,COALESCE(json_extract(s.spec_json,'$.name'),'自主研究')||' · 第'||a.ordinal||'轮'||
              CASE WHEN a.phase='final_test' THEN '独立评价' WHEN json_extract(s.spec_json,'$.reuse_policy')='evaluate_saved'
              THEN '固定候选回测' ELSE '研究报告' END AS title,s.owner_id
              FROM autonomous_attempts a JOIN autonomous_sessions s ON s.session_id=a.session_id
              JOIN jobs j ON j.job_id=a.job_id AND j.run_id=a.run_id AND j.owner_id=s.owner_id
              WHERE a.status='SUCCEEDED' AND j.status='SUCCEEDED' AND EXISTS(
                SELECT 1 FROM autonomous_artifacts f WHERE f.attempt_id=a.attempt_id AND f.session_id=a.session_id
                AND f.kind='evaluation' AND f.media_type='application/json' AND f.part_index=0 AND f.part_count>0
                AND f.logical_id=CASE WHEN a.phase='final_test' THEN 'sealed-final-evaluation' ELSE 'full-experiment' END)""")
        if 'jobs' in self.sources and 'jobs' in tables:
            exclusions=''.join(' AND NOT EXISTS(SELECT 1 FROM '+table+' saved WHERE saved.job_id=j.job_id AND saved.owner_id=j.owner_id)'
                for table in ('research_results','research_daily_results','research_ux_results')if table in tables)
            if 'autonomous' in self.sources and {'autonomous_attempts', 'autonomous_sessions'} <= tables:
                exclusions += """ AND NOT EXISTS(SELECT 1 FROM autonomous_attempts a
                    JOIN autonomous_sessions s USING(session_id)
                    WHERE j.job_id=a.job_id AND s.owner_id=j.owner_id)"""
            selects.append("""SELECT 'jobs' AS source,j.run_id,j.job_id,j.updated_at AS created_at,
              CASE WHEN j.handler LIKE '%model%' OR j.handler LIKE '%train%' THEN 'model'
                   WHEN j.handler LIKE '%debate%' THEN 'ai'
                   WHEN j.handler LIKE '%backtest%' OR j.handler LIKE '%portfolio%' THEN 'backtest'
                   ELSE 'research' END AS kind,j.handler AS title,j.owner_id FROM jobs j
              WHERE j.status='SUCCEEDED' AND j.result_json IS NOT NULL AND j.handler LIKE 'research.%'"""+exclusions)
        return ' UNION ALL '.join(selects)

    def _reports(self, con):
        metadata = self._metadata(con)
        if not metadata:
            return None
        daily = table_exists(con, "research_daily_result_management")
        daily_join = ("LEFT JOIN research_daily_result_management d ON m.source='daily' "
                      "AND d.owner_id=m.owner_id AND d.run_id=m.run_id") if daily else ""
        names = "l.name,d.name," if daily else "l.name,"
        flags = "l.deleted,d.deleted,0" if daily else "l.deleted,0"
        revisions = "l.revision,d.revision,0" if daily else "l.revision,0"
        return f"""WITH metadata AS ({metadata}), reports AS (
            SELECT m.*,m.source||':'||m.run_id AS result_id, COALESCE({names}m.title,'结果') AS name,
                   COALESCE({flags}) AS deleted,COALESCE({revisions}) AS revision
            FROM metadata m LEFT JOIN result_lifecycle l ON m.source!='daily'
              AND l.owner_id=m.owner_id AND l.result_id=m.source||':'||m.run_id
            {daily_join}
        ) """

    def _report_row(self, con, owner_id, result_id):
        self._source(result_id)
        cte = self._reports(con)
        row = None if cte is None else con.execute(
            cte + "SELECT * FROM reports WHERE owner_id=? AND result_id=?", (owner_id, result_id)
        ).fetchone()
        if row is None:
            raise PlatformError('NOT_FOUND', 404)
        return row

    @staticmethod
    def _can_manage(principal):
        return (principal.role != "viewer" and (not isinstance(principal, AccountDelegation)
                or permits(getattr(principal, "mcp_scope", None), "manage")))

    @staticmethod
    def _browse_names(principal):
        return isinstance(principal, AccountDelegation) and permits(getattr(principal, "mcp_scope", None), "browse")

    def _name_authorized(self, principal, row, *, inspect_deleted=False):
        if not self._browse_names(principal) or self.sources[row["source"]].mcp_policy is None:
            return False
        try:
            self.views._version(principal, row["result_id"], inspect_deleted=inspect_deleted or bool(row["deleted"]))
        except (PlatformError, ValueError):
            return False
        return True

    def _item(self, principal, row, *, verified_name=False):
        result_id = row["result_id"]
        source = self.sources[row["source"]]
        delegated = isinstance(principal, AccountDelegation)
        deleted = bool(row["deleted"])
        name = LABELS[row["kind"]] if delegated and not verified_name else row["name"]
        applicable_policy = (source.mcp_policy is not None
                             and (source.name in {"tasks", "research"}
                                  or permits(getattr(principal, "mcp_scope", None), "browse")))
        can_read = not deleted and (not delegated or verified_name)
        read_state = ("unavailable" if deleted else "available" if can_read
                      else "check_required" if applicable_policy else "unavailable")
        reason = ("RESULT_IN_TRASH" if deleted else
                  ("RESULT_SOURCE_CHECK_REQUIRED" if applicable_policy else "MCP_LOCAL_DATA_ONLY")
                  if delegated and not verified_name else None)
        return {key: row[key] for key in ('run_id','job_id','created_at','kind','source')} | {
            'result_id': result_id, 'title': name if delegated else f"{LABELS[row['kind']]} · {name}",
            'name': name, 'status': 'IMPORTED' if source.name == 'migration' else 'SUCCEEDED',
            'deleted': deleted, 'revision': row["revision"],
            'can_read': can_read, 'can_manage': self._can_manage(principal),
            'can_export': can_read and (delegated or source.export is not None), 'reason_code': reason,
            'read_state': read_state,
            'export_state': ('available' if can_read and (delegated or source.export is not None) else
                             'check_required' if read_state == 'check_required' else 'unavailable'),
            'report_url': '/api/v1/results/' + result_id, 'export_url': '/api/v1/results/' + result_id + '/export',
        }

    def list(self,principal,kind='all',limit=50,offset=0,*,state='active',q=None,source='all',from_date=None,to_date=None):
        validate_pagination(limit,offset)
        if (kind not in KINDS or state not in {'active','deleted','all'} or source not in ('all', *SOURCES)
                or q is not None and (type(q) is not str or len(q) > 120 or any(ord(c) < 32 for c in q))):
            raise PlatformError('INVALID_INPUT',422)
        try:
            for value in (from_date,to_date):
                if value is not None and (type(value) is not str or date.fromisoformat(value).isoformat() != value):
                    raise ValueError
            if from_date and to_date and from_date > to_date:
                raise ValueError
        except ValueError:
            raise PlatformError("INVALID_INPUT", 422) from None
        with closing(self.database.connect()) as con:
            con.execute("BEGIN")
            cte = self._reports(con)
            if cte is None:
                return {'items':[],'total':0,'limit':limit,'offset':offset}
            parts = ['owner_id=?']
            args = [principal.user_id]
            if isinstance(principal, AccountDelegation):
                parts.append("source!='migration'")
            for column, value in [('kind', kind), ('source', source), ('deleted', {'active':0,'deleted':1,'all':'all'}[state])]:
                if value != 'all':
                    parts.append(column + '=?')
                    args.append(value)
            upgraded_search = bool(q and q.strip() and self._browse_names(principal))
            if q and q.strip() and not upgraded_search:
                title = _LABEL_SQL if isinstance(principal, AccountDelegation) else 'name'
                fields = (title, 'result_id', 'run_id', 'job_id')
                parts.append('(' + ' OR '.join(f'instr(lower({field}),lower(?))>0' for field in fields) + ')')
                args.extend([q.strip()] * len(fields))
            for value, op in [(from_date, '>='), (to_date, '<=')]:
                if value:
                    parts.append(f"substr(created_at,1,10){op}?")
                    args.append(value)
            where = ' AND '.join(parts)
            total=con.execute(cte+'SELECT count(*) FROM reports WHERE '+where,args).fetchone()[0]
            if upgraded_search:
                if total > NAME_SEARCH_LIMIT:
                    raise PlatformError("RESULT_SEARCH_REFINEMENT_REQUIRED", 422)
                rows = con.execute(cte + "SELECT * FROM reports WHERE " + where + " ORDER BY created_at DESC,source,run_id", args)
                items, matches = self._search_names(principal, rows, q.strip().casefold(), limit, offset)
                return {'items':items,'total':matches,'limit':limit,'offset':offset}
            rows=con.execute(cte+'SELECT * FROM reports WHERE '+where+' ORDER BY created_at DESC,source,run_id LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
        return {'items':[self._item(principal, row, verified_name=self._name_authorized(principal, row)) for row in rows],
                'total':total,'limit':limit,'offset':offset}

    def _search_names(self, principal, rows, needle, limit, offset):
        items, matches = [], 0
        for row in rows:
            verified = self._name_authorized(principal, row)
            name = row["name"] if verified else LABELS[row["kind"]]
            fields = (name, LABELS[row["kind"]], row["result_id"], row["run_id"], row["job_id"])
            if not any(needle in value.casefold() for value in fields):
                continue
            if offset <= matches < offset + limit:
                items.append(self._item(principal, row, verified_name=verified))
            matches += 1
        return items, matches

    def management(self, principal, result_id):
        self._require_migration_owner(principal, result_id)
        with closing(self.database.connect()) as con:
            row = self._report_row(con, principal.user_id, result_id)
        item = self._item(principal, row, verified_name=self._name_authorized(principal, row))
        if item["read_state"] == "check_required":
            if self._browse_names(principal):
                item.update(read_state="unavailable", export_state="unavailable", reason_code="MCP_LOCAL_DATA_ONLY")
                return item
            try:
                self.read(principal, result_id)
            except PlatformError as error:
                item.update(read_state="unavailable", export_state="unavailable", reason_code=error.code)
            else:
                item.update(can_read=True, can_export=True, read_state="available", export_state="available", reason_code=None)
        return item

    def change_lifecycle(self, principal, result_id, command):
        self._require_migration_owner(principal, result_id)
        self._source(result_id)
        if not self._can_manage(principal):
            raise PlatformError("FORBIDDEN", 403)
        target = LifecycleTarget(result_id=result_id, expected_revision=command.expected_revision)
        return self.lifecycle.change(principal, action=command.action, targets=[target],
                                     idempotency_key=command.idempotency_key, name=command.name)

    def batch_lifecycle(self, principal, command):
        if not self._can_manage(principal):
            raise PlatformError("FORBIDDEN", 403)
        for target in command.targets:
            self._require_migration_owner(principal, target.result_id)
        return self.lifecycle.change(principal, action=command.action, targets=command.targets,
                                     idempotency_key=command.idempotency_key, batch=True)

    def _source(self,result_id):
        if type(result_id)is not str or not RESULT_ID.fullmatch(result_id):
            raise PlatformError('NOT_FOUND',404)
        source,run=result_id.split(':',1)
        if source not in self.sources:raise PlatformError('NOT_FOUND',404)
        return self.sources[source],run

    @staticmethod
    def _require_migration_owner(principal, result_id):
        if (isinstance(principal, AccountDelegation) and isinstance(result_id, str)
                and result_id.startswith("migration:")):
            raise PlatformError("NOT_FOUND", 404)

    def read(self,principal,result_id):
        self._require_migration_owner(principal, result_id)
        source,run=self._source(result_id)
        self._visible(principal,source.name,run)
        return self._read_source(principal, source, run)

    def _read_for_metadata(self, principal, result_id):
        source, run = self._source(result_id)
        with closing(self.database.connect()) as con:
            self._report_row(con, principal.user_id, result_id)
        if source.inspect is None:
            raise local_only()
        return self._read_source(principal, source, run, reader=source.inspect)

    def _read_source(self, principal, source, run, *, reader=None):
        # The owning service preserves status, owner, deletion, hash and data rights.
        delegated = isinstance(principal, AccountDelegation)
        if delegated and (source.mcp_policy is None or source.name not in {"research", "tasks"}
                          and not permits(getattr(principal, "mcp_scope", None), "browse")):
            raise local_only()
        result = (reader or source.read)(principal,run)
        if delegated:
            projection = source.mcp_policy(principal, run, result)
            if projection is not None:
                result = projection
            if source.name == "daily":
                result = {key: value for key, value in result.items() if key not in {"name", "deleted", "revision"}}
        return result

    def view_identity(self, principal, result_id, *, inspect_deleted=False):
        """Check the owner and current immutable payload without decoding its report."""
        source, run_id = self._source(result_id)
        table = {"tasks": "research_ux_results", "research": "research_results"}.get(source.name)
        if table is None:
            return None
        with closing(self.database.connect()) as con:
            con.execute("BEGIN")
            if not inspect_deleted and not active_result(con, principal.user_id, result_id):
                raise PlatformError("NOT_FOUND", 404)
            if "result_hash" not in {field["name"] for field in con.execute(f"PRAGMA table_info({table})")}:
                return None
            row = con.execute(f"""SELECT r.result_json,r.result_hash FROM {table} r JOIN jobs j
                                 ON j.job_id=r.job_id AND j.owner_id=r.owner_id AND j.status='SUCCEEDED'
                                 WHERE r.owner_id=? AND r.run_id=?""", (principal.user_id, run_id)).fetchone()
        if row is None or type(row["result_hash"]) is not str:
            return None
        encoded = row["result_json"].encode()
        persisted_hash = sha256(encoded).hexdigest()
        if b'"storage_schema":"research-payload-envelope-1"' in encoded:
            if self.report_storage is None or len(encoded) > 16384:
                return None
            envelope = json.loads(encoded)
            if envelope.get("storage_schema") != "research-payload-envelope-1":
                return None
            persisted_hash = (persisted_hash, self.report_storage(principal.user_id, envelope["reference"]))
        scope = getattr(principal, "mcp_scope", None)
        authorization_kind = ("mcp", scope) if isinstance(principal, AccountDelegation) else ("owner", principal.role)
        return principal.user_id, result_id, authorization_kind, row["result_hash"], persisted_hash

    def reauthorize_view(self, principal, result_id, content):
        if isinstance(principal, AccountDelegation):
            source, run_id = self._source(result_id)
            if source.mcp_policy is None:
                raise local_only()
            source.mcp_policy(principal, run_id, content)

    def export(self,principal,result_id):
        if isinstance(principal, AccountDelegation):
            raise local_only()
        source,run=self._source(result_id)
        self._visible(principal,source.name,run)
        if source.export is None:raise PlatformError('RESULT_EXPORT_UNAVAILABLE',422)
        return source.export(principal,run)

    def _visible(self,principal,source,run):
        with closing(self.database.connect()) as con:
            if self._report_row(con, principal.user_id, f"{source}:{run}")["deleted"]:
                raise PlatformError('NOT_FOUND',404)


def create_router(runtime,*,research_service=None,daily_service=None,task_service=None):
    from fastapi import APIRouter,Request,Query
    from fastapi.responses import Response
    def encode_report(value):
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>8*1024**2:raise PlatformError('RESULT_EXPORT_TOO_LARGE',413)
        return raw
    def export_daily(principal,run):
        value=daily_service.result(principal,run);series={};size=0
        for kind,manifest in value.get('series_manifest',{}).items():
            total=manifest['total']
            if type(total)is not int or not 0<=total<=200000:raise PlatformError('RESULT_EXPORT_TOO_LARGE',413)
            rows=[]
            for offset in range(0,total,500):
                page=daily_service.series(principal,run,kind,500,offset)
                if page['total']!=total:raise PlatformError('RESULT_EXPORT_CHANGED',409)
                part=page['items'];size+=len(json.dumps(part,ensure_ascii=False,allow_nan=False).encode())
                if size>8*1024**2:raise PlatformError('RESULT_EXPORT_TOO_LARGE',413)
                rows.extend(part)
            if len(rows)!=total:raise PlatformError('RESULT_EXPORT_CHANGED',409)
            series[kind]=rows
        return encode_report({'run_id':run,'report':value,'series':series})
    def job_report(principal,run):
        with closing(runtime.database.connect())as con:
            row=con.execute("SELECT job_id FROM jobs WHERE owner_id=? AND run_id=? AND status='SUCCEEDED' AND handler LIKE 'research.%'",(principal.user_id,run)).fetchone()
        if row is None:raise PlatformError('NOT_FOUND',404)
        return runtime.jobs.get(principal,row['job_id'])['result']
    def research_mcp_policy(principal, run, report):
        try:
            if report.get("family") in {"tradingagents-native-report-1", "qlib-workflow-result-1"}:
                from .mcp_research import native_report_projection
                return native_report_projection(research_service, principal, run, report)
            research_service.require_export_rights(principal, run, report)
            ReportDataPolicy(getattr(runtime, "dataset_library", None), principal.user_id).require_document(report)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise local_only() from None
    def daily_mcp_policy(principal, run, report):
        try:
            reference = report["history_reference"]
            summary = daily_service.gateway.summary(principal.user_id, reference["history_id"], reference["version"],
                                                    include_deleted=True)
            if (summary["provider"] != "yfinance" or report.get("provider") != "yfinance"
                    or summary["version"] != reference["version"]
                    or summary["snapshot_content_hash"] != reference["history_content_hash"]
                    or summary["observation_ref"] != reference["observation_ref"]):
                raise local_only()
        except (AttributeError, KeyError, TypeError, ValueError):
            raise local_only() from None
    def inspect_saved(table, principal, run):
        from .contracts import fingerprint
        from .result_views import digest
        from finance_platform.research.native_results import FAMILIES
        from finance_platform.research.payload_storage import hydrate
        with closing(runtime.database.connect()) as con:
            row = con.execute(f"""SELECT r.result_json,r.result_hash FROM {table} r JOIN jobs j
              ON j.job_id=r.job_id AND j.run_id=r.run_id AND j.owner_id=r.owner_id
              WHERE r.owner_id=? AND r.run_id=? AND j.status='SUCCEEDED'""", (principal.user_id, run)).fetchone()
        if row is None:
            raise PlatformError("NOT_FOUND", 404)
        value = hydrate(runtime, principal.user_id, "reports", row["result_json"])
        encoder = digest if table == "research_ux_results" or value.get("family") in FAMILIES else fingerprint
        if encoder(value) != row["result_hash"]:
            raise local_only()
        return value
    from .autonomous_result_index import result_source
    def read_report_storage(owner_id, reference):
        from finance_platform.research.payload_storage import MAXIMUM, storage
        storage(runtime).read(owner_id, reference, category="reports", maximum_bytes=MAXIMUM)
        return reference["sha256"]
    autonomous = result_source(runtime)
    if autonomous is not None:
        def autonomous_policy(principal, attempt_id, report):
            from .mcp_research import require_session_sources
            session = runtime.autonomous_research.get(principal, report["session_id"])
            require_session_sources(runtime, principal, session["spec"])
            ReportDataPolicy(runtime.dataset_library, principal.user_id).require_document(report)
        autonomous = replace(autonomous, mcp_policy=autonomous_policy, inspect=autonomous.read)
    migration = getattr(runtime, "onboarding_migration", None)
    index=UnifiedResultIndex(runtime.database,
        research=None if research_service is None else ResultSource('research',research_service.result,research_service.export_result,
                                                                     mcp_policy=research_mcp_policy,
                                                                     inspect=lambda p,r:inspect_saved("research_results",p,r)),
        daily=None if daily_service is None else ResultSource('daily',daily_service.result,export_daily,daily_service.series,
                                                              mcp_policy=daily_mcp_policy,
                                                              inspect=lambda p,r:daily_service.result(p,r,include_deleted=True)),
        tasks=None if task_service is None else ResultSource('tasks',task_service.result,lambda p,r:encode_report(task_service.result(p,r)),
                                                               mcp_policy=getattr(task_service,"require_mcp_report",None),
                                                               inspect=lambda p,r:inspect_saved("research_ux_results",p,r)),
        jobs=ResultSource('jobs',job_report,lambda p,r:encode_report(job_report(p,r))),
        autonomous=autonomous,
        migration=None if migration is None else ResultSource("migration", migration.read_archive),
        report_storage=read_report_storage)
    router=APIRouter(prefix='/results',tags=['我的结果'])
    from .result_views import filename
    views = index.views
    def call(fn,*args):
        try:return fn(*args)
        except PlatformError:raise
        except ValueError as error:
            code=getattr(error,'code',None)
            if type(code)is str and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',code):raise PlatformError(code,422)from None
            raise PlatformError('RESULT_READ_FAILED',500)from None
    @router.get('')
    def results(request:Request,kind:str='all',limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=10000),
                state:str='active',q:str|None=None,source:str='all',from_date:str|None=None,to_date:str|None=None):
        return index.list(runtime.require_principal(request), kind, limit, offset, state=state, q=q,
                          source=source, from_date=from_date, to_date=to_date)
    @router.get('/{result_id}/management')
    def management(result_id: str, request: Request):
        return index.management(runtime.require_principal(request), result_id)
    @router.post('/lifecycle')
    def batch_lifecycle(body: BatchLifecycleCommand, request: Request):
        return index.batch_lifecycle(runtime.require_principal(request, write=True), body)
    @router.post('/{result_id}/lifecycle')
    def change_lifecycle(result_id: str, body: LifecycleCommand, request: Request):
        return index.change_lifecycle(runtime.require_principal(request, write=True), result_id, body)
    @router.get('/{result_id}/export')
    def export(result_id:str,request:Request):
        raw=call(index.export,runtime.require_principal(request),result_id)
        if type(raw)is not bytes:raise PlatformError('RESULT_EXPORT_INVALID',422)
        if len(raw)>8*1024**2:raise PlatformError('RESULT_EXPORT_TOO_LARGE',413)
        return Response(raw,media_type='application/json',
                        headers={'Content-Disposition':f'attachment; filename="{filename(result_id)}"'})
    @router.get('/{result_id}/summary')
    def summary(result_id:str,request:Request):return call(views.summary,runtime.require_principal(request),result_id)
    @router.get('/{result_id}/series')
    def series(result_id:str,request:Request,section:str='',limit:int=Query(500,ge=1,le=8192),offset:int=Query(0,ge=0,le=2000000),expected_sha256:str|None=None):
        return call(views.section,runtime.require_principal(request),result_id,section,limit,offset,expected_sha256)
    @router.get('/{result_id}/export-manifest')
    def export_manifest(result_id:str,request:Request):return call(views.export_manifest,runtime.require_principal(request),result_id)
    @router.get('/{result_id}')
    def read(result_id:str,request:Request):return call(index.read,runtime.require_principal(request),result_id)
    return router

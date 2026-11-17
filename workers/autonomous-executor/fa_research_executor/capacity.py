"""Persistent no-refund retained-data/metadata-count admission, product DB only.

This does not delete old objects or pretend Docker metadata has a disk quota.
Docker metadata is separately bounded by retained-container count. Exact source,
stdout/stderr and journal bounds are enforced by the controller and inventory.
"""
from dataclasses import dataclass, asdict
from .protocol import require, encoded, digest

STDOUT_LIMIT=65536
STDERR_LIMIT=65536
JOURNAL_LIMIT=128*1024*1024
METADATA_ALLOWANCE=256*1024
MAX_CODE_JSON=48*1024*6


@dataclass(frozen=True)
class Policy:
    revision:str='retained-capacity-1'
    prefix_objects:int=8192
    reserved_bytes:int=8*1024*1024*1024
    journal_bytes:int=JOURNAL_LIMIT
    events:int=100000


class CapacityBook:
    @classmethod
    def open_existing(cls,journal,*,policy=Policy()):
        value=cls.__new__(cls);value.journal=journal;value.policy=policy
        return value

    def __init__(self,journal,*,policy=Policy()):
        self.journal=journal;self.policy=policy
        with journal.connect() as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS capacity_policy(id INTEGER PRIMARY KEY CHECK(id=1),json BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS capacity_plans(plan_id TEXT PRIMARY KEY,binding_hash TEXT NOT NULL,runtime_sha256 TEXT NOT NULL,
              prefixes INTEGER NOT NULL,reserved_bytes INTEGER NOT NULL,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS capacity_slots(plan_id TEXT NOT NULL,symbol TEXT NOT NULL,ordinal INTEGER NOT NULL,
              request_limit INTEGER NOT NULL,reserved_bytes INTEGER NOT NULL,request_hash TEXT UNIQUE,name TEXT UNIQUE,
              PRIMARY KEY(plan_id,symbol,ordinal));
            CREATE TABLE IF NOT EXISTS capacity_legacy(request_hash TEXT PRIMARY KEY,reserved_bytes INTEGER NOT NULL);
            CREATE TRIGGER IF NOT EXISTS capacity_plans_no_delete BEFORE DELETE ON capacity_plans BEGIN SELECT RAISE(ABORT,'immutable'); END;
            CREATE TRIGGER IF NOT EXISTS capacity_policy_no_update BEFORE UPDATE ON capacity_policy BEGIN SELECT RAISE(ABORT,'immutable'); END;
            CREATE TRIGGER IF NOT EXISTS capacity_legacy_no_delete BEFORE DELETE ON capacity_legacy BEGIN SELECT RAISE(ABORT,'immutable'); END;
            ''')
            expected=encoded(asdict(policy));old=con.execute('SELECT json FROM capacity_policy WHERE id=1').fetchone()
            if old is None:con.execute('INSERT INTO capacity_policy VALUES(1,?)',(expected,))
            else:require(bytes(old[0])==expected,'EXECUTOR_CAPACITY_POLICY_CHANGED')

    def _used(self,con):
        planned=con.execute('SELECT COALESCE(SUM(prefixes),0),COALESCE(SUM(reserved_bytes),0) FROM capacity_plans').fetchone()
        legacy=con.execute('SELECT COUNT(*),COALESCE(SUM(reserved_bytes),0) FROM capacity_legacy').fetchone()
        extra=(0,0)
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retained_closure_artifact_sets'").fetchone():
            extra=con.execute('SELECT COALESCE(SUM(artifact_count),0),COALESCE(SUM(reserved_bytes),0) FROM retained_closure_artifact_sets').fetchone()
        return planned[0]+legacy[0]+extra[0],planned[1]+legacy[1]+self.policy.journal_bytes+extra[1]

    def register_exact_retained_closures(self,header,*,budget):
        require(getattr(self.journal, 'qa_state', None) is None, 'QA_HISTORICAL_RECOVERY_FORBIDDEN')
        from .retained_closure import register
        return register(self,header,budget=budget)

    @staticmethod
    def _insert_retained_closure_set(con,operation,charge,raw,sha256):
        con.execute('INSERT INTO retained_closure_artifact_sets VALUES(1,?,3,?,?,?)',(operation,charge,raw,sha256))

    def retained_closure_files(self):
        from .retained_closure import load_set
        with self.journal.connect()as con:row=load_set(con)
        return {}if row is None else{f['relative']:f for f in row[0]['files']}

    def register_legacy(self,request_hash,actual_bytes):
        with self.journal.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT 1 FROM capacity_slots WHERE request_hash=?',(request_hash,)).fetchone():return
            if con.execute('SELECT 1 FROM capacity_legacy WHERE request_hash=?',(request_hash,)).fetchone():return
            require(type(actual_bytes)is int and actual_bytes>=0,'EXECUTOR_STORAGE_ACCOUNTING_INVALID')
            count,size=self._used(con)
            require(count+1<=self.policy.prefix_objects,'EXECUTOR_OBJECT_LIMIT')
            require(size+actual_bytes+METADATA_ALLOWANCE<=self.policy.reserved_bytes,'EXECUTOR_RETAINED_BYTES_LIMIT')
            con.execute('INSERT INTO capacity_legacy VALUES(?,?)',(request_hash,actual_bytes+METADATA_ALLOWANCE))

    def reserve_plan(self,plan_id,binding_hash,runtime_sha256,slots,*,actual_staging_bytes):
        require(type(slots)is list and bool(slots),'EXECUTOR_EMPTY_CAPACITY_PLAN')
        require(actual_staging_bytes<=self.policy.reserved_bytes,'EXECUTOR_RETAINED_BYTES_LIMIT')
        with self.journal.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            require(not con.execute('SELECT 1 FROM capacity_plans WHERE plan_id=?',(plan_id,)).fetchone(),'EXECUTOR_PLAN_ALREADY_RESERVED')
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unreserved_helper_closures'").fetchone():
                require(not con.execute('SELECT 1 FROM unreserved_helper_closures WHERE attempt_id=?',(plan_id,)).fetchone(),
                        'EXECUTOR_EARLY_CLOSED_ATTEMPT')
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retained_closure_artifact_sets'").fetchone():
                from .retained_closure import FILES, load_set
                load_set(con) # validates any saved historical registry before reuse checks
                require(plan_id not in{r[0]for r in FILES}or not con.execute('SELECT 1 FROM retained_closure_artifact_sets').fetchone(),
                        'EXECUTOR_RETAINED_ATTEMPT_REUSE_BLOCK')
            require(not con.execute("SELECT 1 FROM capacity_plans WHERE status='ACTIVE'").fetchone(),'EXECUTOR_ACTIVE_PLAN_BLOCK')
            count,size=self._used(con);requested=sum(s['reserved_bytes'] for s in slots)
            require(count+len(slots)<=self.policy.prefix_objects,'EXECUTOR_OBJECT_LIMIT')
            require(size+requested<=self.policy.reserved_bytes,'EXECUTOR_RETAINED_BYTES_LIMIT')
            con.execute("INSERT INTO capacity_plans VALUES(?,?,?,?,?,'ACTIVE')",(plan_id,binding_hash,runtime_sha256,len(slots),requested))
            for slot in slots:
                con.execute('INSERT INTO capacity_slots VALUES(?,?,?,?,?,NULL,NULL)',
                    (plan_id,slot['symbol'],slot['index'],slot['request_limit'],slot['reserved_bytes']))
        return {'policy':asdict(self.policy),'plan_id':plan_id,'reserved_prefixes':len(slots),'reserved_retained_bytes':requested,
                'actual_staging_bytes':actual_staging_bytes,'refund_on_failure':False}

    def active_plans(self):
        with self.journal.connect() as con:
            return con.execute("SELECT plan_id,binding_hash,runtime_sha256 FROM capacity_plans WHERE status='ACTIVE'").fetchall()

    def _recovery_scope(self,con):
        from .legacy_bindings import require_configured
        require_configured()
        from .recovery import PLAN,BINDING_SHA,OLD_RUNTIME,REQUEST_SHA,CID
        import re
        policy=con.execute('SELECT json FROM capacity_policy WHERE id=1').fetchone()
        require(policy is not None and bytes(policy[0])==encoded(asdict(self.policy)),'EXECUTOR_CAPACITY_POLICY_CHANGED')
        row=con.execute('SELECT binding_hash,runtime_sha256,prefixes,reserved_bytes,status FROM capacity_plans WHERE plan_id=?',(PLAN,)).fetchone()
        require(row is not None and row[0]==BINDING_SHA and row[1]==OLD_RUNTIME,'RECOVERY_PLAN_IDENTITY_MISMATCH')
        require(type(row[2])is int and 0<row[2]<=8192 and type(row[3])is int and row[3]>=0,'RECOVERY_PLAN_INVALID')
        if row[4]=='ACTIVE':
            active=con.execute("SELECT plan_id FROM capacity_plans WHERE status='ACTIVE' LIMIT 2").fetchall()
            require(active==[(PLAN,)],'RECOVERY_OTHER_ACTIVE_PLAN')
        else:require(row[4]=='CLOSED','RECOVERY_PLAN_INVALID')
        slots=con.execute('SELECT symbol,ordinal,request_limit,reserved_bytes,request_hash,name FROM capacity_slots WHERE plan_id=? ORDER BY symbol,ordinal LIMIT 8193',(PLAN,)).fetchall()
        require(all(type(s[0])is str and type(s[1])is int and s[1]>=0 and type(s[2])is int and 0<s[2]<=4194304
                    and type(s[3])is int and s[3]>=s[2] for s in slots),'RECOVERY_SLOT_INVALID')
        require(len(slots)==row[2] and sum(s[3]for s in slots)==row[3],'RECOVERY_SLOT_TOTAL_MISMATCH')
        claims=[s for s in slots if s[4]is not None or s[5]is not None]
        require(len(claims)==1 and claims[0][4]==REQUEST_SHA and type(claims[0][5])is str
                and re.fullmatch('fa-attempt-[a-f0-9]{32}',claims[0][5]),'RECOVERY_CLAIM_SET_MISMATCH')
        slot=claims[0]
        attempt=con.execute('SELECT name,container_id,status FROM attempts WHERE request_hash=?',(REQUEST_SHA,)).fetchone()
        require(attempt is not None and attempt[0]==slot[5] and attempt[1]==CID
                and attempt[2]in {'STOPPED','CREATED_REJECTED'},'RECOVERY_ATTEMPT_UNPROVEN')
        events=con.execute('SELECT sequence,body,sha256 FROM events WHERE request_hash=? ORDER BY sequence LIMIT 33',(REQUEST_SHA,)).fetchall()
        require(0<len(events)<=32,'RECOVERY_EVENTS_UNPROVEN')
        from .protocol import strict_json
        for _,body,expected in events:
            require(len(body)<=16384,'RECOVERY_EVENTS_UNPROVEN')
            event=strict_json(bytes(body),16384)
            require(digest(event)==expected and ('container_id'not in event or event['container_id']==CID)
                    and ('name'not in event or event['name']==slot[5])
                    and ('request_sha256'not in event or event['request_sha256']==REQUEST_SHA),'RECOVERY_EVENTS_UNPROVEN')
        count,size=self._used(con)
        return {'plan_id':PLAN,'binding_sha256':BINDING_SHA,'runtime_sha256':OLD_RUNTIME,
                'status':row[4],'prefixes':row[2],'reserved_bytes':row[3],
                'slots_sha256':digest(slots),'policy_sha256':digest(asdict(self.policy)),
                'retained_totals':{'prefixes':count,'bytes':size},
                'claim':{'symbol':slot[0],'ordinal':slot[1],'request_limit':slot[2],
                         'reserved_bytes':slot[3],'request_sha256':slot[4],'name':slot[5],
                         'container_id':attempt[1],'journal_status':attempt[2],
                         'events_sha256':digest([(e[0],e[2])for e in events])}}

    def read_recovery_scope(self,*,budget):
        self._require_recovery_path()
        budget.check()
        with self.journal.connect(operation_budget=budget) as con:
            return self._recovery_scope(con)

    def _require_recovery_path(self):
        from .legacy_bindings import require_configured
        require_configured()
        require(getattr(self.journal, 'qa_state', None) is None, 'QA_HISTORICAL_RECOVERY_FORBIDDEN')
        from pathlib import Path
        from .recovery import STAGING
        root=Path(STAGING);file=Path(self.journal.path)
        require(root.is_dir() and not root.is_symlink() and root.resolve()==root
                and file==root/'controller-journal.sqlite3' and not file.is_symlink()
                and file.is_file() and file.stat().st_nlink==1,'RECOVERY_JOURNAL_SCOPE_MISMATCH')

    @staticmethod
    def _recovery_record(con,plan):
        from .protocol import strict_json
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_recoveries'").fetchone():return None
        row=con.execute('SELECT proof_key,record,sha256 FROM capacity_recoveries WHERE plan_id=?',(plan,)).fetchone()
        if row is None:return None
        record=strict_json(bytes(row[1]),16384)
        require(digest(record)==row[2],'RECOVERY_RECORD_CORRUPT')
        return row[0],record,row[2]

    def recover_exact_plan(self,header,observer,*,budget):
        """One existing SQLite write transaction owns observation and commit.

        The observer performs only the exact bounded SDK/path reads. All plan,
        profile, quota, immutable-record and close decisions stay in this API.
        """
        from .recovery import PLAN,BINDING_SHA,RecoveryBudget,validate_header,validate_observation
        from .protocol import strict_json
        from pathlib import Path
        require(type(budget)is RecoveryBudget and budget.close_timer is not None,'RECOVERY_KERNEL_BUDGET_REQUIRED')
        self._require_recovery_path()
        require(len(encoded(header))<=8192,'RECOVERY_HEADER_TOO_LARGE')
        header=strict_json(encoded(header),8192) # immutable local preimage
        key=validate_header(header);budget.check()
        with self.journal.connect(operation_budget=budget) as con:
            # The same DB is used by reserve_plan, claim and journal.admit.
            # Holding BEGIN IMMEDIATE blocks those writers through observation;
            # process death releases/rolls back the ownership gate automatically.
            con.execute('BEGIN IMMEDIATE');budget.check()
            before=self._recovery_scope(con);existing=self._recovery_record(con,PLAN)
            if before['status']=='CLOSED':
                require(existing is not None and existing[0]==key,'RECOVERY_CONFLICT_OR_MISSING_RECORD')
                require(existing[1]['target_after_sha256']==digest({k:v for k,v in before.items()if k!='retained_totals'}),'RECOVERY_CLOSED_PREIMAGE_CHANGED')
                return {'verification':existing[1],'sha256':existing[2]}
            require(before['status']=='ACTIVE' and existing is None,'RECOVERY_CONFLICT_OR_MISSING_RECORD')
            require(callable(observer),'RECOVERY_FIXED_OBSERVER_REQUIRED')
            observed=observer(before,budget);budget.check()
            proof=validate_observation(before,observed,budget)
            require(self._recovery_scope(con)==before,'RECOVERY_PREIMAGE_CHANGED')
            require(self.journal.event_count(con)<self.policy.events,'EXECUTOR_JOURNAL_LIMIT')
            # Individual DDL statements remain in THIS transaction; executescript
            # would implicitly commit and is deliberately not used here.
            con.execute('CREATE TABLE IF NOT EXISTS capacity_recoveries(plan_id TEXT PRIMARY KEY,operation_id TEXT UNIQUE NOT NULL,proof_key TEXT NOT NULL,record BLOB NOT NULL,sha256 TEXT NOT NULL)')
            con.execute("CREATE TRIGGER IF NOT EXISTS capacity_recoveries_no_update BEFORE UPDATE ON capacity_recoveries BEGIN SELECT RAISE(ABORT,'immutable'); END")
            con.execute("CREATE TRIGGER IF NOT EXISTS capacity_recoveries_no_delete BEFORE DELETE ON capacity_recoveries BEGIN SELECT RAISE(ABORT,'immutable'); END")
            self._close_plan(con,PLAN,BINDING_SHA,require_active=True)
            after=self._recovery_scope(con)
            require(after==dict(before,status='CLOSED'),'RECOVERY_NO_REFUND_READBACK_FAILED')
            import hashlib
            from . import controller_journal,recovery
            sources={name:hashlib.sha256(Path(file).read_bytes()).hexdigest()for name,file in
                [('capacity',__file__),('controller_journal',controller_journal.__file__),('recovery',recovery.__file__)]}
            record={'schema':'fa-independent-plan-recovery-verification-1','header':header,
                    'plan_id':PLAN,'binding_sha256':BINDING_SHA,'original_runtime_sha256':before['runtime_sha256'],
                    'verifier_sources':sources,'observation':proof,'before_sha256':digest(before),
                    'operation_budget':budget.snapshot(),
                    'after_sha256':digest(after),'retained_totals_before':before['retained_totals'],
                    'target_after_sha256':digest({k:v for k,v in after.items()if k!='retained_totals'}),
                    'retained_totals_after':after['retained_totals'],'status':'CLOSED_CAPACITY_RETAINED',
                    'original_missing_closure_evidence_sha256':'8a319ec3d8501b616aeaf2b9829cb90a5414aa1609161fefb2e5e372e097961e',
                    'original_failure_preserved':True,'historical_linux_closure_receipt':False,
                    'meaning':'NEW_TIME_INDEPENDENT_VERIFICATION_NOT_ORIGINAL_CLOSURE','refund':False}
            raw=encoded(record);require(len(raw)<=16384,'RECOVERY_RECORD_TOO_LARGE')
            con.execute('INSERT INTO capacity_recoveries VALUES(?,?,?,?,?)',(PLAN,header['operation_id'],key,raw,digest(record)))
            budget.check()
            return {'verification':record,'sha256':digest(record)}

    def close_plan(self,plan_id,binding_hash):
        with self.journal.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._close_plan(con,plan_id,binding_hash)

    @staticmethod
    def _close_plan(con,plan_id,binding_hash,*,require_active=False):
        row=con.execute('SELECT binding_hash,status FROM capacity_plans WHERE plan_id=?',(plan_id,)).fetchone()
        require(row is not None and row[0]==binding_hash,'EXECUTOR_CAPACITY_BINDING_MISMATCH')
        require(not require_active or row[1]=='ACTIVE','EXECUTOR_RECOVERY_PREIMAGE_CHANGED')
        result=con.execute("UPDATE capacity_plans SET status='CLOSED' WHERE plan_id=? AND binding_hash=? AND status='ACTIVE'",(plan_id,binding_hash))
        require(not require_active or result.rowcount==1,'EXECUTOR_RECOVERY_PREIMAGE_CHANGED')

    def claim(self,plan_id,binding_hash,symbol,index,request_hash,name,request_bytes):
        with self.journal.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            plan=con.execute('SELECT binding_hash,status FROM capacity_plans WHERE plan_id=?',(plan_id,)).fetchone()
            require(plan is not None and plan[0]==binding_hash,'EXECUTOR_CAPACITY_BINDING_MISMATCH')
            require(plan[1]=='ACTIVE','EXECUTOR_PLAN_NOT_ACTIVE')
            slot=con.execute('SELECT request_limit,request_hash FROM capacity_slots WHERE plan_id=? AND symbol=? AND ordinal=?',
                             (plan_id,symbol,index)).fetchone()
            require(slot is not None and slot[1] is None,'EXECUTOR_PREFIX_NOT_PRE_RESERVED')
            require(request_bytes<=slot[0],'EXECUTOR_REQUEST_EXCEEDS_RESERVATION')
            con.execute('UPDATE capacity_slots SET request_hash=?,name=? WHERE plan_id=? AND symbol=? AND ordinal=?',
                        (request_hash,name,plan_id,symbol,index))


def plan_slots(rows_by_symbol):
    from .protocol import MAX_REQUEST
    slots=[]
    for symbol,rows in sorted(rows_by_symbol.items()):
        for index in range(len(rows)):
            request_limit=len(encoded(rows[:index+1]))+MAX_CODE_JSON+16384
            require(request_limit<=MAX_REQUEST,'EXECUTOR_REQUEST_TOO_LARGE_FOR_PLAN')
            total=request_limit+STDOUT_LIMIT+STDERR_LIMIT+METADATA_ALLOWANCE+16384
            slots.append({'symbol':symbol,'index':index,'request_limit':request_limit,'reserved_bytes':total})
    return slots


CHINESE_REASONS={
 'EXECUTOR_RETAINED_ATTEMPT_REUSE_BLOCK':'本次历史尝试的证据已保留记账，已拒绝复用其身份，原任务状态与额度保持不变。',
 'EXECUTOR_EARLY_CLOSED_ATTEMPT':'本次执行器尝试已在预留前闭场，已拒绝迟到的重新预留，原失败与累计额度保持不变。',
 'EXECUTOR_PLAN_NOT_ACTIVE':'本次执行器计划已闭场，已拒绝迟到的前缀请求，历史额度保持不变。',
 'EXECUTOR_JOURNAL_BUSY':'执行器状态正在被独占核验或写入，本次未继续执行。',
 'EXECUTOR_OBJECT_LIMIT':'本批容器/前缀的累计数量预算已满（含已预留项），未继续调用模型。历史记录保持不变。',
 'EXECUTOR_RETAINED_BYTES_LIMIT':'本批执行器保留数据的累计空间预算不足，未继续调用模型；不会自动删除旧证据。',
 'EXECUTOR_RUNNING_OWNED_BLOCK':'本批仍有已登记容器运行，已阻止新的研究；不会接管或终止另一任务。',
 'EXECUTOR_RECONCILIATION_REQUIRED':'上次容器的归属或创建结果尚未核实，已阻止新的研究，保留原记录待核对。',
 'EXECUTOR_STORAGE_UNACCOUNTED':'本批暂存目录存在未核对文件或大小变化，已停止准入；不会广泛清理。',
 'EXECUTOR_CAPACITY_POLICY_CHANGED':'本批容量规则与持久记录不一致，不能通过重启重置限额。',
 'EXECUTOR_JOURNAL_LIMIT':'本批执行器日志已达容量上限，停止新的研究并保留原证据。',
 'EXECUTOR_ACTIVE_PLAN_BLOCK':'本批上一研究执行器尚未确认闭场，未继续调用模型；保留原归属等待核对。'
 ,'EXECUTOR_PLAN_ALREADY_RESERVED':'本次研究的空间预留已经存在，不能重复调用模型或重置用量。'
 ,'EXECUTOR_PREFIX_NOT_PRE_RESERVED':'实验前缀未通过受信空间预留，已停止执行。'
 ,'EXECUTOR_REQUEST_EXCEEDS_RESERVATION':'本次输入超过已预留的空间上限，已停止执行。'
 ,'EXECUTOR_DAEMON_READY_TIMEOUT':'专用研究服务未在45秒内就绪，未继续调用模型；请查看本批启动记录。'
 ,'EXECUTOR_UNIT_IDENTITY_MISMATCH':'专用研究服务配置与已核验版本不一致，已停止自动启动。'
 ,'EXECUTOR_UNIT_START_FAILED':'专用研究服务启动失败，未继续调用模型。'
 ,'EXECUTOR_DAEMON_PROFILE_MISMATCH':'专用研究服务的目录或隔离能力与已核验配置不一致，已停止执行。'
 ,'EXECUTOR_FREE_SPACE_INSUFFICIENT':'本批暂存盘可用空间不足以覆盖新实验的保守预留，未继续调用模型。'
}

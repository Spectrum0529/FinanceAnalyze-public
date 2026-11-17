"""Independent controller product journal, never formal research Ledger.

Creation intent precedes Docker effects; unknown/active ownership blocks new
admission until explicit trusted reconciliation, even after controller restart.
"""
import sqlite3
from contextlib import contextmanager
from .protocol import encoded, digest, require


class ControllerJournal:
    @classmethod
    def open_existing(cls,path,*,operation_budget=None):
        """Existing trusted file only; no constructor/schema/bootstrap writes."""
        from pathlib import Path
        import stat
        file=Path(path);info=file.lstat()
        require(file.is_file() and not file.is_symlink() and info.st_nlink==1
                and stat.S_ISREG(info.st_mode),'RECOVERY_JOURNAL_FILE_INVALID')
        value=cls.__new__(cls);value.path=str(file);value.cleanup_budget=operation_budget;value.existing_only=True
        return value

    def __init__(self, path,*,cleanup_budget=None,qa_state=None):
        self.path = str(path)
        self.cleanup_budget=cleanup_budget
        self.qa_state = qa_state
        require(qa_state is None or str(qa_state.journal) == self.path, 'QA_JOURNAL_BINDING_MISMATCH')
        with self.connect() as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS attempts(request_hash TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,
              container_id TEXT,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,request_hash TEXT NOT NULL,
              body BLOB NOT NULL,sha256 TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
            ''')

    @contextmanager
    def connect(self,*,operation_budget=None):
        budget=operation_budget if operation_budget is not None else self.cleanup_budget
        timeout=3 if budget is None else budget.timeout(3)
        qa_state = getattr(self, 'qa_state', None)
        if qa_state is not None:
            qa_state.verify_journal()
        if qa_state is not None or getattr(self,'existing_only',False):
            from pathlib import Path
            connection=sqlite3.connect(Path(self.path).absolute().as_uri()+'?mode=rw',uri=True,timeout=timeout)
        else:connection=sqlite3.connect(self.path,timeout=timeout)
        try:
            if budget is not None:budget.check()
            require(connection.execute('PRAGMA page_size').fetchone()[0]==4096,'EXECUTOR_JOURNAL_PROFILE_INVALID')
            require(connection.execute('PRAGMA max_page_count=32768').fetchone()[0]<=32768,'EXECUTOR_JOURNAL_LIMIT')
            yield connection
            if budget is not None:budget.check()
            connection.commit()
            if budget is not None:budget.check()
        except sqlite3.OperationalError as error:
            connection.rollback()
            if getattr(error,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                require(False,'EXECUTOR_JOURNAL_BUSY')
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def event_count(con):
        count=con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        for table in('capacity_recoveries','unreserved_helper_closures','retained_closure_artifact_sets'):
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                count+=con.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        return count

    def record_unreserved_closure(self,binding,runtime,receipt,*,budget):
        """One immutable no-plan closure, never fake request/plan/STOPPED.

        Local helper ownership/closure is verified by the fixed caller. Same
        original cleanup budget and existing journal page/event caps apply.
        """
        from .closure import validate_early_closure
        from .protocol import strict_json
        require(budget.active and budget.close_timer is not None,'EARLY_CLOSURE_CLEANUP_BUDGET_REQUIRED')
        # Freeze before any SQL/callback can mutate the supplied preimage.
        require(len(encoded({'binding':binding,'receipt':receipt}))<=16384,'EARLY_CLOSURE_TOO_LARGE')
        binding=strict_json(encoded(binding),4096);receipt=strict_json(encoded(receipt),16384)
        validate_early_closure(binding,runtime,receipt);budget.check()
        record={'schema':'fa-unreserved-helper-closure-1','binding':binding,'runtime_sha256':runtime,
            'closure':receipt,'scope':'CURRENT_HELPER_NO_PLAN_NO_OWNED_REQUESTS',
            'capacity_refund':False,'original_failure_unchanged':True}
        raw=encoded(record);require(len(raw)<=16384,'EARLY_CLOSURE_TOO_LARGE')
        aid=binding['attempt_id'];body_hash=digest(record)
        with self.connect(operation_budget=budget)as con:
            con.execute('BEGIN IMMEDIATE');budget.check()
            require(not con.execute('SELECT 1 FROM capacity_plans WHERE plan_id=?',(aid,)).fetchone()
                and not con.execute('SELECT 1 FROM capacity_slots WHERE plan_id=? LIMIT 1',(aid,)).fetchone(),
                'EARLY_CLOSURE_RESERVED_EFFECTS_PRESENT')
            exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unreserved_helper_closures'").fetchone()
            if exists:
                old=con.execute('SELECT record,sha256 FROM unreserved_helper_closures WHERE attempt_id=?',(aid,)).fetchone()
                if old is not None:
                    require(bytes(old[0])==raw and old[1]==body_hash,'EARLY_CLOSURE_CONFLICT')
                    return {'storage':'UNRESERVED_PRODUCT_JOURNAL','verification':record,'sha256':body_hash}
            require(self.event_count(con)<100000,'EXECUTOR_JOURNAL_LIMIT')
            # All additive schema/INSERT statements share this transaction;
            # executescript would commit the gate early and is not used here.
            con.execute('CREATE TABLE IF NOT EXISTS unreserved_helper_closures(attempt_id TEXT PRIMARY KEY,record BLOB NOT NULL,sha256 TEXT NOT NULL)')
            con.execute("CREATE TRIGGER IF NOT EXISTS unreserved_helper_closures_no_update BEFORE UPDATE ON unreserved_helper_closures BEGIN SELECT RAISE(ABORT,'immutable'); END")
            con.execute("CREATE TRIGGER IF NOT EXISTS unreserved_helper_closures_no_delete BEFORE DELETE ON unreserved_helper_closures BEGIN SELECT RAISE(ABORT,'immutable'); END")
            self._insert_unreserved_closure(con,aid,raw,body_hash);budget.check()
            return {'storage':'UNRESERVED_PRODUCT_JOURNAL','verification':record,'sha256':body_hash}

    @staticmethod
    def _insert_unreserved_closure(con,aid,raw,body_hash):
        con.execute('INSERT INTO unreserved_helper_closures VALUES(?,?,?)',(aid,raw,body_hash))

    def admit(self, request_hash, name):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            require(self.event_count(con)<100000,'EXECUTOR_JOURNAL_LIMIT')
            require(not con.execute("SELECT 1 FROM attempts WHERE status NOT IN ('STOPPED','CREATED_REJECTED')").fetchone(), 'PRIOR_EXECUTOR_CLOSURE_UNRESOLVED')
            require(not con.execute('SELECT 1 FROM attempts WHERE request_hash=?',(request_hash,)).fetchone(), 'EXECUTOR_REQUEST_ALREADY_ATTEMPTED')
            con.execute("INSERT INTO attempts VALUES(?,?,NULL,'CREATE_INTENT')", (request_hash, name))
            event = {'event': 'CREATE_INTENT_BEFORE_DOCKER', 'request_sha256': request_hash, 'name': name}
            con.execute('INSERT INTO events(request_hash,body,sha256) VALUES(?,?,?)',(request_hash, encoded(event), digest(event)))

    def record(self, request_hash, status, event, container_id=None):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            require(len(encoded(event))<=16384 and self.event_count(con)<100000,'EXECUTOR_JOURNAL_LIMIT')
            require(con.execute('SELECT 1 FROM attempts WHERE request_hash=?',(request_hash,)).fetchone() is not None, 'EXECUTOR_INTENT_REQUIRED')
            con.execute('UPDATE attempts SET status=?,container_id=COALESCE(?,container_id) WHERE request_hash=?',(status,container_id,request_hash))
            con.execute('INSERT INTO events(request_hash,body,sha256) VALUES(?,?,?)',(request_hash, encoded(event), digest(event)))
        return {'persisted': True, 'event_sha256': digest(event)}

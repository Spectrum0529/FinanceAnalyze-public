"""Exact THREE historical artifact accounting only; no task/plan recovery.

pipe_eof=False and original failures remain. No unlinked-legacy absence claim.
Only FA010's reviewed installed-source/bounded maintenance caller may invoke.
"""
import hashlib
import os
from pathlib import Path
import stat
from .protocol import require,exact,sha,digest,encoded,strict_json
from .capacity import METADATA_ALLOWANCE

from .paths import executor_root
STAGING=executor_root()/'runtime/staging'
OWNER_UID=1000 # optional locally reviewed historical binding
READONLY_SHA=None # optional locally reviewed historical binding
PROVENANCE_SHA=None # optional locally reviewed historical binding
# Exact original identity/preimages, never runtime-selected filenames.
BLOCKED_OPERATION=None
FILES=() # optional locally reviewed historical binding


def identity(s):return(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)


def observe_file(root,target,budget):
    from .legacy_bindings import require_configured
    require_configured()
    aid,binding,runtime,expected,preimage=target
    path=root/('closure-'+aid+'.json');budget.check()
    for parent in(root,*root.parents):require(not parent.is_symlink(),'RETAINED_CLOSURE_PATH_UNPROVEN')
    before=path.lstat()
    require(stat.S_ISREG(before.st_mode)and before.st_nlink==1 and before.st_uid==OWNER_UID
        and before.st_size<=16384 and identity(before)==preimage,'RETAINED_CLOSURE_PREIMAGE_CHANGED')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    try:
        held=os.fstat(fd);require(identity(held)==preimage,'RETAINED_CLOSURE_PREIMAGE_CHANGED')
        raw=os.read(fd,16385);require(len(raw)==held.st_size and not os.read(fd,1),'RETAINED_CLOSURE_LENGTH_CHANGED')
        require(hashlib.sha256(raw).hexdigest()==expected,'RETAINED_CLOSURE_HASH_CHANGED')
        value=strict_json(raw,16384)
        exact(value,'binding_sha256 runtime_sha256 helpers_closed containers_closed pipe_eof active_read_counters cleanup_budget')
        require(value['binding_sha256']==binding and value['runtime_sha256']==runtime
            and value['helpers_closed']is True and value['containers_closed']is True and value['pipe_eof']is False,
            'RETAINED_CLOSURE_ORIGINAL_FIELDS_CHANGED')
        require(identity(os.fstat(fd))==preimage and identity(path.lstat())==preimage,'RETAINED_CLOSURE_PREIMAGE_CHANGED')
        allocated=max(held.st_size,getattr(held,'st_blocks',0)*512);budget.check()
        return {'relative':path.name,'attempt_id':aid,'binding_sha256':binding,'runtime_sha256':runtime,
            'sha256':expected,'bytes':len(raw),'allocated_bytes':allocated,'metadata':list(preimage),
            'charged_bytes':allocated+METADATA_ALLOWANCE,'local_claims':{'helpers_closed':True,'containers_closed':True,'pipe_eof':False},
            'meaning':'EXACT_RETAINED_ARTIFACT_NOT_OLD_TASK_OR_IO_ACCEPTANCE'}
    finally:os.close(fd)


def load_set(con):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retained_closure_artifact_sets'").fetchone():return None
    rows=con.execute('SELECT operation_id,artifact_count,reserved_bytes,record,sha256 FROM retained_closure_artifact_sets LIMIT 2').fetchall()
    if not rows:return None
    from .legacy_bindings import require_configured
    require_configured()
    require(len(rows)==1,'RETAINED_CLOSURE_REGISTRY_INVALID');op,count,size,raw,expected=rows[0]
    record=strict_json(bytes(raw),16384)
    require(digest(record)==expected and record['schema']=='fa-exact-retained-closure-accounting-1'
        and record['header']['operation_id']==op and record['header']['readonly_evidence_sha256']==READONLY_SHA
        and record['header']['lifecycle_evidence_sha256']==PROVENANCE_SHA and count==3 and len(record['files'])==3
        and size==sum(r['charged_bytes']for r in record['files']),'RETAINED_CLOSURE_REGISTRY_INVALID')
    for row,target in zip(record['files'],FILES):
        require(row['attempt_id']==target[0]and row['relative']=='closure-'+target[0]+'.json'
            and row['sha256']==target[3]and row['metadata']==list(target[4]),'RETAINED_CLOSURE_REGISTRY_INVALID')
    return record,expected


def register(book,header,*,budget):
    """One transaction verifies three unchanged files then charges once.

    No fake request/plan, file writes, task status or old recovery CAS. New
    retained-artifact units consume the same original conservative object cap.
    """
    from .legacy_bindings import require_configured
    require_configured()
    from .recovery import RecoveryBudget
    import re
    require(type(budget)is RecoveryBudget and budget.close_timer is not None,'RETAINED_CLOSURE_KERNEL_REQUIRED')
    require(Path(book.journal.path)==STAGING/'controller-journal.sqlite3'and STAGING.resolve()==STAGING,'RETAINED_CLOSURE_SCOPE_MISMATCH')
    journal_info=Path(book.journal.path).lstat()
    require(stat.S_ISREG(journal_info.st_mode)and journal_info.st_nlink==1 and journal_info.st_uid==OWNER_UID
        and journal_info.st_size<=book.policy.journal_bytes,'RETAINED_CLOSURE_JOURNAL_UNPROVEN')
    header=strict_json(encoded(header),8192)
    exact(header,'schema operation_id review_sha256 verifier_runtime_sha256 owner_source_manifest_sha256 readonly_evidence_sha256 lifecycle_evidence_sha256')
    require(header['schema']=='fa-retained-closure-accounting-request-1'and re.fullmatch('[a-f0-9]{32}',header['operation_id'])
        and header['operation_id']!=BLOCKED_OPERATION
        and header['readonly_evidence_sha256']==READONLY_SHA and header['lifecycle_evidence_sha256']==PROVENANCE_SHA,
        'RETAINED_CLOSURE_AUTHORITY_MISMATCH')
    for key in('review_sha256','verifier_runtime_sha256','owner_source_manifest_sha256'):sha(header[key])
    budget.check()
    with book.journal.connect(operation_budget=budget)as con:
        con.execute('BEGIN IMMEDIATE');budget.check()
        existing=load_set(con)
        if existing is not None:require(existing[0]['header']==header,'RETAINED_CLOSURE_CONFLICT')
        from dataclasses import asdict
        policy=con.execute('SELECT json FROM capacity_policy WHERE id=1').fetchone()
        require(policy is not None and bytes(policy[0])==encoded(asdict(book.policy)),'EXECUTOR_CAPACITY_POLICY_CHANGED')
        for aid,*_ in FILES:
            require(not con.execute('SELECT 1 FROM capacity_plans WHERE plan_id=?',(aid,)).fetchone()
                and not con.execute('SELECT 1 FROM capacity_slots WHERE plan_id=? LIMIT 1',(aid,)).fetchone(),
                'RETAINED_CLOSURE_SELECTED_MEMBERSHIP_CHANGED')
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unreserved_helper_closures'").fetchone():
                require(not con.execute('SELECT 1 FROM unreserved_helper_closures WHERE attempt_id=?',(aid,)).fetchone(),
                        'RETAINED_CLOSURE_SELECTED_MEMBERSHIP_CHANGED')
        observed=[observe_file(STAGING,t,budget)for t in FILES]
        if existing is not None:
            require(existing[0]['files']==observed,'RETAINED_CLOSURE_CONFLICT')
            return {'verification':existing[0],'sha256':existing[1]}
        count,size=book._used(con);charge=sum(r['charged_bytes']for r in observed)
        require(count+3<=book.policy.prefix_objects,'EXECUTOR_OBJECT_LIMIT')
        require(size+charge<=book.policy.reserved_bytes,'EXECUTOR_RETAINED_BYTES_LIMIT')
        require(book.journal.event_count(con)<book.policy.events,'EXECUTOR_JOURNAL_LIMIT')
        record={'schema':'fa-exact-retained-closure-accounting-1','header':header,'files':observed,
            'retained_before':{'objects':count,'bytes':size},'retained_after':{'objects':count+3,'bytes':size+charge},
            'charged_retained_artifact_units':3,'charged_bytes':charge,'refund':False,
            'unlinked_legacy_membership':'UNOBSERVED_NOT_SCANNED_CONSERVATIVE_ADDITIONAL_CHARGE',
            'old_task_plan_and_pipe_status_unchanged':True,'meaning':'ACCOUNTED_ARTIFACTS_ONLY_NOT_UNIQUE_ROOTCAUSE_OR_TASK_SUCCESS'}
        raw=encoded(record);require(len(raw)<=16384,'RETAINED_CLOSURE_RECORD_LIMIT')
        con.execute('CREATE TABLE IF NOT EXISTS retained_closure_artifact_sets(id INTEGER PRIMARY KEY CHECK(id=1),operation_id TEXT UNIQUE NOT NULL,artifact_count INTEGER NOT NULL,reserved_bytes INTEGER NOT NULL,record BLOB NOT NULL,sha256 TEXT NOT NULL)')
        con.execute("CREATE TRIGGER IF NOT EXISTS retained_closure_artifact_sets_no_update BEFORE UPDATE ON retained_closure_artifact_sets BEGIN SELECT RAISE(ABORT,'immutable'); END")
        con.execute("CREATE TRIGGER IF NOT EXISTS retained_closure_artifact_sets_no_delete BEFORE DELETE ON retained_closure_artifact_sets BEGIN SELECT RAISE(ABORT,'immutable'); END")
        book._insert_retained_closure_set(con,header['operation_id'],charge,raw,digest(record))
        require([observe_file(STAGING,t,budget)for t in FILES]==observed,'RETAINED_CLOSURE_PREIMAGE_CHANGED')
        require(book._used(con)==(count+3,size+charge),'RETAINED_CLOSURE_ACCOUNTING_READBACK_FAILED')
        require(load_set(con)==(record,digest(record)),'RETAINED_CLOSURE_RECORD_READBACK_FAILED');budget.check()
        return {'verification':record,'sha256':digest(record)}

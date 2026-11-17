"""Two bounded, owned QA daemon starts through the production bootstrap."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import select
import sqlite3
import stat
import subprocess
import time

from .protocol import digest, encoded, exact, identifier, require, strict_json
from .qa_scope_contract import cold_unit_name
from .qa_state import _directory, _file, _read


from .paths import user_unit_directory
UNIT_DIR = user_unit_directory()
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
USER_BUS = Path('/run/user/1000/bus')
PROPERTIES = ('LoadState','FragmentPath','UnitFileState','DropInPaths','NeedDaemonReload','ActiveState',
              'MainPID','InvocationID','ControlGroup')
ATTEMPT_LIMIT = 2
SYSTEMD = 'org.freedesktop.systemd1'
MANAGER_PATH = '/org/freedesktop/systemd1'
BUS = 'org.freedesktop.DBus'
COMPLETION_FIELDS = ('USER_UNIT','USER_INVOCATION_ID','JOB_ID','JOB_TYPE','JOB_RESULT',
                     '_BOOT_ID','_UID','_PID','__MONOTONIC_TIMESTAMP','__CURSOR')


def _exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _start_ticks(pid, group):
    process=Path('/proc')/str(pid)
    require(process.stat().st_uid==1000, 'QA_COLD_MAIN_PID_MISMATCH')
    fields=(process/'stat').read_text().rsplit(') ',1)[-1].split()
    ticks=int(fields[19])
    require(ticks>0 and '0::'+group in (process/'cgroup').read_text().splitlines(),
            'QA_COLD_MAIN_PID_MISMATCH')
    return ticks


class ColdDaemon:
    def __init__(self, state):
        self.state=state
        self.root=state.run_root/'cold-evidence'
        self.unit_name=cold_unit_name(state.identity.run_id)
        self.unit_file=UNIT_DIR/self.unit_name
        self.unit_object=MANAGER_PATH+'/unit/'+self.unit_name.replace('-','_2d').replace('.','_2e')
        self.binding=None
        self.ordinal=None
        self.intent=None
        self.previous=None
        self.generation=None
        self.pidfd=None
        self.manager=None
        self.submission_started=None
        self.job_id=None
        self.client_reaped=False
        self.completion=None
        self.unit_identity=None

    def _path(self, kind, ordinal):
        return self.root/f'{kind}-{ordinal}.json'

    def unit_bytes(self):
        root=self.state.run_root
        runtime=self.state.socket.parent
        command=['/usr/bin/env','-i','PATH=/usr/bin:/bin:/usr/sbin:/sbin','LANG=C.UTF-8',
                 'HOME='+str(root/'daemon-home'),'XDG_RUNTIME_DIR='+str(runtime),
                 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
                 'XDG_CONFIG_HOME='+str(root/'docker-config'),'XDG_DATA_HOME='+str(self.state.docker_data),
                 'TMPDIR='+str(root/'tmp'),'DOCKER_TMPDIR='+str(root/'tmp'),'/usr/bin/dockerd-rootless.sh',
                 '--host='+self.state.daemon_url,'--data-root='+str(self.state.docker_data),
                 '--exec-root='+str(runtime/'exec'),'--pidfile='+str(runtime/'dockerd.pid'),
                 '--config-file='+str(root/'docker-config/daemon.json')]
        return '\n'.join(('[Unit]','Description=FinanceAnalyze isolated cold-start QA','',
            '[Service]','Type=simple','Delegate=yes','KillMode=mixed','Restart=no',
            'TimeoutStartSec=45','TimeoutStopSec=30','TasksMax=1024','MemoryMax=4294967296',
            'RuntimeDirectory='+runtime.name,'RuntimeDirectoryMode=0700',
            'ExecStart='+' '.join(command),'','[Install]','WantedBy=default.target','')).encode('ascii')

    def verify_recipe(self):
        fd=os.open(self.unit_file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==1000
                    and stat.S_IMODE(info.st_mode)&0o022==0,'EXECUTOR_UNIT_IDENTITY_MISMATCH')
            recipe=stream.read(8193)
            current=self.unit_file.lstat()
        identity=(info.st_dev,info.st_ino,info.st_ctime_ns)
        require(identity==(current.st_dev,current.st_ino,current.st_ctime_ns)
                and recipe==self.unit_bytes() and hashlib.sha256(recipe).hexdigest()==self.state.identity.unit_sha256,
                'EXECUTOR_UNIT_IDENTITY_MISMATCH')
        for folder in ('daemon-home','docker-config','tmp'):
            _directory(self.state.run_root/folder,private=True)
        self._verify_data_root()
        config=strict_json(_read(self.state.run_root/'docker-config/daemon.json',8192),8192)
        require(config=={'log-driver':'none','live-restore':False},'QA_COLD_DAEMON_CONFIG_INVALID')
        return identity

    def _verify_data_root(self):
        # dockerd can set its own directory to 0710. The run parent remains
        # private 0700 and the daemon directory is never a guest mount.
        _directory(self.state.run_root,private=True)
        path=self.state.docker_data
        info=path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==1000 and path.resolve()==path
                and stat.S_IMODE(info.st_mode) in (0o700,0o710),'QA_DIRECTORY_INVALID')

    def _load(self, kind, ordinal):
        return strict_json(_read(self._path(kind,ordinal),65536),65536)

    def bind(self, binding, phase):
        exact(binding,'session_id attempt_id job_id run_id fencing_token')
        for key in ('session_id','attempt_id','job_id','run_id'):
            identifier(binding[key])
        require(type(binding['fencing_token']) is int and binding['fencing_token']>0
                and phase=='development' and self.binding is None,'QA_COLD_ATTEMPT_BINDING_INVALID')
        self.binding=dict(binding)

    def _record(self, kind, value):
        with _file(self._path(kind,self.ordinal),os.O_WRONLY|os.O_CREAT|os.O_EXCL) as fd:
            with os.fdopen(os.dup(fd),'wb') as stream:
                stream.write(encoded(value))
                stream.flush()
            os.fsync(fd)
        directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _command(self, args, timeout):
        env={'PATH':'/usr/bin:/bin','HOME':str(self.state.run_root/'daemon-home'),'LANG':'C.UTF-8',
             'XDG_RUNTIME_DIR':'/run/user/1000','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus',
             'SYSTEMD_COLORS':'0'}
        reply=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,env=env,
                             cwd=self.state.run_root,timeout=timeout,check=False)
        require(reply.returncode==0 and len(reply.stdout)<=16384,'QA_COLD_MANAGER_QUERY_FAILED')
        return reply.stdout

    def _bus(self, timeout, *args):
        return self._command(['/usr/bin/busctl','--user','--no-pager','--allow-interactive-authorization=no',*args],timeout)

    def await_manager(self, check, limit):
        """Wait only before first binding, using the caller's existing deadline."""
        require(self.manager is None,'QA_COLD_MANAGER_IDENTITY_MISMATCH')

        def anchor():
            bus=USER_BUS.lstat()
            require(stat.S_ISSOCK(bus.st_mode) and bus.st_uid==1000,'EXECUTOR_USER_BUS_IDENTITY_MISMATCH')
            return BOOT_ID.read_text().strip(),bus.st_dev,bus.st_ino

        check()
        original=anchor()
        endpoint=('call',BUS,'/org/freedesktop/DBus',BUS,'NameHasOwner','s',SYSTEMD)
        while True:
            check()
            require(anchor()==original,'QA_COLD_MANAGER_IDENTITY_MISMATCH')
            owned=self._bus(limit(1),*endpoint).strip()
            check()
            require(owned in (b'b false',b'b true'),'QA_COLD_MANAGER_QUERY_FAILED')
            if owned==b'b true':
                break
            time.sleep(limit(.1))
        manager=self._manager(limit)
        require(manager['boot_id']==original[0] and self._manager(limit)==manager and anchor()==original,
                'QA_COLD_MANAGER_IDENTITY_MISMATCH')
        check()
        limit(1)
        return manager

    def _manager(self, limit):
        boot=BOOT_ID.read_text().strip()
        require(re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',boot) is not None,
                'QA_COLD_UNIT_GENERATION_MISMATCH')
        endpoint=['call',BUS,'/org/freedesktop/DBus',BUS]
        owner=re.fullmatch(rb's "(:[0-9]+\.[0-9]+)"',self._bus(limit(1),*endpoint,'GetNameOwner','s',SYSTEMD).strip())
        require(owner is not None,'QA_COLD_MANAGER_IDENTITY_MISMATCH')
        name=owner[1].decode('ascii')
        credentials=re.fullmatch(rb'u ([1-9][0-9]{0,9})',
            self._bus(limit(1),*endpoint,'GetConnectionUnixProcessID','s',name).strip())
        require(credentials is not None and int(credentials[1])>1,'QA_COLD_MANAGER_IDENTITY_MISMATCH')
        pid=int(credentials[1])
        group='/user.slice/user-1000.slice/user@1000.service/init.scope'
        return {'boot_id':boot,'bus_owner':name,'pid':pid,'start_ticks':_start_ticks(pid,group)}

    def _show(self, timeout):
        args=['/usr/bin/systemctl','--user','--no-pager','--all','show',
              *('--property='+name for name in PROPERTIES),self.unit_name]
        pairs=[line.split('=',1) for line in self._command(args,timeout).decode().splitlines()]
        require(all(len(pair)==2 for pair in pairs) and len(pairs)==len(PROPERTIES), 'QA_COLD_UNIT_QUERY_FAILED')
        values=dict(pairs)
        require(set(values)==set(PROPERTIES) and values['LoadState']=='loaded'
                and values['FragmentPath']==str(self.unit_file) and values['UnitFileState']=='disabled'
                and values['NeedDaemonReload']=='no' and not values['DropInPaths'],
                'QA_COLD_UNIT_IDENTITY_MISMATCH')
        return values

    def _snapshot(self, limit):
        values=self._show(limit(1))
        raw=self._bus(limit(1),'get-property',self.manager['bus_owner'],self.unit_object,SYSTEMD+'.Unit','Job')
        job=re.fullmatch(rb'\(uo\) (0|[1-9][0-9]{0,9}) "(/[A-Za-z0-9_/]*)"',raw.strip())
        require(job is not None,'QA_COLD_UNIT_QUERY_FAILED')
        job_id, path=(field.decode('ascii') for field in job.groups())
        require(path in ('/',self.unit_object) if job_id=='0' else path==MANAGER_PATH+'/job/'+job_id,
                'QA_COLD_UNIT_QUERY_FAILED')
        return {**values,'Job':job_id}

    def _done(self, limit):
        if self.completion is not None:
            return self.completion
        require(self.job_id is not None and self._manager(limit)==self.manager,'QA_COLD_START_OWNERSHIP_UNKNOWN')
        args=['/usr/bin/journalctl','--user','--no-pager','-n','2','--output=json',
              '--output-fields='+','.join(COMPLETION_FIELDS),
              '_BOOT_ID='+self.manager['boot_id'].replace('-',''), '_UID=1000',
              '_PID='+str(self.manager['pid']), 'USER_UNIT='+self.unit_name,
              'JOB_ID='+str(self.job_id),'JOB_TYPE=start','JOB_RESULT=done']
        rows=self._command(args,limit(1)).splitlines()
        if not rows:
            return None
        require(len(rows)==1,'QA_COLD_START_OWNERSHIP_UNKNOWN')
        row=strict_json(rows[0],16384)
        require(all(type(row.get(key)) is str for key in COMPLETION_FIELDS)
                and row['USER_UNIT']==self.unit_name and row['JOB_ID']==str(self.job_id)
                and row['JOB_TYPE']=='start' and row['JOB_RESULT']=='done'
                and row['_BOOT_ID']==self.manager['boot_id'].replace('-','')
                and row['_UID']=='1000' and row['_PID']==str(self.manager['pid'])
                and int(row['__MONOTONIC_TIMESTAMP'])>=self.submission_started
                and re.fullmatch('[a-f0-9]{32}',row['USER_INVOCATION_ID'])
                and row['USER_INVOCATION_ID']!='0'*32,'QA_COLD_START_OWNERSHIP_UNKNOWN')
        self.completion={key:row[key] for key in COMPLETION_FIELDS}
        return self.completion

    def observe_client(self, output, reaped):
        prefix=b'Enqueued anchor job '
        lines=[line for line in output.split(b'\n')[:-1] if line.startswith(prefix)]
        target=rb'Enqueued anchor job ([1-9][0-9]{0,9}) '+re.escape(self.unit_name.encode('ascii'))+rb'/start\.'
        self.client_reaped=reaped
        if len(lines)==1 and (match:=re.fullmatch(target,lines[0])) and int(match[1])<=0xffffffff:
            self.job_id=int(match[1])

    def _prior_closed(self):
        require(not _exists(self._path('failure',1)) and
                all(_exists(self._path(kind,1)) for kind in ('transaction','ready','local-close','windows-close')),
                'QA_COLD_PRIOR_CLOSURE_UNPROVEN')
        prior=self._load('start',1)
        transaction=self._load('transaction',1)
        ready=self._load('ready',1)
        local=self._load('local-close',1)
        host=self._load('windows-close',1)
        require(prior['schema']=='fa-qa-cold-start-1' and prior['ordinal']==1
                and prior['scope_sha256']==self.state.identity.sha256
                and prior['unit_sha256']==self.state.identity.unit_sha256
                and ready['schema']=='fa-qa-cold-ready-1' and ready['image_digest']==self.state.identity.image_digest
                and transaction['schema']=='fa-qa-cold-transaction-1' and transaction['intent_sha256']==digest(prior)
                and ready['intent_sha256']==digest(prior) and ready['transaction_sha256']==digest(transaction)
                and ready['generation']['invocation_id']==transaction['completion']['USER_INVOCATION_ID']
                and local['schema']=='fa-qa-cold-local-close-1'
                and local['intent_sha256']==digest(prior) and local['generation_sha256']==digest(ready['generation'])
                and host['scope_sha256']==self.state.identity.sha256 and host['local_close_sha256']==digest(local),
                'QA_COLD_PRIOR_CLOSURE_UNPROVEN')
        require(prior['binding']['session_id']==self.binding['session_id']
                and all(prior['binding'][key]!=self.binding[key] for key in ('attempt_id','job_id','run_id')),
                'QA_COLD_RESUME_BINDING_MISMATCH')
        receipt=host['event']
        native=receipt['native_close']
        samples=native['empty_samples']
        require(host['schema']=='fa-qa-cold-windows-close-1' and host['event_kind']=='BRIDGE_CLOSED'
                and receipt['binding_sha256']==digest(prior['binding']) and receipt['returncode']==0
                and receipt['linux_closure_receipt'] is True and receipt['io_joined'] is True
                and receipt['primary_code'] is None and receipt['cleanup_code'] is None
                and receipt['cleanup_protocol_code'] is None and receipt['forced'] is False
                and native['held_admission']['held_before_resume'] is True
                and native['root_handle_signalled'] is True and native['handles_closed'] is True
                and native['pipes_closed'] is True and native['forced'] is False and len(samples)==2
                and all(row['active_processes']==0 and row['pids']==[] for row in samples),
                'QA_COLD_PRIOR_CLOSURE_UNPROVEN')
        attempt=prior['binding']['attempt_id']
        identifier(attempt)
        closure=strict_json(_read(self.state.staging/('closure-'+attempt+'.json'),65536),65536)
        require(local['closure_sha256']==digest(closure) and closure['binding_sha256']==digest(prior['binding'])
                and closure['runtime_sha256']==self.state.identity.runtime_sha256
                and closure['helpers_closed'] is True and closure['containers_closed'] is True,
                'QA_COLD_PRIOR_CLOSURE_UNPROVEN')
        with closing(sqlite3.connect(self.state.journal.as_uri()+'?mode=ro',uri=True,timeout=.5)) as con:
            require(not con.execute("SELECT 1 FROM attempts WHERE status NOT IN ('STOPPED','CREATED_REJECTED') LIMIT 1").fetchone()
                    and con.execute("SELECT plan_id,binding_hash,runtime_sha256,status FROM capacity_plans LIMIT 2").fetchall()
                    ==[(attempt,digest(prior['binding']),self.state.identity.runtime_sha256,'CLOSED')],
                    'QA_COLD_PRIOR_CLOSURE_UNPROVEN')
        return ready['generation']

    def before_start(self, check, limit):
        require(self.state._claimed and self.binding is not None,'QA_STATE_LOCK_REQUIRED')
        self.state.verify_journal()
        _directory(self.root,private=True)
        require(self.root.resolve()==self.root,'QA_DIRECTORY_INVALID')
        require(not _exists(self._path('start',ATTEMPT_LIMIT)), 'QA_COLD_ATTEMPT_LIMIT')
        self.ordinal=2 if _exists(self._path('start',1)) else 1
        if self.ordinal==2:
            self.previous=self._prior_closed()
        else:
            require(next(self.root.iterdir(),None) is None and self.state.journal.stat().st_size==0,
                    'QA_COLD_FRESH_STATE_REQUIRED')
        self.manager=self.await_manager(check,limit)
        self.unit_identity=self.verify_recipe()
        values=self._snapshot(limit)
        require(values['Job']=='0' and self._absent(values),
                'QA_COLD_DAEMON_NOT_ABSENT')
        self.intent={'schema':'fa-qa-cold-start-1','scope_sha256':self.state.identity.sha256,
                     'unit_sha256':self.state.identity.unit_sha256,'ordinal':self.ordinal,
                     'binding':self.binding,'socket_initially_absent':True,'unit_initially_inactive':True,
                     'unit_job_initially_zero':True,'unit':self.unit_name,'manager':self.manager,
                     'unit_file_identity':list(self.unit_identity)}
        self._record('start',self.intent)
        check()

    def dispatch(self, check, limit):
        require(self.verify_recipe()==self.unit_identity and self._manager(limit)==self.manager,
                'EXECUTOR_UNIT_IDENTITY_MISMATCH')
        values=self._snapshot(limit)
        require(values['Job']=='0' and self._absent(values) and self.verify_recipe()==self.unit_identity,
                'QA_COLD_DAEMON_NOT_ABSENT')
        check()
        self.submission_started=time.monotonic_ns()//1000

    def _absent(self, unit):
        return unit['ActiveState']=='inactive' and unit['MainPID']=='0' and not _exists(self.state.socket)

    def reconcile(self, cleanup, error):
        if self.submission_started is None:
            return
        result={'schema':'fa-qa-cold-failure-1','intent_sha256':digest(self.intent),'job_id':self.job_id,
                'client_reaped':self.client_reaped,'error_type':type(error).__name__,'state':'FAILED_UNKNOWN'}
        try:
            cleanup.enter()
            limit=cleanup.timeout
            require(self._manager(limit)==self.manager and self.verify_recipe()==self.unit_identity,
                    'QA_COLD_START_OWNERSHIP_UNKNOWN')
            unit=self._snapshot(limit)
            result['observed']={key:unit[key] for key in ('Job','ActiveState','MainPID','InvocationID')}
            require(self.client_reaped and self.job_id is not None,'QA_COLD_START_OWNERSHIP_UNKNOWN')
            # Job.Cancel cannot require that the job is still waiting at the
            # manager. An observed job remains for the trusted holder to resolve.
            completion=self._done(limit) if unit['Job']=='0' else None
            if completion is not None:
                result['completion']=completion
                require(self._manager(limit)==self.manager and self.verify_recipe()==self.unit_identity,
                        'QA_COLD_START_OWNERSHIP_UNKNOWN')
                final=self._snapshot(limit)
                result['observed']={key:final[key] for key in ('Job','ActiveState','MainPID','InvocationID')}
                if final['Job']=='0' and self._absent(final):
                    cleanup.check()
                    result['state']='FAILED_RECONCILED'
        except Exception as reconcile_error:
            result['reconcile_error_type']=type(reconcile_error).__name__
        self._record('failure',result)

    def _read_generation(self, timeout):
        unit=self._show(timeout)
        pid=int(unit['MainPID'])
        invocation=unit['InvocationID']
        require(unit['ActiveState']=='active' and pid>0 and re.fullmatch('[a-f0-9]{32}',invocation)
                and invocation!='0'*32 and unit['ControlGroup'].endswith('/'+self.unit_name),
                'QA_COLD_UNIT_GENERATION_MISMATCH')
        boot=BOOT_ID.read_text().strip()
        require(re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',boot) is not None,
                'QA_COLD_UNIT_GENERATION_MISMATCH')
        socket=self.state.socket
        _directory(socket.parent,private=True)
        info=socket.lstat()
        require(socket.parent.resolve()==socket.parent and stat.S_ISSOCK(info.st_mode)
                and info.st_uid==os.geteuid()==1000,'QA_DAEMON_SOCKET_MISMATCH')
        self._verify_data_root()
        return {'boot_id':boot,'invocation_id':invocation,'main_pid':pid,'main_start_ticks':_start_ticks(pid,unit['ControlGroup']),
                'socket_device':info.st_dev,'socket_inode':info.st_ino,'socket_uid':info.st_uid,
                'control_group':unit['ControlGroup']}

    def capture(self, limit):
        require(self.state._claimed and self.intent is not None and self.generation is None,'QA_STATE_LOCK_REQUIRED')
        completion=self._done(limit)
        if completion is None:
            return False
        require(self.verify_recipe()==self.unit_identity and self._manager(limit)==self.manager,
                'EXECUTOR_UNIT_IDENTITY_MISMATCH')
        require(self._snapshot(limit)['Job']=='0','QA_COLD_START_OWNERSHIP_UNKNOWN')
        value=self._read_generation(limit(1))
        require(value['invocation_id']==completion['USER_INVOCATION_ID'] and value['boot_id']==self.manager['boot_id'],
                'QA_COLD_START_OWNERSHIP_UNKNOWN')
        require(self.previous is None or value['invocation_id']!=self.previous['invocation_id'],
                'QA_COLD_UNIT_GENERATION_MISMATCH')
        self.pidfd=os.pidfd_open(value['main_pid'])
        self.generation=value
        self.verify(limit(1))
        require(self.verify_recipe()==self.unit_identity,'EXECUTOR_UNIT_IDENTITY_MISMATCH')
        self.transaction={'schema':'fa-qa-cold-transaction-1','intent_sha256':digest(self.intent),
                          'manager':self.manager,'job_id':self.job_id,'submission_started_us':self.submission_started,
                          'client_exit_code':0,'completion':completion}
        self._record('transaction',self.transaction)
        return True

    def verify(self, timeout=1):
        require(self.state._claimed and self.pidfd is not None, 'QA_COLD_UNIT_GENERATION_MISMATCH')
        wait=select.poll()
        wait.register(self.pidfd,select.POLLIN)
        require(not wait.poll(0) and self._read_generation(timeout)==self.generation and not wait.poll(0),
                'QA_COLD_UNIT_GENERATION_MISMATCH')

    def ready(self, seconds, profile, limit):
        self.verify(limit(1))
        self._record('ready',{'schema':'fa-qa-cold-ready-1','intent_sha256':digest(self.intent),
            'transaction_sha256':digest(self.transaction),
            'generation':self.generation,'start_exit_code':0,'elapsed_millis':int(seconds*1000),
            'profile_sha256':digest(profile),'image_digest':self.state.identity.image_digest})

    def finish_ready(self, limit):
        require(self.verify_recipe()==self.unit_identity and self._manager(limit)==self.manager,
                'EXECUTOR_UNIT_IDENTITY_MISMATCH')
        require(self._snapshot(limit)['Job']=='0','QA_COLD_START_OWNERSHIP_UNKNOWN')
        self.verify(limit(1))

    def record_closed(self, closure, cleanup):
        if closure.get('storage')!='RESERVED_PLAN_FILE':
            return
        self.verify(cleanup.timeout(1))
        cleanup.check()
        proof=strict_json(_read(Path(closure['path']),65536),65536)
        self._record('local-close',{'schema':'fa-qa-cold-local-close-1','intent_sha256':digest(self.intent),
            'generation_sha256':digest(self.generation),'closure_sha256':digest(proof)})
        cleanup.check()

    def release(self):
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd=None

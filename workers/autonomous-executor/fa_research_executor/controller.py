"""Trusted Linux Docker owner. No daemon setup; no caller Docker flags.

Created containers are retained stopped for evidence; only exact owned ID is
controlled. Requires actual FA012 reviewed image/daemon/profile before use.
"""
import os
import hashlib
import re
import select
import struct
import sys
import tempfile
import time
import uuid
import threading
import stat
import socket
from pathlib import Path
from .protocol import require, validate_request, encoded, digest, MAX_RESPONSE, Rejected
from .capacity import CapacityBook, plan_slots, STDOUT_LIMIT, STDERR_LIMIT, JOURNAL_LIMIT

from .paths import executor_root
STAGING_ROOT=executor_root()/'runtime/staging'

ENV = {'HOME': '/tmp', 'LANG': 'C.UTF-8', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
       'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
TMPFS = {'/tmp': 'rw,noexec,nosuid,nodev,size=134217728,mode=1777',
         '/out': 'rw,noexec,nosuid,nodev,size=33554432,mode=1777'}
ENTRY = ['/opt/executor/bin/python', '-m', 'fa_research_executor.guest']


class DockerController:
    def __init__(self, *, daemon_url, image_digest, runtime_sha256, staging_root, record, approved_image_env,
                 cleanup_budget=None, qa_state=None):
        require(sys.platform == 'linux', 'LINUX_CONTROLLER_REQUIRED')
        require(re.fullmatch(r'(?:sha256:[a-f0-9]{64}|[a-z0-9./:_-]+@sha256:[a-f0-9]{64})', image_digest) is not None, 'PINNED_IMAGE_REQUIRED')
        root = Path(staging_root)
        require(root==STAGING_ROOT, 'STAGING_ROOT_INVALID')
        if qa_state is None:
            require(re.fullmatch(r'unix:///run/user/[0-9]+/finance-research-20261106/docker.sock', daemon_url) is not None,
                    'INITIAL_ROOTLESS_DAEMON_REQUIRED')
            require(not root.is_symlink() and root.is_dir() and root.resolve() == root, 'STAGING_ROOT_INVALID')
        else:
            from .qa_state import QAState
            require(type(qa_state) is QAState and runtime_sha256 == qa_state.identity.runtime_sha256
                    and image_digest == qa_state.identity.image_digest, 'QA_SCOPE_RUNTIME_MISMATCH')
            require(daemon_url == qa_state.daemon_url, 'EXECUTOR_SOCKET_IDENTITY_MISMATCH')
            qa_state.verify_journal()
            qa_state.verify_daemon_socket()
            root = qa_state.staging
        require(all(type(v) is str and v.split('=', 1)[0] in {'PATH', 'LANG', 'PYTHON_VERSION', 'PYTHON_SHA256',
                    'PYTHON_PIP_VERSION', 'PYTHON_SETUPTOOLS_VERSION', *ENV} for v in approved_image_env), 'IMAGE_ENV_ALLOWLIST_REQUIRED')
        import docker
        from .supervisor import CleanupBudget
        self.cleanup_budget=cleanup_budget if cleanup_budget is not None else CleanupBudget()
        self.qa_state = qa_state
        client_options = {} if qa_state is None else {'version':'1.45'}
        self.client = docker.DockerClient(base_url=daemon_url, timeout=self.cleanup_budget.timeout(3), **client_options)
        self._bind_sdk_deadline()
        self.image = image_digest; self.runtime_sha256 = runtime_sha256
        self.root = root; self.record = record; self.image_env = approved_image_env
        from .controller_journal import ControllerJournal
        journal = root/'controller-journal.sqlite3' if qa_state is None else qa_state.journal
        self.journal = ControllerJournal(journal,cleanup_budget=self.cleanup_budget,qa_state=qa_state)
        self.capacity=CapacityBook(self.journal)
        self._owned_requests=set();self._plan=None
        self.cancelled=threading.Event()
        self.worker_joined=True
        self._transport_close_failed=False

    def _owner_labels(self, name):
        labels = {'finance.executor_owner': 'fa015-autonomous-r01', 'finance.attempt': name}
        if self.qa_state is not None:
            labels['finance.qa_scope'] = self.qa_state.identity.sha256
        return labels

    def _require_qa_container(self, container, name, request_hash, container_id, *, code):
        if self.qa_state is None:
            return
        path = self._input_path(name)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == request_hash, code)
        attrs = container.attrs
        mounts = [item for item in attrs['Mounts'] if item['Type'] == 'bind']
        require((container_id is None or container.id == container_id)
                and attrs.get('Name') == '/' + name and attrs.get('Image') == self.image
                and attrs['Config'].get('Labels') == self._owner_labels(name.removeprefix('fa-attempt-'))
                and len(mounts) == 1 and mounts[0]['Source'] == str(path.parent)
                and mounts[0]['Destination'] == '/input' and mounts[0]['RW'] is False, code)

    def _qa_locator(self, name, container_id):
        if self.qa_state is not None:
            self._input_path(name)
            require(container_id is None or (type(container_id) is str and
                    re.fullmatch('[a-f0-9]{64}', container_id) is not None), 'QA_CONTAINER_ID_INVALID')
        return container_id or name

    def _bind_sdk_deadline(self):
        # One instance-local request hook: no global client.timeout mutation,
        # so worker/supervisor calls share the clock without racing a setting.
        from urllib3.util import Timeout
        original=self.client.api.send
        def send(request,**kwargs):
            if self.qa_state is not None:
                if self.qa_state.cold is None:
                    self.qa_state.verify_daemon_socket()
                else:
                    self.qa_state.verify_daemon_socket(self.cleanup_budget.timeout(1))
            if self.cleanup_budget.active:
                remaining=self.cleanup_budget.timeout(3)
                limit=kwargs.get('timeout')
                parts=limit if type(limit)is tuple and len(limit)==2 else (limit,limit)
                require(all(v is None or type(v)in {int,float} and v>0 for v in parts),'SDK_TIMEOUT_UNSUPPORTED')
                connect,read=(min(remaining,3 if v is None else v)for v in parts)
                kwargs={**kwargs,'timeout':Timeout(total=remaining,connect=connect,read=read)}
            return original(request,**kwargs)
        self.client.api.send=send

    def _close_attach(self, attached):
        """Close only the SDK's known local Unix upgrade, using its own closer.

        SocketIO.close alone only drops an IO reference. Retain the exact
        endpoint before the SDK shutdown and release both wrappers afterwards.
        Unknown transports or any close exception remain unproven.
        """
        from docker.types import CancellableStream
        self.cleanup_budget.check()
        response=getattr(attached,'_response',None)
        raw=getattr(getattr(getattr(getattr(response,'raw',None),'_fp',None),'fp',None),'raw',None)
        endpoint=getattr(raw,'_sock',None)
        require(attached is raw and isinstance(raw,socket.SocketIO) and isinstance(endpoint,socket.socket)
                and endpoint.family==socket.AF_UNIX and endpoint.fileno()>=0
                and attached.fileno()==endpoint.fileno(),'ATTACH_TRANSPORT_UNSUPPORTED')
        try:
            # Official Docker7.2.0 implementation performs SHUT_RDWR then
            # socket.close, rather than closing only the SocketIO facade.
            CancellableStream(iter(()),response).close()
        finally:
            try:
                self.cleanup_budget.check()
                response.close()
            finally:
                self.cleanup_budget.check()
                attached.close()
        self.cleanup_budget.check()
        require(raw.closed and endpoint.fileno()==-1 and response.raw.closed,
                'ATTACH_TRANSPORT_CLOSE_UNPROVEN')

    def _stop_owned(self, container):
        """A lost kill reply is not proof either way; inspect the exact handle.

        Four SDK calls at most, each bounded by the existing 3s client timeout.
        Never retry kill or adopt another object. Failed phases cannot skip
        the final inspection attempt; no STOPPED record without actual proof.
        """
        self.cleanup_budget.enter()
        failures=[]
        def attempt(method, **kwargs):
            try:
                self.cleanup_budget.check()
                getattr(container,method)(**kwargs)
                self.cleanup_budget.check()
                return True
            except Exception as error:
                failures.append((method,error))
                return False
        inspected=attempt('reload')
        if not inspected or container.attrs['State']['Running']:
            attempt('kill')
        if not inspected or container.attrs['State']['Status']!='created':
            attempt('wait',timeout=self.cleanup_budget.timeout(3))
        inspected=attempt('reload')
        state=container.attrs.get('State',{}) if inspected else {}
        if (not inspected or state.get('Running') is not False or state.get('Pid')!=0
                or state.get('Status') not in {'created','exited'}):
            error=Rejected('CONTAINER_CLEANUP_UNPROVEN')
            for method,fault in failures:
                error.add_note(method+':'+type(fault).__module__+'.'+type(fault).__name__)
            if failures:
                raise error from failures[-1][1]
            raise error
        return ('CREATED_REJECTED' if state['Status']=='created' else 'STOPPED'), [
            {'phase':method,'module':type(error).__module__,'type':type(error).__name__}
            for method,error in failures]

    def abort_owned(self):
        self.cleanup_budget.enter();self.cleanup_budget.check()
        self.cancelled.set()
        with self.journal.connect() as con:
            pending=con.execute("SELECT request_hash,name,container_id FROM attempts WHERE status NOT IN ('STOPPED','CREATED_REJECTED')").fetchall()
        import docker
        for request_hash,name,container_id in pending:
            if request_hash not in self._owned_requests:continue
            try:
                container=self.client.containers.get(self._qa_locator(name,container_id))
            except docker.errors.NotFound:
                self.journal.record(request_hash,'CREATE_OUTCOME_UNKNOWN',{'event':'ABSENCE_NOT_CLOSURE_PROOF','name':name})
                continue
            container.reload()
            labels=container.attrs['Config'].get('Labels',{})
            require(labels.get('finance.executor_owner')=='fa015-autonomous-r01'
                    and labels.get('finance.attempt')==name.removeprefix('fa-attempt-'),'OWNED_CONTAINER_IDENTITY_MISMATCH')
            self._require_qa_container(container,name,request_hash,container_id,code='OWNED_CONTAINER_IDENTITY_MISMATCH')
            status,failures=self._stop_owned(container)
            self.journal.record(request_hash,status,
                                {'event':'SUPERVISOR_CLOSED_EXACT_OWNED','container_id':container.id,
                                 'cleanup_failures':failures},container.id)

    def close(self):
        self.cleanup_budget.enter();self.cleanup_budget.check()
        require(self.worker_joined,'LINUX_WORKER_CLOSE_UNPROVEN')
        self.client.close()
        self.cleanup_budget.check()
        require(not self._transport_close_failed,'CONTAINER_CLEANUP_UNPROVEN')
        with self.journal.connect() as con:
            pending=con.execute("SELECT request_hash FROM attempts WHERE status NOT IN ('STOPPED','CREATED_REJECTED')").fetchall()
            require(not any(row[0] in self._owned_requests for row in pending), 'CONTROLLER_CLOSURE_UNPROVEN')

    def finalize_plan(self):
        # Called only after worker/own containers/children are closed and local
        # closure receipt fsynced. Count and bytes are NEVER refunded.
        self.cleanup_budget.check()
        if self._plan is not None:
            self.capacity.close_plan(*self._plan)
        self.cleanup_budget.check()

    def _binding_for(self,request):
        return {k:request[k] for k in ('session_id','attempt_id','job_id','run_id','fencing_token')}

    def _input_path(self,name):
        require(re.fullmatch('fa-attempt-[a-f0-9]{32}',name) is not None,'EXECUTOR_RECONCILIATION_REQUIRED')
        token=name.removeprefix('fa-attempt-')
        matches=list(self.root.glob('input-'+token+'*'))
        require(len(matches)==1,'EXECUTOR_STORAGE_UNACCOUNTED')
        directory=matches[0]
        require(directory.is_dir() and not directory.is_symlink() and directory.resolve().parent==self.root,'EXECUTOR_STORAGE_UNACCOUNTED')
        path=directory/'request.json'
        require(path.is_file() and not path.is_symlink(),'EXECUTOR_STORAGE_UNACCOUNTED')
        info=path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=4*1024*1024,'EXECUTOR_STORAGE_UNACCOUNTED')
        return path

    def reconcile_startup(self):
        """Observe exact known objects; never stop another live helper's work."""
        import docker
        from .protocol import strict_json
        started=time.monotonic()
        with self.journal.connect() as con:
            pending=con.execute("SELECT request_hash,name,container_id FROM attempts WHERE status NOT IN ('STOPPED','CREATED_REJECTED')").fetchall()
        require(len(pending)<=8,'EXECUTOR_RECONCILIATION_REQUIRED')
        for request_hash,name,container_id in pending:
            require(time.monotonic()-started<15,'EXECUTOR_RECONCILIATION_REQUIRED')
            path=self._input_path(name);raw=path.read_bytes()
            require(hashlib.sha256(raw).hexdigest()==request_hash,'EXECUTOR_RECONCILIATION_REQUIRED')
            request=strict_json(raw,4*1024*1024)
            try:container=self.client.containers.get(self._qa_locator(name,container_id))
            except docker.errors.NotFound:
                require(False,'EXECUTOR_RECONCILIATION_REQUIRED')
            container.reload();attrs=container.attrs
            require(container_id is None or container.id==container_id,'EXECUTOR_RECONCILIATION_REQUIRED')
            labels=attrs['Config'].get('Labels',{})
            require(labels.get('finance.executor_owner')=='fa015-autonomous-r01' and labels.get('finance.attempt')==name.removeprefix('fa-attempt-'),'EXECUTOR_RECONCILIATION_REQUIRED')
            self._require_qa_container(container,name,request_hash,container_id,code='EXECUTOR_RECONCILIATION_REQUIRED')
            mounts=[m for m in attrs['Mounts'] if m['Type']=='bind']
            require(len(mounts)==1 and mounts[0]['Source']==str(path.parent) and mounts[0]['Destination']=='/input' and mounts[0]['RW'] is False,'EXECUTOR_RECONCILIATION_REQUIRED')
            require(not attrs['State']['Running'] and not attrs['State'].get('Restarting'),'EXECUTOR_RUNNING_OWNED_BLOCK')
            # Stopped Docker state alone cannot prove the old controller writer
            # ended. Require its exact durable closure receipt as well.
            receipt_path=self.root/('closure-'+request['attempt_id']+'.json')
            require(receipt_path.is_file() and not receipt_path.is_symlink() and receipt_path.stat().st_nlink==1
                    and receipt_path.stat().st_size<=65536,'EXECUTOR_RECONCILIATION_REQUIRED')
            receipt=strict_json(receipt_path.read_bytes(),65536)
            require(receipt.get('binding_sha256')==digest(self._binding_for(request)) and receipt.get('runtime_sha256')==request['runtime_sha256']
                    and receipt.get('helpers_closed') is True and receipt.get('containers_closed') is True,'EXECUTOR_RECONCILIATION_REQUIRED')
            time.sleep(.05);container.reload()
            require(container.attrs['State']['Status'] in {'created','exited'} and not container.attrs['State']['Running']
                    and container.attrs['State'].get('Pid',0)==0,'EXECUTOR_RECONCILIATION_REQUIRED')
            self.journal.record(request_hash,'STOPPED',{'event':'RECONCILED_EXACT_CLOSED_RECEIPT','container_id':container.id},container.id)
        for plan,binding,runtime in self.capacity.active_plans():
            receipt_path=self.root/('closure-'+plan+'.json')
            require(receipt_path.is_file() and not receipt_path.is_symlink() and receipt_path.stat().st_nlink==1
                    and receipt_path.stat().st_size<=65536,'EXECUTOR_ACTIVE_PLAN_BLOCK')
            receipt=strict_json(receipt_path.read_bytes(),65536)
            require(receipt.get('binding_sha256')==binding and receipt.get('runtime_sha256')==runtime
                    and receipt.get('helpers_closed') is True and receipt.get('containers_closed') is True,'EXECUTOR_ACTIVE_PLAN_BLOCK')
            self.capacity.close_plan(plan,binding)
        return {'pending_objects_observed':len(pending),'policy':'NO_LIVE_ADOPTION_OR_STOP_AT_STARTUP'}

    def storage_inventory(self):
        with self.journal.connect() as con:
            attempts=con.execute('SELECT request_hash,name FROM attempts').fetchall()
            plans={row[0] for row in con.execute('SELECT plan_id FROM capacity_plans')}
        expected_dirs={};known_attempts=set();retained={}
        registered_closures=self.capacity.retained_closure_files()
        from .protocol import strict_json
        for request_hash,name in attempts:
            path=self._input_path(name);raw=path.read_bytes()
            require(hashlib.sha256(raw).hexdigest()==request_hash,'EXECUTOR_STORAGE_UNACCOUNTED')
            body=strict_json(raw,4*1024*1024);known_attempts.add(body['attempt_id'])
            expected_dirs[path.parent.name]=(request_hash,{'request.json'})
            expected_dirs['output-'+name.removeprefix('fa-attempt-')]=(request_hash,{'stdout.bin','stderr.bin'})
            retained[request_hash]=0
        total=0;count=0
        for path in self.root.iterdir():
            count+=1;require(count<=25000,'EXECUTOR_STORAGE_UNACCOUNTED')
            info=path.lstat();require(not stat.S_ISLNK(info.st_mode),'EXECUTOR_STORAGE_UNACCOUNTED')
            if path.name in expected_dirs:
                require(stat.S_ISDIR(info.st_mode),'EXECUTOR_STORAGE_UNACCOUNTED')
                request_hash,allowed=expected_dirs[path.name]
                require({p.name for p in path.iterdir()}==allowed,'EXECUTOR_STORAGE_UNACCOUNTED')
                for child in path.iterdir():
                    item=child.lstat()
                    require(child.name in allowed and stat.S_ISREG(item.st_mode) and item.st_nlink==1,'EXECUTOR_STORAGE_UNACCOUNTED')
                    size=max(item.st_size,getattr(item,'st_blocks',0)*512)
                    total+=size;retained[request_hash]+=size
            else:
                require(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'EXECUTOR_STORAGE_UNACCOUNTED')
                if path.name.startswith('controller-journal.sqlite3'):
                    require(path.name in {'controller-journal.sqlite3','controller-journal.sqlite3-wal','controller-journal.sqlite3-shm','controller-journal.sqlite3-journal'}
                            and info.st_size<=JOURNAL_LIMIT,'EXECUTOR_JOURNAL_LIMIT')
                else:
                    if path.name in registered_closures:
                        from .retained_closure import FILES,observe_file
                        target=next(t for t in FILES if path.name=='closure-'+t[0]+'.json')
                        require(observe_file(self.root,target,self.cleanup_budget)==registered_closures[path.name],
                                'EXECUTOR_STORAGE_UNACCOUNTED')
                    else:
                        require(path.name.startswith('closure-') and path.name.endswith('.json') and path.name[8:-5] in plans|known_attempts
                                and info.st_size<=65536,'EXECUTOR_STORAGE_UNACCOUNTED')
                total+=max(info.st_size,getattr(info,'st_blocks',0)*512)
        require(all((self.root/name).is_dir() for name in expected_dirs),'EXECUTOR_STORAGE_UNACCOUNTED')
        require(all((self.root/name).is_file()for name in registered_closures),'EXECUTOR_STORAGE_UNACCOUNTED')
        for request_hash,size in retained.items():self.capacity.register_legacy(request_hash,size)
        return total

    def reserve_experiment(self,context,begun,rows_by_symbol):
        binding={'session_id':begun['session_id'],'attempt_id':begun['attempt_id'],'job_id':context.job_id,
                 'run_id':context.run.run_id,'fencing_token':context.fencing_token}
        actual=self.storage_inventory()
        slots=plan_slots(rows_by_symbol)
        import shutil
        require(shutil.disk_usage(self.root).free>=sum(s['reserved_bytes'] for s in slots)+16*1024*1024,'EXECUTOR_FREE_SPACE_INSUFFICIENT')
        receipt=self.capacity.reserve_plan(begun['attempt_id'],digest(binding),self.runtime_sha256,slots,actual_staging_bytes=actual)
        self._plan=(begun['attempt_id'],digest(binding))
        return receipt

    def _inspect(self, container, stage, name):
        container.reload(); a = container.attrs; h = a['HostConfig']; c = a['Config']
        image = self.client.images.get(self.image)
        require(a['Image']==image.id and (image.id==self.image if self.image.startswith('sha256:')
                else self.image in image.attrs.get('RepoDigests',[])), 'IMAGE_IDENTITY_MISMATCH')
        ic = image.attrs['Config']
        require(not ic.get('Volumes') and not ic.get('Healthcheck') and ic.get('Env', []) == self.image_env, 'IMAGE_CONFIG_UNREVIEWED')
        require(c['User'] == '10001:10001' and c['Entrypoint'] == ENTRY and not c.get('Cmd') and not c.get('Tty'), 'CONTAINER_CONFIG_INVALID')
        expected_env = {v.split('=', 1)[0]: v.split('=', 1)[1] for v in self.image_env}
        expected_env.update(ENV)
        require(sorted(c['Env']) == sorted(k+'='+v for k, v in expected_env.items()), 'ENVIRONMENT_MISMATCH')
        require(c['Labels'] == self._owner_labels(name), 'OWNER_LABEL_MISMATCH')
        require(h['NetworkMode'] == 'none' and h['ReadonlyRootfs'] and not h['Privileged'], 'ISOLATION_PROFILE_INVALID')
        require(h['CapDrop'] == ['ALL'] and not h.get('CapAdd') and h['SecurityOpt'] == ['no-new-privileges=true'], 'CAPABILITY_PROFILE_INVALID')
        require(h.get('PidMode', '') == '' and h['IpcMode'] == 'private' and h['CgroupnsMode'] == 'private'
                and h.get('UsernsMode', '') != 'host', 'NAMESPACE_PROFILE_INVALID')
        require(not h.get('Devices') and not h.get('DeviceRequests') and not h.get('PortBindings')
                and not h.get('ExtraHosts') and not h.get('Binds') and not h.get('Links') and not h.get('VolumesFrom'), 'HOST_ATTACHMENT_REJECTED')
        require(h['Memory'] == 1073741824 and h['MemorySwap'] == 1073741824 and h['PidsLimit'] == 64
                and h['NanoCpus'] == 1000000000 and h['Tmpfs'] == TMPFS and h['Init'] is True, 'RESOURCE_PROFILE_INVALID')
        require(h['RestartPolicy']['Name'] == 'no' and h['LogConfig']['Type'] == 'none', 'RESTART_LOG_PROFILE_INVALID')
        limits = {v['Name']: (v['Soft'], v['Hard']) for v in h['Ulimits']}
        require(limits == {'nofile': (128, 128), 'core': (0, 0)}, 'ULIMIT_PROFILE_INVALID')
        mounts = a['Mounts']
        bind = [m for m in mounts if m['Type'] == 'bind']
        require(len(bind) == 1 and bind[0]['Source'] == str(stage) and bind[0]['Destination'] == '/input'
                and bind[0]['RW'] is False and bind[0]['Propagation'] == 'rprivate', 'MOUNT_PROFILE_INVALID')
        require(all(m['Type'] == 'bind' or m['Type'] == 'tmpfs' and m['Destination'] in TMPFS for m in mounts), 'EXTRA_MOUNT_REJECTED')
        require(a['State']['Status'] == 'created', 'PRESTART_STATE_INVALID')
        return {'image_id': a['Image'], 'container_id': container.id, 'profile': 'FA012-initial60s-1GiB-rootless', 'inspect_sha256': digest(a)}

    def execute(self, request, active):
        return self._execute(request, active, validate_request)

    def execute_sealed(self, request, active, *, seal):
        from .protocol import validate_final_request
        require(request.get('seal_sha256')==digest(seal) and request.get('source_sha256')==seal['candidate_hash']
                and request.get('session_id')==seal['session_id'] and request.get('attempt_id')==seal['attempt_id']
                and seal.get('exposure_recorded') is True, 'FINAL_SEAL_BINDING_MISMATCH')
        return self._execute(request, active, validate_final_request)

    def _execute(self, request, active, validator):
        original_active=active
        def active():
            require(not self.cancelled.is_set(),'LINUX_SUPERVISOR_CANCELLED')
            original_active()
        validator(request)
        require(request['runtime_sha256'] == self.runtime_sha256, 'RUNTIME_BINDING_MISMATCH')
        active(); name = uuid.uuid4().hex
        require(self._plan is not None and request['reservation_id']==self._plan[0],'EXECUTOR_PREFIX_NOT_PRE_RESERVED')
        symbol,index=request['experiment_id'].rsplit(':',1)[-1],len(request['input']['rows'])-1
        # experiment_id is <attempt_id>:<symbol>, never a host path.
        self.capacity.claim(self._plan[0],self._plan[1],symbol,index,digest(request),'fa-attempt-'+name,len(encoded(request)))
        self.journal.admit(digest(request),'fa-attempt-'+name)
        self._owned_requests.add(digest(request))
        stage = self.root/('input-'+name)
        stage.mkdir(mode=0o700)
        path = stage/'request.json'
        with path.open('xb') as stream:
            stream.write(encoded(request)); stream.flush(); os.fsync(stream.fileno())
        path.chmod(0o444); stage.chmod(0o555)
        require(path.stat().st_nlink == 1 and not path.is_symlink(), 'INPUT_ALIAS_REJECTED')
        import docker
        container = None
        sock = None
        stopped = False
        stdout = bytearray(); stderr = bytearray()
        output_dir = self.root/('output-'+name)
        output_dir.mkdir(mode=0o700)
        request_hash = digest(request)
        try:
            # SDK sends JSON API fields, never shell interpolation/from_env.
            container = self.client.containers.create(self.image, entrypoint=ENTRY, command=[],
                user='10001:10001', environment=ENV, working_dir='/tmp', network_mode='none',
                read_only=True, cap_drop=['ALL'], security_opt=['no-new-privileges=true'],
                pids_limit=64, nano_cpus=1000000000, mem_limit=1073741824, memswap_limit=1073741824,
                ipc_mode='private', cgroupns='private', init=True, tmpfs=TMPFS,
                ulimits=[docker.types.Ulimit(name='nofile', soft=128, hard=128), docker.types.Ulimit(name='core', soft=0, hard=0)],
                mounts=[docker.types.Mount('/input', str(stage), type='bind', read_only=True, propagation='rprivate')],
                restart_policy={'Name': 'no'}, log_config=docker.types.LogConfig(type='none'),
                labels=self._owner_labels(name), name='fa-attempt-'+name,
                stdin_open=False, tty=False, detach=True)
            self.journal.record(request_hash, 'CREATED', {'event':'CREATED_ID_DURABLE','container_id':container.id}, container.id)
            receipt = self._inspect(container, stage, name)
            self.record({'event': 'CREATED_INSPECTED_NOT_STARTED', 'request_sha256': request_hash, **receipt})
            # Attach before start, no race that could discard the first output.
            sock = container.attach_socket(params={'stdout': 1, 'stderr': 1, 'stream': 1})
            active(); container.start(); started = time.monotonic()
            self.journal.record(request_hash, 'RUNNING', {'event':'STARTED','container_id':container.id})
            pending = bytearray()
            done = False
            while not done:
                active(); require(time.monotonic()-started < 60, 'CONTAINER_WALL_EXCEEDED')
                if not select.select([sock], [], [], .1)[0]:
                    continue
                block = sock.read(65536)
                if not block:
                    done = True; break
                pending.extend(block)
                require(len(pending) <= MAX_RESPONSE+262144+16, 'CONTAINER_OUTPUT_EXCEEDED')
                while len(pending) >= 8:
                    stream, length = pending[0], struct.unpack('>I', pending[4:8])[0]
                    require(stream in {1, 2} and pending[1:4] == b'\0\0\0' and length <= STDOUT_LIMIT, 'DOCKER_STREAM_INVALID')
                    if len(pending) < 8+length:
                        break
                    target = stdout if stream == 1 else stderr
                    require(len(target)+length<=(STDOUT_LIMIT if stream==1 else STDERR_LIMIT),'CONTAINER_OUTPUT_EXCEEDED')
                    target.extend(pending[8:8+length]); del pending[:8+length]
                    require(len(stdout)<=STDOUT_LIMIT and len(stderr)<=STDERR_LIMIT,'CONTAINER_OUTPUT_EXCEEDED')
            require(not pending, 'DOCKER_STREAM_TRUNCATED')
            status = container.wait(timeout=3)
            container.reload()
            require(status['StatusCode'] == 0 and not container.attrs['State']['Running'], 'CONTAINER_FAILED')
            # Wait confirms stopped own container; actual cgroup closure/resource
            # negative proof remains FA012 acceptance, never inferred here.
            stopped = True
            self.journal.record(request_hash, 'STOPPED', {'event':'STOPPED','container_id':container.id,'stdout_sha256':hashlib.sha256(stdout).hexdigest()})
            self.record({'event': 'CONTAINER_STOPPED', 'request_sha256': request_hash, 'container_id': container.id,
                         'stdout_bytes': len(stdout), 'stderr_bytes': len(stderr)})
            return bytes(stdout), {'request_sha256': request_hash, 'writers_closed': True, 'owned_tree_empty': True}
        except BaseException:
            self.cleanup_budget.enter()
            raise
        finally:
            try:
                # Drop the unread output connection BEFORE asking Docker to
                # stop. A blocked output consumer must not hold up kill's reply.
                # A close failure still cannot skip exact-owned stop attempts.
                socket_error=None
                if sock is not None:
                    try:
                        self._close_attach(sock)
                    except Exception as error:
                        self.cleanup_budget.enter()
                        socket_error=error
                        self._transport_close_failed=True
                # A failed journal write or ambiguous host ACK must not pick
                # a different terminal receipt for a physically stopped guest.
                if container is not None and not stopped:
                    # Only this just-created opaque owned handle, never an old PID.
                    if self.qa_state is not None:
                        self.cleanup_budget.check()
                        container.reload()
                        self._require_qa_container(container,'fa-attempt-'+name,request_hash,container.id,
                                                   code='OWNED_CONTAINER_IDENTITY_MISMATCH')
                    status,failures=self._stop_owned(container)
                    if socket_error is not None:
                        raise Rejected('CONTAINER_CLEANUP_UNPROVEN') from socket_error
                    self.journal.record(request_hash,status,
                                        {'event':'FAILED_RETAINED_CLOSED','container_id':container.id,
                                         'cleanup_failures':failures})
                    self.record({'event': 'FAILED_ATTEMPT_RETAINED_STOPPED', 'request_sha256': request_hash, 'container_id': container.id})
                elif container is None:
                    # Lost create reply may still mean an existing container.
                    self.journal.record(request_hash, 'CREATE_OUTCOME_UNKNOWN', {'event':'CREATE_OUTCOME_UNKNOWN','name':'fa-attempt-'+name})
                elif socket_error is not None:
                    raise Rejected('CONTAINER_CLEANUP_UNPROVEN') from socket_error
            finally:
                # Evidence IO happens AFTER owned cleanup; a full evidence
                # disk must not skip kill/wait and leave a running writer.
                for filename, data in (('stdout.bin',stdout),('stderr.bin',stderr)):
                    self.cleanup_budget.check()
                    with (output_dir/filename).open('xb') as stream:
                        stream.write(data); stream.flush(); os.fsync(stream.fileno())
                    self.cleanup_budget.check()

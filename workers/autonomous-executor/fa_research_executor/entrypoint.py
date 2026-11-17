"""Trusted Linux stdio RPC entrypoint, configured only by FA010 deployment."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import re
import time
from .protocol import (BOOTSTRAP_DIAGNOSTIC_CODES, BOOTSTRAP_DIAGNOSTIC_PREFIX,
                       require, strict_json, digest, Rejected)
from .bridge import FramedChannel, LinuxHostRPC

from .paths import executor_root, verified_paths
CONFIG = executor_root()/'deployment/executor-config.json'


class BoundActiveReads:
    """Coalesce only true active reads, never a mutation or another binding."""
    def __init__(self,rpc,*,binding,runtime_sha256,local_check,transport_check,cancel_research,clock=time.monotonic,cleanup_budget=None):
        self.rpc=rpc;self.binding_sha256=digest(binding);self.runtime_sha256=runtime_sha256
        self.local_check=local_check;self.transport_check=transport_check
        self.cancel_research=cancel_research;self.clock=clock
        self.valid_until=None;self.cleanup_only=False;self.remote_stopped=False
        self.cleanup_budget=cleanup_budget
        self.counts={'local_checks':0,'remote_calls':0,'cache_hits':0,'remote_failures':0,
                     'remote_true_replies':0,'typed_stopped_replies':0,
                     'invalid_replies':0,'invalidations':0,'remote_milliseconds':0}

    def invalidate(self):
        self.valid_until=None;self.counts['invalidations']+=1

    def refresh_active(self):
        self.invalidate()
        return self.active()

    def begin_cleanup(self):
        self.invalidate();self.cleanup_only=True
        if self.cleanup_budget is not None:self.cleanup_budget.enter()

    def received_cleanup(self):
        self.remote_stopped=True;self._reject()

    def _binding(self):
        require(digest(self.rpc.binding)==self.binding_sha256,'BRIDGE_BINDING_MISMATCH')

    def _reject(self):
        self.begin_cleanup();self.cancel_research()

    def active(self):
        self.counts['local_checks']+=1
        try:
            self._binding();self.local_check()
            require(not self.cleanup_only,'LINUX_RESEARCH_CLEANUP_ONLY')
            started=self.clock()
            if self.valid_until is not None and started<self.valid_until:
                self.counts['cache_hits']+=1
                return True
            self.invalidate();self.counts['remote_calls']+=1
            try:
                value=self.rpc.call('active')
            except BaseException:
                self.counts['remote_failures']+=1
                raise
            finally:
                self.counts['remote_milliseconds']+=max(0,int((self.clock()-started)*1000))
            if not(type(value)is dict and set(value)=={'active'} and value['active']is True):
                self.counts['invalid_replies']+=1
                raise Rejected('BRIDGE_ACTIVE_REPLY_INVALID')
            self._binding();self.local_check()
            self.counts['remote_true_replies']+=1
            # Conservative anchor: never refresh from response time/mutation.
            self.valid_until=started+.5 if self.clock()<started+.5 else None
            return True
        except BaseException as error:
            if type(error)is Rejected and str(error)=='BRIDGE_CLEANUP_ONLY':
                self.counts['typed_stopped_replies']+=1;self.remote_stopped=True
            self._reject()
            raise

    def call(self,operation,value=None):
        try:
            require(operation!='active','ACTIVE_READ_ENTRY_REQUIRED')
            self._binding();self.transport_check()
            event=value.get('event') if operation=='controller_record' and type(value)is dict else None
            terminal=event in {'CONTAINER_STOPPED','FAILED_ATTEMPT_RETAINED_STOPPED','HELPER_CLOSED'}
            late_created=event=='CREATED_INSPECTED_NOT_STARTED'
            if not self.cleanup_only:
                try:self.local_check()
                except BaseException:
                    self._reject()
                    if not(terminal or late_created):raise
            if self.cleanup_only:
                require(terminal or late_created,'LINUX_RESEARCH_CLEANUP_ONLY')
            result=self.rpc.call(operation,value)
            self.transport_check()
            if self.cleanup_only and late_created:
                # Receipt-only late registration must NEVER authorize start,
                # even if an incompatible peer mistakenly returns normal ACK.
                raise Rejected('LATE_CREATED_NO_START_AUTHORIZATION')
            if not self.cleanup_only:self.local_check()
            return result
        except BaseException as error:
            if type(error)is Rejected and str(error)=='BRIDGE_CLEANUP_ONLY':
                self.counts['typed_stopped_replies']+=1;self.remote_stopped=True
            self._reject()
            raise

    def snapshot(self):
        return {'max_age_ms':500,**self.counts}


def main():
    require(sys.platform == 'linux', 'LINUX_ORCHESTRATOR_REQUIRED')
    qa_state = None
    if sys.argv[1:]:
        from .qa_state import load_qa_state
        from .qa_scope_contract import HOST_VENV
        qa_state = load_qa_state(sys.argv[1:])
        require(Path(sys.prefix) == qa_state.run_root / HOST_VENV, 'QA_HOST_ENVIRONMENT_MISMATCH')
        config_bytes, manifest_bytes = qa_state.config_and_manifest()
    else:
        require(CONFIG.is_file() and not CONFIG.is_symlink(), 'TRUSTED_EXECUTOR_CONFIG_REQUIRED')
        config_bytes = CONFIG.read_bytes()
        manifest_path = CONFIG.parent / 'runtime-manifest.json'
        require(manifest_path.is_file() and not manifest_path.is_symlink(), 'RUNTIME_MANIFEST_REQUIRED')
        manifest_bytes = manifest_path.read_bytes()
    config = strict_json(config_bytes, 65536)
    require(set(config) == {'daemon_url','image_digest','runtime_sha256','staging_root','approved_image_env'}, 'EXECUTOR_CONFIG_INVALID')
    manifest = strict_json(manifest_bytes, 1048576)
    require(set(manifest)=={'schema','package_files','dependencies','image_digest'}
            and manifest['schema']=='fa-native-runtime-manifest-1' and digest(manifest)==config['runtime_sha256']
            and manifest['image_digest']==config['image_digest'],'RUNTIME_MANIFEST_BINDING_MISMATCH')
    verified_paths(manifest)
    from .legacy_bindings import configure as configure_legacy
    configure_legacy(manifest)
    import hashlib
    package=Path(__file__).resolve().parent
    expected=manifest['package_files']
    require(type(expected)is dict and set(expected)=={p.name for p in package.glob('*.py')},'RUNTIME_SOURCE_SET_MISMATCH')
    for name,sha in expected.items():
        path=package/name
        require(path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest()==sha,'RUNTIME_SOURCE_CHANGED')
    from .controller import STAGING_ROOT
    from .bootstrap import DAEMON
    expected_daemon = DAEMON if qa_state is None else qa_state.daemon_url
    require(config['daemon_url']==expected_daemon,'EXECUTOR_SOCKET_IDENTITY_MISMATCH')
    require(Path(config['staging_root'])==STAGING_ROOT,'STAGING_ROOT_INVALID')
    if qa_state is not None:
        require(config['runtime_sha256']==qa_state.identity.runtime_sha256
                and config['image_digest']==qa_state.identity.image_digest, 'QA_SCOPE_RUNTIME_MISMATCH')
        if qa_state.cold is not None:
            import os
            os.umask(0o077)
    with qa_state.claim() if qa_state is not None else nullcontext():
        _run(config, qa_state)


def _model_request(binding, ordinal, model):
    attempt_id = binding['attempt_id']
    request = {'call_id':attempt_id+':model:'+str(ordinal), 'attempt_id':attempt_id,
               'role':model['role'], 'messages':model['messages'], 'json_mode':model['json_mode']}
    if 'history_groups' in model:
        request['history_groups'] = model['history_groups']
    return request


def _run(config, qa_state):
    from .supervisor import InputPump, supervised,CleanupBudget
    import os
    cleanup=CleanupBudget();cleanup.install_kernel_enforcement()
    pump=InputPump(sys.stdin.buffer,cleanup_budget=cleanup)
    channel = FramedChannel(pump, sys.stdout.buffer)
    # RD-Agent/native diagnostics must never corrupt the framed stdout stream.
    sys.stdout = sys.stderr
    start = channel.receive()
    require(set(start) == {'schema','binding','begun','runtime_sha256'} and start['schema'] == 'fa-rpc-start-1', 'BRIDGE_START_INVALID')
    require(start['runtime_sha256']==config['runtime_sha256'],'BRIDGE_RUNTIME_MISMATCH')
    binding = start['binding']; begun = start['begun']
    rpc = LinuxHostRPC(channel,binding,cleanup_budget=cleanup,clock=cleanup.clock)
    require(binding['session_id'] == begun['session_id'] and binding['attempt_id'] == begun['attempt_id'], 'BRIDGE_BINDING_MISMATCH')
    controller=None
    business_deadline=time.monotonic()+begun['reservation']['compute_seconds']
    if qa_state is not None:
        business_deadline = min(business_deadline, time.monotonic() + qa_state.identity.expires_at - time.time())
    cleanup.bind_business_deadline(business_deadline)
    def transport_check():
        cleanup.check()
        require(not pump.stop.is_set() and not pump.eof.is_set(),'LINUX_SUPERVISOR_CANCELLED')
    def local_check():
        transport_check()
        require(not pump.research_stop.is_set()
                and (controller is None or not controller.cancelled.is_set()),'LINUX_SUPERVISOR_CANCELLED')
        require(time.monotonic()<business_deadline,'EXPERIMENT_WALL_BUDGET_EXCEEDED')
        require(pump.deadline is None or time.monotonic()<pump.deadline,'EXECUTOR_DAEMON_READY_TIMEOUT')
    reads=BoundActiveReads(rpc,binding=binding,runtime_sha256=config['runtime_sha256'],
        local_check=local_check,transport_check=transport_check,cancel_research=pump.research_stop.set,cleanup_budget=cleanup)
    rpc.on_cleanup=reads.received_cleanup
    active=reads.active
    context = SimpleNamespace(job_id=binding['job_id'],fencing_token=binding['fencing_token'],
                              run=SimpleNamespace(run_id=binding['run_id']), require_active=active,
                              activity_counters=reads.snapshot)
    from .controller import DockerController, STAGING_ROOT
    from .driver import LinuxResearchDriver
    from .bootstrap import ensure_daemon, verify_qa_daemon
    if qa_state is None:
        STAGING_ROOT.mkdir(mode=0o700,parents=False,exist_ok=True)
    setup_error=None
    try:
        pump.deadline=time.monotonic()+43
        if qa_state is None:
            ensure_daemon(active)
        elif qa_state.cold is not None:
            qa_state.cold.bind(binding,begun['phase'])
            ensure_daemon(active,cold=qa_state.cold,cleanup=cleanup,
                          deadline=min(pump.deadline,business_deadline),final_active=reads.refresh_active)
        else:
            verify_qa_daemon(qa_state, active)
    except Exception as error:
        setup_error=error
        code=str(error) if type(error) is Rejected else ''
        if code not in BOOTSTRAP_DIAGNOSTIC_CODES:
            code='EXECUTOR_BOOTSTRAP_FAILED'
        # This is diagnostic only. The original failure and closure handling
        # below still decide the outcome, and missing closure stays unknown.
        try:
            marker=BOOTSTRAP_DIAGNOSTIC_PREFIX.decode('ascii')
            sys.stderr.write(f'{marker}{digest(binding)} {code}\n')
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
    finally:pump.deadline=None
    if setup_error is not None and qa_state is not None and qa_state.cold is not None:
        # No trusted controller was obtained. In particular, terminating the
        # start client never authorizes the ordinary no-effect closure receipt.
        raise setup_error
    import os
    os.environ.update({'LITELLM_LOCAL_MODEL_COST_MAP':'True','PYTHON_DOTENV_DISABLED':'1','OTEL_SDK_DISABLED':'true',
                       'MLFLOW_DISABLE_TELEMETRY':'true','LANGSMITH_TRACING':'false','LANGCHAIN_TRACING_V2':'false'})
    calls = 0
    def model(**kwargs):
        nonlocal calls
        calls += 1
        return reads.call('model_call', _model_request(binding, calls, kwargs))['content']
    try:
        if setup_error is not None:raise setup_error
        controller = DockerController(**config,record=lambda event:reads.call('controller_record',event),
                                      cleanup_budget=cleanup,qa_state=qa_state)
        driver = LinuxResearchDriver(controller=controller, broker=model,
            export_input=lambda context,begun:reads.call('export_input'),
            resolve_roles=lambda context,spec:reads.call('resolve_roles'),
            committed_history=lambda context,session:reads.call('committed_history'),
            reserve_prefix=lambda context,begun,symbol,index,sha:reads.call('reserve_prefix',{'symbol':symbol,'index':index,'request_hash':sha}),
            commit_stage_artifacts=lambda context,begun,artifacts:reads.call('commit_stage_artifacts',artifacts),
            progress=lambda context,stage,**details:reads.call('progress',{'stage':stage,**details}))
        seconds = begun['reservation']['compute_seconds']
        if qa_state is not None:
            seconds = min(seconds, max(0.001, business_deadline-time.monotonic()))
        outcome = supervised(lambda:driver.run(context,begun),pump=pump,controller=controller,
                             seconds=seconds,cleanup_budget=cleanup)
    except Exception as error:
        # Detailed native exception belongs in bounded owned diagnostic stderr,
        # never raw exception text in product/Core outcome or model prompt.
        import traceback
        traceback.print_exc(limit=12, file=sys.stderr)
        from .artifacts import single
        from .capacity import CHINESE_REASONS
        import re
        code = str(error) if type(error) is Rejected else 'AUTONOMOUS_EXPERIMENT_FAILED'
        if re.fullmatch('[A-Z][A-Z0-9_]{0,63}',code) is None:
            code = 'AUTONOMOUS_EXPERIMENT_FAILED'
        outcome = {'status':'FAILED','error_code':code,'metrics':{},
                   'artifacts':[single('evaluation','application/json',{'status':'FAILED','code':code,
                       'message_zh':CHINESE_REASONS.get(code,'本次研究未完成，原记录已保留。')},'failure')]}
    finally:
        reads.begin_cleanup()
    try:
        if controller is not None:controller.close()
    except BaseException:
        # A stuck trusted worker or unknown container cannot publish HELPER_CLOSED.
        # Preserve journal and end this exact helper without waiting on threads.
        import os
        os._exit(2)
    # Only this trusted orchestrator's own direct children, never host process
    # enumeration/adoption. Container closure comes from its durable journal.
    for task in Path('/proc/self/task').iterdir():
        cleanup.check()
        children=task/'children'
        if children.exists():
            require(not children.read_text().strip(),'LINUX_HELPER_CHILDREN_STILL_ACTIVE')
    from .closure import persist_local_closure
    # Durable local proof precedes host ACK. A no-plan/no-effect failure uses
    # only the existing bounded immutable journal, never an orphan file.
    local_closure=persist_local_closure(controller,binding,config['runtime_sha256'],
        {'binding_sha256':digest(binding),'runtime_sha256':config['runtime_sha256'],
         'helpers_closed':True,'containers_closed':True,'pipe_eof':pump.eof.is_set(),
         'active_read_counters':reads.snapshot(),'cleanup_budget':cleanup.snapshot()},cleanup=cleanup)
    if controller is not None:controller.finalize_plan()
    if pump.stop.is_set() or pump.eof.is_set():
        # Local durable proof survives broken host IO. Never publish success or
        # deadlock waiting for a reply on the already closed cancellation pipe.
        os._exit(1)
    reads.call('controller_record',{'event':'HELPER_CLOSED','binding_sha256':digest(binding),
             'runtime_sha256':config['runtime_sha256'],'helpers_closed':True,'containers_closed':True})
    if reads.remote_stopped or time.monotonic()>=business_deadline:
        os._exit(1)
    rpc.outcome(outcome)
    # Host closes stdin after consuming outcome. Join the one transport-only
    # reader before interpreter shutdown; no buffered daemon reader is left.
    cleanup.join(pump.thread,1)
    if pump.thread.is_alive():
        os._exit(2)
    if qa_state is not None and qa_state.cold is not None and outcome['status']=='SUCCEEDED':
        qa_state.cold.record_closed(local_closure,cleanup)
    cleanup.finish()


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        import os
        os._exit(2)

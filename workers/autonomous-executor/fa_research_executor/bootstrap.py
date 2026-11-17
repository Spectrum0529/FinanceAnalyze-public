"""Explicit-attempt-only readiness of one existing pinned user unit.

No daemon install, context switch, old PID adoption or second dockerd process.
"""
import hashlib
import os
from pathlib import Path
import selectors
import stat
import subprocess
import time
from .protocol import require

from .paths import executor_root, user_unit_directory
ROOT=executor_root()
UNIT=user_unit_directory()/'financeanalyze-research-docker-20261106.service'
UNIT_SHA='0f58a59525c7048e78aa276bbb20051b39cd37a2abf43317f6b609b553c7e37c'
SOCKET=Path('/run/user/1000/finance-research-20261106/docker.sock')
DAEMON='unix://'+str(SOCKET)
ARGV=['/usr/bin/systemctl','--user','start','financeanalyze-research-docker-20261106.service']


def _verify_profile(data, docker_root):
    require(data['DockerRootDir']==str(docker_root) and data['ServerVersion']=='29.9.3'
            and data['CgroupDriver']=='systemd' and data['CgroupVersion']=='2'
            and 'name=rootless' in data['SecurityOptions'] and 'name=seccomp,profile=builtin' in data['SecurityOptions']
            and all(data.get(k) is True for k in ('MemoryLimit','SwapLimit','CpuCfsPeriod','CpuCfsQuota','PidsLimit')),
            'EXECUTOR_DAEMON_PROFILE_MISMATCH')


def verify_qa_daemon(state, active):
    """FA010 owns the transient unit. The helper verifies its exact live socket."""
    state.verify_daemon_socket()
    active()
    import docker
    client = docker.DockerClient(base_url=state.daemon_url, version='1.45', timeout=3)
    try:
        _verify_profile(client.info(), state.docker_data)
        state.verify_daemon_socket()
        active()
    finally:
        client.close()


def ensure_daemon(active, *, cold=None, cleanup=None, deadline=None, final_active=None):
    if cold is None:
        return _ensure_daemon(active)
    require(cleanup is not None and deadline is not None,'QA_COLD_BOOTSTRAP_BOUND_REQUIRED')
    try:
        return _ensure_daemon(active,cold=cold,cleanup=cleanup,deadline=deadline,final_active=final_active)
    except Exception as error:
        try:
            cold.reconcile(cleanup,error)
        except Exception as reconcile_error:
            error.add_note('Cold reconciliation did not produce a terminal receipt: '+type(reconcile_error).__name__)
        raise


def _ensure_daemon(active, *, cold=None, cleanup=None, deadline=None, final_active=None):
    root,unit,unit_sha,socket,daemon,docker_root,argv,home = (
        (ROOT,UNIT,UNIT_SHA,SOCKET,DAEMON,ROOT/'runtime/docker-data',ARGV,ROOT/'runtime/home')
        if cold is None else (cold.state.run_root,cold.unit_file,cold.state.identity.unit_sha256,
            cold.state.socket,cold.state.daemon_url,cold.state.docker_data,
            ['/usr/bin/systemctl','--user','--show-transaction','--job-mode=fail','start',cold.unit_name],
            cold.state.run_root/'daemon-home'))
    started=time.monotonic()
    absolute=started+43 if deadline is None else min(started+43,deadline)
    def limit(maximum):
        remaining=absolute-time.monotonic()
        require(remaining>0,'EXECUTOR_DAEMON_READY_TIMEOUT')
        return min(maximum,remaining)
    def check():
        active()
        limit(43)
    check()
    if cold is None:
        require(unit.is_file() and not unit.is_symlink(),'EXECUTOR_UNIT_IDENTITY_MISMATCH')
        require(hashlib.sha256(unit.read_bytes()).hexdigest()==unit_sha,'EXECUTOR_UNIT_IDENTITY_MISMATCH')
    else:
        cold.verify_recipe()
    bus=Path('/run/user/1000/bus')
    while not bus.exists():
        check();time.sleep(.1)
    require(stat.S_ISSOCK(bus.lstat().st_mode) and bus.lstat().st_uid==1000,'EXECUTOR_USER_BUS_IDENTITY_MISMATCH')
    env={'PATH':'/usr/bin:/bin','HOME':str(home),'XDG_RUNTIME_DIR':'/run/user/1000',
         'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus','LANG':'C.UTF-8'}
    if cold is not None:
        cold.before_start(check,limit)
        env.update(SYSTEMD_COLORS='0',SYSTEMD_LOG_TARGET='console',LC_ALL='C')
        cold.dispatch(check,limit)
    _start_unit(argv,env,root,check,limit,cold=cold,cleanup=cleanup)
    import docker
    import requests
    while True:
        check()
        if socket.exists():
            info=socket.lstat()
            require(stat.S_ISSOCK(info.st_mode) and info.st_uid==1000,'EXECUTOR_SOCKET_IDENTITY_MISMATCH')
            if cold is not None:
                if cold.generation is None and not cold.capture(limit):
                    time.sleep(limit(.1))
                    continue
                cold.verify(limit(1))
            client=None
            try:
                timeout=min(2,max(.1,43-(time.monotonic()-started))) if cold is None else limit(2)
                client=docker.DockerClient(base_url=daemon,version='1.45',timeout=timeout)
                data=client.info()
                _verify_profile(data, docker_root)
                check()
                if cold is not None:
                    image=cold.state.identity.image_digest
                    client.api.timeout=limit(2)
                    require(client.images.get(image).id==image,'IMAGE_IDENTITY_MISMATCH')
                    check()
                    profile={key:data[key] for key in ('DockerRootDir','ServerVersion','CgroupDriver',
                        'CgroupVersion','SecurityOptions','MemoryLimit','SwapLimit','CpuCfsPeriod','CpuCfsQuota','PidsLimit')}
                    cold.ready(time.monotonic()-started,profile,limit)
                receipt={'unit_sha256':unit_sha,'daemon':daemon,'duration_seconds':time.monotonic()-started,
                         'status':'ADVERTISED_READY_NOT_ISOLATION_ACCEPTANCE'}
            except (docker.errors.DockerException,requests.exceptions.RequestException):
                check()
            else:
                if cold is None:
                    return receipt
                # The final check runs after ready and after client.close below.
                break
            finally:
                if client is not None:client.close()
        time.sleep(.1 if cold is None else limit(.1))
    cold.finish_ready(limit)
    limit(43)
    (final_active or active)()
    limit(43)
    return receipt


def _start_unit(argv,env,root,check,limit,*,cold,cleanup):
    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             env=env,close_fds=True,cwd=root)
    selector=None
    output=0
    stderr=bytearray()
    stderr_nonblocking=False
    try:
        selector=selectors.DefaultSelector()
        for stream in (process.stdout,process.stderr):
            os.set_blocking(stream.fileno(),False)
            if stream is process.stderr:stderr_nonblocking=True
            selector.register(stream,selectors.EVENT_READ)
        while process.poll() is None or selector.get_map():
            check()
            for key,_ in selector.select(.1 if cold is None else limit(.1)):
                block=os.read(key.fileobj.fileno(),4096)
                if not block:selector.unregister(key.fileobj)
                output+=len(block);require(output<=65536,'EXECUTOR_UNIT_OUTPUT_LIMIT')
                if cold is not None and key.fileobj is process.stderr:
                    stderr.extend(block)
        require(process.returncode==0,'EXECUTOR_UNIT_START_FAILED')
    finally:
        try:
            if selector is not None:selector.close()
            if process.poll() is None:
                if cold is not None:
                    cleanup.enter()
                process.terminate()
                wait=lambda:1 if cold is None else cleanup.timeout(1)
                try:process.wait(timeout=wait())
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=wait())
        finally:
            if cold is not None:
                reaped=process.poll() is not None
                if reaped and stderr_nonblocking and output<=65536 and len(stderr)<65536:
                    try:
                        stderr.extend(os.read(process.stderr.fileno(),65537-len(stderr)))
                    except (OSError, ValueError):
                        pass
                cold.observe_client(stderr if output<=65536 and len(stderr)<=65536 else b'',reaped)
            for stream in (process.stdout,process.stderr):stream.close()
    if cold is not None:
        require(cold.job_id is not None,'QA_COLD_START_OWNERSHIP_UNKNOWN')

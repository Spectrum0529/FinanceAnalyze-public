"""Fixed owned export bootstrap; original Qlib dump/pools and local provider API."""
import csv
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

HERE=Path(__file__).resolve().parent
REPO=Path(os.environ['FA014_RUNTIME_ROOT']);RUN=Path(os.environ['FA014_OWNED_ATTEMPT'])
if not REPO.is_absolute() or not RUN.is_absolute() or '..' in RUN.parts or not RUN.is_relative_to(REPO/'data/development/qlib-work'):raise RuntimeError('OWNED_RUNTIME_ROOT_REQUIRED')
BINDING=RUN/'dump_binding.json';binding_raw=BINDING.read_bytes()
if hashlib.sha256(binding_raw).hexdigest()!=os.environ['FA014_DUMP_BINDING_SHA256']:raise RuntimeError('SERVER_DUMP_BINDING_CHANGED')
binding=json.loads(binding_raw)
if Path(binding['runtime_root'])!=REPO:raise RuntimeError('SERVER_DUMP_ROOT_CHANGED')
launch_path=HERE/'qlib_dump_domain.py';launch_raw=launch_path.read_bytes();launch_pin=binding['launch_contract']
if len(launch_raw)!=launch_pin['bytes'] or hashlib.sha256(launch_raw).hexdigest()!=launch_pin['sha256']:raise RuntimeError('LAUNCH_CONTRACT_CHANGED')
launch_spec=importlib.util.spec_from_file_location('fixed_dump_launch_contract',launch_path)
launch=importlib.util.module_from_spec(launch_spec);launch_spec.loader.exec_module(launch)
LAUNCH_CWD=Path.cwd().resolve()
RUN=launch.locate_attempt(REPO,LAUNCH_CWD,str(RUN))
request_raw=(RUN/'request.json').read_bytes();request=json.loads(request_raw)
if request['binding_sha256']!=hashlib.sha256(binding_raw).hexdigest():raise RuntimeError('BINDING_CHANGED')
owned=request['owned_input'];BASE=Path(binding['base_python']['path']);SITE=Path(binding['site_root'])
launch.assert_owned_layout(REPO,LAUNCH_CWD,RUN,owned,request['attempt_id'])
CSV=Path(owned['CSV_directory']);OUTPUT=RUN/'native';ORIGINAL=Path(binding['official_dump']['path'])
if not CSV.is_relative_to(Path(owned['directory'])):raise RuntimeError('OWNED_INPUT_REQUIRED')
# Exact short work mapping and unchanged logical input layout are checked by launch.
if (owned['owner_id']!=request['owner_id'] or owned['run_id']!=request['run_id']
    or owned['logical_data_hash']!=binding['logical_data_hash']):raise RuntimeError('OWNED_INPUT_REQUIRED')
is_root=__name__=='__main__' 

diagnostic_path=HERE/'qlib_dump_trace.py';diagnostic_raw=diagnostic_path.read_bytes();diagnostic_pin=binding['diagnostic_source']
if len(diagnostic_raw)!=diagnostic_pin['bytes'] or hashlib.sha256(diagnostic_raw).hexdigest()!=diagnostic_pin['sha256']:raise RuntimeError('DIAGNOSTIC_SOURCE_CHANGED')
diagnostic_spec=importlib.util.spec_from_file_location('fixed_phase_trace',diagnostic_path)
diagnostics=importlib.util.module_from_spec(diagnostic_spec);diagnostic_spec.loader.exec_module(diagnostics)
phase=diagnostics.PhaseTrace(RUN,is_root);phase.emit('ENTRY_VALIDATED');phase.arm()
def pin(path):
    raw=path.read_bytes();return {'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
def equal_pin(actual,expected):return actual['bytes']==expected['bytes'] and actual['sha256']==expected['sha256']
phase.emit('INITIAL_PINS_BEGIN')
for pin_index,entry in enumerate(binding['child_sources']):
    if not equal_pin(pin(REPO/entry['path']),entry):raise RuntimeError('CHILD_SOURCE_CHANGED')
    phase.emit('INITIAL_PIN_DONE',pin_index)
phase.emit('INITIAL_PINS_DONE')
if not equal_pin(pin(BASE),binding['base_python']) or not equal_pin(pin(Path(binding['python_dll']['path'])),binding['python_dll']):raise RuntimeError('RUNTIME_CHANGED')
if not equal_pin(pin(Path(owned['input_manifest'])),{'bytes':Path(owned['input_manifest']).stat().st_size,'sha256':owned['owner_manifest_sha256']}):raise RuntimeError('OWNER_MANIFEST_CHANGED')
is_root=__name__=='__main__'
bootstrap_path=REPO/binding['identity_helper']['path']
spec=importlib.util.spec_from_file_location('fixed_qlib_identity_helper',bootstrap_path)
identity_helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(identity_helper)
if sys.version_info[:3]!=(3,12,14) or not sys.flags.isolated or not sys.flags.no_site:raise RuntimeError('DIRECT_BASE_FLAGS_REQUIRED')
sys.path[:0]=[str(SITE),str(ORIGINAL.parent)]
sys.prefix=sys.exec_prefix=str(Path(binding['venv_prefix']))
if is_root:
    phase.emit('WAIT_QUERY')
    raw=sys.stdin.buffer.readline(65537)
    phase.emit('QUERY_RECEIVED')
    if len(raw)>65536 or not raw.endswith(b'\n'):raise RuntimeError('BOUNDED_GO_REQUIRED')
    go=json.loads(raw)
    if set(go)!={'phase','job_handle','expected_pid','request_sha256'} or go['phase']!='QUERY' or go['request_sha256']!=hashlib.sha256(request_raw).hexdigest():raise RuntimeError('FIXED_GO_REQUIRED')
    identity=identity_helper.own_identity(go['job_handle'])
    ready={'phase':'READY',**identity,'request_sha256':go['request_sha256'],
        'image_sha256':pin(Path(identity['actual_image']))['sha256'],
        'dll_sha256':pin(Path(identity['actual_python_dll']))['sha256'],
        'site_root':str(SITE.resolve()),'prefix':str(Path(sys.prefix).resolve()),
        'base_prefix':str(Path(sys.base_prefix).resolve()),'qlib_imported':False}
    if identity['actual_pid']!=go['expected_pid']:raise RuntimeError('PID_IDENTITY_REQUIRED')
    if any(k.split('.')[0] in {'qlib','numpy','pandas','mlflow'} for k in sys.modules):raise RuntimeError('NO_BUSINESS_IMPORT_BEFORE_EXEC')
    print(json.dumps(ready),flush=True)
    phase.emit('READY_SENT');phase.emit('WAIT_EXEC')
    if sys.stdin.buffer.readline(6)!=b'EXEC\n':raise RuntimeError('EXEC_REQUIRED')
    phase.emit('EXEC_RECEIVED');phase.emit('POSTEXEC_PINS_BEGIN')
    for pin_index,entry in enumerate(binding['child_sources']):
        if not equal_pin(pin(REPO/entry['path']),entry):raise RuntimeError('POST_READY_SOURCE_CHANGED')
        phase.emit('POSTEXEC_PIN_DONE',pin_index)
    phase.emit('POSTEXEC_PINS_DONE')

# Every original pool helper reexecutes this file as __mp_main__. All install
# the same audit policy before importing dump_bin/Qlib/data. No pool is replaced.
def image_identity():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetModuleHandleW.argtypes=[wintypes.LPCWSTR];kernel.GetModuleHandleW.restype=wintypes.HANDLE
    kernel.GetModuleFileNameW.argtypes=[wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD];kernel.GetModuleFileNameW.restype=wintypes.DWORD
    def name(handle):
        buf=ctypes.create_unicode_buffer(32768)
        if not 0<kernel.GetModuleFileNameW(handle,buf,len(buf))<len(buf):raise RuntimeError('IMAGE_QUERY_FAILED')
        return str(Path(buf.value).resolve())
    image=name(None);dll=name(kernel.GetModuleHandleW('python312.dll'))
    return {'pid':os.getpid(),'image':image,'dll':dll,'image_sha256':pin(Path(image))['sha256'],
        'dll_sha256':pin(Path(dll))['sha256'],'site_root':str(SITE.resolve()),'prefix':str(Path(sys.prefix).resolve()),
        'role':'root' if is_root else 'official_pool_helper'}
current_identity=image_identity()
pool_path=HERE/'qlib_dump_pool_gate.py'
pool_spec=importlib.util.spec_from_file_location('owned_canonical_pool_gate',pool_path)
pool_module=importlib.util.module_from_spec(pool_spec);pool_spec.loader.exec_module(pool_module)
import _winapi
pool_gate=pool_module.CanonicalPoolGate(BASE,os.getpid(),is_root)
pool_gate.install(_winapi)
phase.emit('AUDIT_INIT_BEGIN')
refusal_path=HERE/'qlib_dump_optional_ipv6.py'
refusal_spec=importlib.util.spec_from_file_location('fixed_optional_ipv6_refusal',refusal_path)
refusal_module=importlib.util.module_from_spec(refusal_spec);refusal_spec.loader.exec_module(refusal_module)
refusal=refusal_module.OptionalIPv6Refusal(RUN,binding['optional_probe_sources'],is_root=is_root)
sys.addaudithook(refusal.hook)
import atexit
atexit.register(refusal.close)
log=(RUN/('audit-'+str(os.getpid())+'.jsonl')).open('x',encoding='utf-8');busy=False;spawn_count=0
def record(event,status):
    global busy
    busy=True
    try:log.write(json.dumps({'event':event,'status':status})+'\n');log.flush()
    finally:busy=False
read_roots=[SITE,BASE.parent,ORIGINAL.parent,CSV,RUN,Path('C:/Windows')]
read_files={BINDING.resolve(),Path(__file__).resolve(),bootstrap_path.resolve(),Path(owned['input_manifest']).resolve(),LAUNCH_CWD}
write_roots=[RUN/'home',RUN/'tmp',OUTPUT]
def allowed(raw,write=False):
    if isinstance(raw,int):return True
    if os.fsdecode(raw)==os.devnull:return True
    path=Path(os.fsdecode(raw)).resolve()
    if not write and path in read_files:return True
    if write and (path==RUN/('identity-'+str(os.getpid())+'.json') or (is_root and path==RUN/'native_verification.json')):return True
    return any(path.is_relative_to(root.resolve()) for root in (write_roots if write else read_roots))
def tokens(command):
    shell=ctypes.WinDLL('shell32',use_last_error=True);count=ctypes.c_int()
    shell.CommandLineToArgvW.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype=ctypes.POINTER(wintypes.LPWSTR)
    argv=shell.CommandLineToArgvW(command,ctypes.byref(count))
    if not argv:raise RuntimeError('SPAWN_PARSE_FAILED')
    try:return [argv[i] for i in range(count.value)]
    finally:
        kernel=ctypes.WinDLL('kernel32');kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree(argv)
hostname_calls=0
def audit(event,values):
    global spawn_count,hostname_calls
    if busy:return
    denied=False
    if event=='socket.gethostname' and is_root and hostname_calls<16:
        hostname_calls+=1
        if hostname_calls==1:record('local_hostname_metadata','PERMITTED_NO_NETWORK')
        return
    if event.startswith('socket.') or event in {'urllib.Request','http.client.connect','sqlite3.connect','subprocess.Popen','os.system','os.posix_spawn','os.spawn'}:denied=True
    elif event=='_winapi.CreateProcess':
        permitted=pool_gate.audit(values)
        if permitted:spawn_count+=1;record(event,'PERMITTED_OFFICIAL_POOL_FLAGS_0_TO_DETACHED_8');return
        denied=True
    elif event=='open' and isinstance(values[0],(str,bytes,os.PathLike)):
        mode=values[1] or '';flags=values[2] if isinstance(values[2],int) else 0
        writing=any(c in mode for c in 'wax+') or bool(flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
        denied=not allowed(values[0],writing)
    elif event in {'os.listdir','os.scandir'} and values and values[0] is not None:denied=not allowed(values[0])
    elif event in {'os.mkdir','os.remove','os.rmdir','os.chmod','os.utime','os.truncate'}:denied=not allowed(values[0],True)
    elif event in {'os.rename','os.link','os.symlink'}:denied=not all(allowed(v,True) for v in values[:2])
    elif event=='os.chdir':denied=True
    if denied:record(event,'DENIED');raise RuntimeError('NATIVE_CONTROL_DENIED')
sys.addaudithook(audit)
refusal.install_profile()
phase.emit('AUDIT_INSTALLED')
phase.emit('IDENTITY_BEGIN')
with (RUN/('identity-'+str(os.getpid())+'.json')).open('x',encoding='utf-8') as out:json.dump(current_identity,out)
phase.emit('IDENTITY_DONE');phase.emit('DUMP_IMPORT_BEGIN')
import dump_bin
refusal.verify_after_import()
refusal.close()
phase.emit('DUMP_IMPORT_DONE')
if Path(dump_bin.__file__).resolve()!=ORIGINAL.resolve():raise RuntimeError('ORIGINAL_DUMP_REQUIRED')
if is_root:
    import multiprocessing
    multiprocessing.set_executable(str(BASE))
    expected={}
    phase.emit('CSV_VERIFY_BEGIN')
    for csv_index,entry in enumerate(owned['CSV_files']):
        path=Path(entry['path']);raw=path.read_bytes()
        if not equal_pin(pin(path),entry):raise RuntimeError('OWNED_CSV_CHANGED')
        with path.open(newline='',encoding='utf-8') as stream:rows=list(csv.DictReader(stream))
        dates=[r['date'] for r in rows]
        if len(rows)!=entry['rows'] or dates!=sorted(set(dates)):raise RuntimeError('CSV_DUPLICATE_OR_ORDER')
        expected[entry['symbol']]={row['date']:row for row in rows}
        phase.emit('CSV_ITEM_DONE',csv_index)
    phase.emit('CSV_VERIFY_DONE');phase.emit('DUMP_INIT_BEGIN')
    official_dump=dump_bin.DumpDataAll(data_path=str(CSV),qlib_dir=str(OUTPUT),freq='day',max_workers=1,
        date_field_name='date',symbol_field_name='symbol',file_suffix='.csv',
        include_fields='open,close,high,low,volume,vwap,factor',backup_dir=None)
    phase.emit('DUMP_INIT_DONE');phase.emit('DUMP_CALL_BEGIN')
    official_dump.dump()
    phase.emit('DUMP_CALL_DONE')
    # Official local provider/storage APIs; no hand-written binary reader or init,
    # Recorder, task workflow, fit/predict, SDK/provider network or market claim.
    phase.emit('PROVIDER_IMPORT_BEGIN')
    import numpy as np
    from qlib.data.data import LocalCalendarProvider,LocalFeatureProvider
    from qlib.data.storage.file_storage import FileInstrumentStorage
    import qlib
    from qlib.constant import REG_US
    # Official storage requires initialized C.region/config, even with an explicit URI.
    qlib.init(provider_uri=str(OUTPUT),region=REG_US,kernels=1,joblib_backend='threading',
        expression_cache=None,dataset_cache=None,auto_mount=False,local_cache_path=str(RUN/'home/qlib-cache'),
        exp_manager={'class':'MLflowExpManager','module_path':'qlib.workflow.expm',
            'kwargs':{'uri':(RUN/'home/mlruns').as_uri(),'default_exp_name':'synthetic-provider-check'}})
    phase.emit('PROVIDER_IMPORT_DONE');phase.emit('PROVIDER_VERIFY_BEGIN')
    def backend(name):return {'class':name,'module_path':'qlib.data.storage.file_storage','kwargs':{'provider_uri':str(OUTPUT)}}
    calendar=[d.strftime('%Y-%m-%d') for d in LocalCalendarProvider(backend=backend('FileCalendarStorage')).load_calendar('day',False)]
    if calendar!=owned['calendar']:raise RuntimeError('CALENDAR_MISMATCH')
    instruments=FileInstrumentStorage('all','day',provider_uri=str(OUTPUT)).data
    if set(instruments)!=set(expected):raise RuntimeError('INSTRUMENT_MISMATCH')
    provider=LocalFeatureProvider(backend=backend('FileFeatureStorage'));reads=0;missing={}
    for symbol,rows in expected.items():
        if [(a.strftime('%Y-%m-%d'),b.strftime('%Y-%m-%d')) for a,b in instruments[symbol]]!=[(calendar[0],calendar[-1])]:raise RuntimeError('INSTRUMENT_WINDOW_MISMATCH')
        values={}
        for field in ['open','close','high','low','volume','vwap','factor']:
            series=provider.feature(symbol,'$'+field,0,419,'day');reads+=1
            if list(series.index)!=list(range(420)):raise RuntimeError('NATIVE_INDEX_MISMATCH')
            wanted=np.array([float(rows[day][field]) if day in rows else np.nan for day in calendar],dtype=np.float64)
            actual=series.to_numpy();np.testing.assert_array_equal(np.isnan(actual),np.isnan(wanted))
            np.testing.assert_allclose(actual,wanted,rtol=1e-7,atol=1e-7,equal_nan=True)
            values[field]=actual;missing[symbol]=int(np.isnan(actual).sum())
        wanted_raw=np.array([float(rows[day]['close'])/float(rows[day]['factor']) if day in rows else np.nan for day in calendar])
        np.testing.assert_allclose(values['close'].astype(np.float64)/values['factor'].astype(np.float64),wanted_raw,rtol=1e-7,atol=1e-7,equal_nan=True)
    if missing!={symbol:(1 if symbol=='SYNTHC' else 0) for symbol in expected}:raise RuntimeError('MISSING_SESSION_MISMATCH')
    verification={'status':'PASS_OFFICIAL_LOCAL_PROVIDER_READ','feature_reads':reads,'calendar_dates':len(calendar),
        'instruments':9,'missing_sessions':missing,'input_owner_manifest_sha256':owned['owner_manifest_sha256'],
        'logical_data_hash':owned['logical_data_hash'],'rtol':1e-7,'atol':1e-7,'exact_indices_and_NaN_masks':True,
        'reader':'Original LocalCalendarProvider/LocalFeatureProvider with FileStorage backend; owned-URI qlib.init, no model',
        'native_precision':'official float32, not decimal bit equality','original_spawn_requests':spawn_count}
    with (RUN/'native_verification.json').open('x',encoding='utf-8') as out:json.dump(verification,out,indent=2)
    record('official_dump_and_provider_read','RETURNED_NO_PARENT_PUBLICATION')
    phase.emit('PROVIDER_VERIFY_DONE');phase.emit('COMPLETE');phase.finish()

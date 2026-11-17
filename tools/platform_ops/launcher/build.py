"""Compile-only Windows launcher builder. Stdlib, existing Framework csc, no installs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',type=Path,required=True)
    p.add_argument('--output-directory',type=Path,required=True)
    a=p.parse_args(); project=a.project_root.resolve(); out=a.output_directory.resolve(); source=Path(__file__).resolve().parent
    compiler=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if os.name!='nt' or not compiler.is_file(): raise SystemExit('Existing Windows Framework64 csc.exe is required')
    out.mkdir(parents=True,exist_ok=True)
    targets=['tools/platform_ops/start.ps1','tools/platform_ops/stop.ps1','tools/platform_ops/source-install.ps1',
        'src/finance_platform/operations/source_binding.py','src/finance_platform/operations/launch.py',
        'src/finance_platform/operations/protect-private.ps1']
    pins={name:sha(project/name) for name in targets}
    pins['tools/platform_ops/launcher/bridge.ps1']=sha(source/'bridge.ps1')
    code='namespace FinanceAnalyzeLauncher { static class BuildPins { internal static readonly System.Collections.Generic.Dictionary<string,string> Values = new System.Collections.Generic.Dictionary<string,string> {\n'
    code+=''.join('{"'+name+'","'+digest+'"},\n' for name,digest in pins.items())+'}; }}\n'
    generated=out/'BuildPins.cs'; exe=out/'FinanceAnalyze.exe'
    if exe.exists() or generated.exists(): raise SystemExit('Exclusive build outputs already exist; use a new output directory')
    generated.write_text(code,encoding='utf-8')
    argv=[str(compiler),'/nologo','/target:winexe','/platform:x64','/optimize+','/codepage:65001',
        '/out:'+str(exe),'/win32manifest:'+str(source/'launcher.manifest'),
        '/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll','/reference:System.Web.Extensions.dll',
        str(source/'Program.cs'),str(generated)]
    started=datetime.now(timezone.utc).isoformat()
    result=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    (out/'compile.stdout.log').write_bytes(result.stdout);(out/'compile.stderr.log').write_bytes(result.stderr)
    record={'started_at_utc':started,'finished_at_utc':datetime.now(timezone.utc).isoformat(),'argv':argv,
        'exit_code':result.returncode,'compiler_sha256':sha(compiler),'pins':pins,
        'source_sha256':sha(source/'Program.cs'),'bridge_sha256':sha(source/'bridge.ps1'),
        'manifest_sha256':sha(source/'launcher.manifest'),'generated_pins_sha256':sha(generated),
        'exe_sha256':sha(exe) if exe.exists() else None,'exe_bytes':exe.stat().st_size if exe.exists() else None,
        'services_started':0,'dependency_downloads':0,'global_changes':0}
    (out/'build.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'COMPILED' if result.returncode==0 else 'COMPILE_FAILED','exit_code':result.returncode,
        'exe_sha256':record['exe_sha256'],'exe_bytes':record['exe_bytes']}))
    return result.returncode
if __name__=='__main__':raise SystemExit(main())

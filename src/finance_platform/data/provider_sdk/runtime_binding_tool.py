"""Explicit FA010 binding producer, stdlib only; output must be reviewed/sealed."""
import argparse
import hashlib
import json
from pathlib import Path


def binding_for(python):
    python=Path(python).resolve(strict=True)
    if python.name.lower()!='python.exe' or python.parent.name!='Scripts':raise ValueError('Windows venv Python required')
    venv=python.parents[1];cfg=venv/'pyvenv.cfg'
    values=dict(line.split(' = ',1) for line in cfg.read_text().splitlines() if ' = ' in line)
    base=Path(values['home'])/'python.exe'
    if base.resolve()==python or (base.parent/'pyvenv.cfg').exists():raise ValueError('Direct base interpreter required')
    site=venv/'Lib/site-packages'
    if not site.is_dir():raise ValueError('Missing venv site-packages')
    def reference(path):
        raw=path.read_bytes()
        return {'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    return {'schema_version':'sdk-direct-base-runtime-binding-1','expected_version':[3,12,14],
            'venv_python':reference(python),'venv_config':reference(cfg),'base_python':reference(base),'base_dll':reference(base.parent/'python312.dll'),
            'site_root':str(site.resolve()),'source':'Explicit reviewed binding for this exact deployment venv; no candidate-path fallback. Actual child version/image/DLL checked before SDK.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--python',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();value=binding_for(args.python)
    with Path(args.output).open('x',encoding='utf-8') as output:json.dump(value,output,indent=2)

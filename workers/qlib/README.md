# qlib Windows dependency worker

Prepared package only; native workflow and platform integration are not accepted.
Use the project's existing Windows uv and Python3.12.14. Install only after a concrete deployment release:

    uv sync --project <PROJECT_ROOT>/workers/qlib --locked --no-install-project --python 3.12.14 --no-python-downloads

Replace <PROJECT_ROOT> with this checkout's resolved root. This creates only this worker's .venv; it does not install the enclosing FinanceAnalyze package. Registry artifacts may require approved downloads; the two workers do not share dependency trees.

After installation, invoke this worker's .venv/Scripts/python.exe -I -B -X utf8 inspect_runtime.py under its worker directory and capture stdout in the released run. The probe checks exact local prefix, manifest/lock/wheel identities, installed versions, primary wheel source files and the actual loaded Python DLL without importing the application or invoking the official CLI. It has not been run in this preparation.

The installed console entrypoint is qrun.exe (module qlib.cli.run). It is a reference launch surface, not an authorized business command. The existing controlled-worker adapter, sealed input/request, model/checkpoint/access and process admission remain the native owner’s later scope. No login, default interactive CLI or arbitrary config is started by packaging.

Candidate tests continue to use their previously approved environments and identities. Production binding must be measured from this project-local worker; no report-environment fallback or copied venv is permitted. Worker-local measured bindings remain runtime artifacts.

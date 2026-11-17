"""Bind goal adapters to the existing guarded SDK child and optional OpenBB."""
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys

from predictor.access import AccessPolicy
from finance_platform.data.contracts import DataError
from finance_platform.data.provider_sdk.runner import SDKRunner


def registered_runners(runtime, *, pins, fixture=None, fixture_module=None,
                       windows_binding=None, openbb_binding=None):
    """Only host-owned bindings may supply an isolated offline fixture."""
    source_root = Path(__file__).resolve().parents[2]
    kwargs = {"policy": AccessPolicy(runtime.root), "source_root": source_root,
              "pins": pins, "fixture": fixture, "fixture_module": fixture_module}
    runners = {}
    try:
        shared = SDKRunner(python=sys.executable, runtime_binding=windows_binding, **kwargs)
        runners["akshare_cn"] = shared
        try:
            if version("tushare") == "1.4.29":
                runners["tushare"] = shared
        except PackageNotFoundError:
            pass
    except DataError:
        pass
    binding = openbb_binding if openbb_binding is not None else runtime.openbb_runtime_binding
    if binding is not None:
        from importlib.metadata import distributions
        expected = {"openbb-nasdaq": "2.0.0", "openbb-core": "2.0.1"}
        installed = {dist.metadata["Name"]: dist.version for dist in distributions(path=[binding["site_root"]])
                     if dist.metadata["Name"] in expected}
        if installed == expected:
            try:
                runners["openbb_nasdaq"] = SDKRunner(python=binding["venv_python"]["path"],
                                                      runtime_binding=binding, **kwargs)
            except DataError:
                pass
    return runners


def installed_goal_pins():
    directory = Path(__file__).resolve().parents[1] / "data" / "provider_sdk"
    return json.loads((directory / "goal_sdk_pins.json").read_bytes())

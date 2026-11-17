"""Shared integration construction; no SDK import and no ambient fixture mode."""
import json
from pathlib import Path
import sys
from predictor.access import AccessPolicy
from .service import ProviderDataService
from .runner import SDKRunner


def runtime_components(runtime):
    policy=AccessPolicy(runtime.root)
    pins=json.loads(Path(__file__).with_name("pins.json").read_text(encoding="utf-8"))
    service=ProviderDataService(runtime.database,policy)
    # Anchored to the installed reviewed package, not the data root of an app.
    source_root=Path(__file__).resolve().parents[3]
    if not (source_root/'finance_platform/__init__.py').is_file():
        from finance_platform.data.contracts import DataError
        raise DataError('SDK_RUNTIME_SOURCE_UNAVAILABLE')
    runner=SDKRunner(policy,python=sys.executable,pins=pins,source_root=source_root)
    return service,runner

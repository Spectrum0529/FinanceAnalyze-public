"""Reviewed Windows bytes for the SDK templates made executable by account grants."""
from hashlib import sha256

from .contracts import StrategyError, canonical
from .packages import inspect_package


# These are release pins, independent of the template generator. Only the
# strategy ID varies. A change to a pinned template needs a new approval source.
APPROVAL_SOURCE = "PRODUCT_REVIEWED_SDK_TEMPLATE_V1"
_REFERENCE_ID = "reviewed-template-v1"
_COMMON_FILES = {
    "README.txt": "a1aec750a678702bc8eb4fe1f278bbba4a7d3ebd7c6735d655b7275eab7c22e9",
    "dependencies.lock.json": "cf0aca5e8af3242e1ed52f5e5e40d13c4cc5ee3289bc822ceb9aabf827db5174",
}
_REVIEWED = {
    "event": (
        "4c966ce8facfb24e67f6501280b76c1ec6574b3498b8b648c94993b495b9d5d9",
        "a23b1733e800e092e99660a8b379b22f7904b443f4cdb6cc659db8d704e4707c",
    ),
    "batch": (
        "c5bdcdfb781ae664c3af4b1e93197708d3859725c825de54bf24e2a1ff7cb57f",
        "8f4a2f1e413201b9409b7ce4ac6c64668a4b452c886498e0513c1f0f8e1e2a13",
    ),
    "target": (
        "3a4b1faddacc726dfa2f168473e57669183df2a415298f578a4a9d58428d39d8",
        "756953c21fdce6207903f9147557449e78114acdfadd58d19e8bcdd5c5d9d8f5",
    ),
}
STYLES = frozenset(_REVIEWED)


def approved_package_hash(source, strategy_id, style):
    try:
        manifest, files, package_hash = inspect_package(source)
        expected_manifest, expected_code = _REVIEWED[style]
        normalized = canonical({**manifest.raw, "id": _REFERENCE_ID})
        expected_files = _COMMON_FILES | {
            "strategy.py": expected_code,
            "manifest.json": sha256(canonical(manifest.raw)).hexdigest(),
        }
        actual = {name: sha256(body).hexdigest() for name, body in files.items()}
        if (manifest.raw["id"] == strategy_id and manifest.raw["style"] == style
                and sha256(normalized).hexdigest() == expected_manifest and actual == expected_files):
            return package_hash
    except (OSError, KeyError, StrategyError):
        pass
    raise StrategyError("STRATEGY_BUILTIN_TEMPLATE_MISMATCH")

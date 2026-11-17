"""Bounded deployment config and value-free diagnostic checks."""
import importlib.metadata
import ipaddress
import json
from pathlib import Path
import platform
import re
import sys
import tomllib
from urllib.parse import urlsplit

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from finance_platform.core.errors import PlatformError
from finance_platform.core.security import clean
from predictor.access import AccessPolicy

DEFAULTS = {"schema_version": 1, "bind_host": "127.0.0.1", "port": 8000,
            "database": ".local/platform/platform.sqlite3", "sending_workers": 1,
            "live_enabled": False, "untrusted_strategies_enabled": False,
            "queue_warning": 100, "pending_order_warning": 20,
            "worker_stale_seconds": 60, "notification": {"enabled": False, "endpoint": None}}


def validate_config(value):
    clean(value)
    if type(value) is not dict or set(value) - set(DEFAULTS):
        raise PlatformError("INVALID_INPUT")
    config = DEFAULTS | value
    if config["schema_version"] != 1 or config["live_enabled"] is not False or config["untrusted_strategies_enabled"] is not False:
        raise PlatformError("INVALID_INPUT")
    if config["bind_host"] not in {"127.0.0.1", "::1"}:
        raise PlatformError("INVALID_INPUT")
    for key, lower, upper in (("port", 1024, 65535), ("sending_workers", 1, 1),
                              ("queue_warning", 1, 1000000), ("pending_order_warning", 1, 1000000),
                              ("worker_stale_seconds", 10, 3600)):
        if type(config[key]) is not int or not lower <= config[key] <= upper:
            raise PlatformError("INVALID_INPUT")
    if type(config["database"]) is not str or not re.fullmatch(r"\.local/platform/[a-zA-Z0-9_-]+\.sqlite3", config["database"]):
        raise PlatformError("INVALID_INPUT")
    notification = config["notification"]
    if type(notification) is not dict or set(notification) != {"enabled", "endpoint"} or type(notification["enabled"]) is not bool:
        raise PlatformError("INVALID_INPUT")
    if notification["endpoint"] is not None:
        validate_receiver(notification["endpoint"])
    if notification["enabled"] and not notification["endpoint"]:
        raise PlatformError("INVALID_INPUT")
    return config


def validate_receiver(endpoint):
    """Only explicit literal IPv4 loopback fake receivers, no redirects or DNS."""
    if type(endpoint) is not str or len(endpoint) > 512:
        raise PlatformError("INVALID_INPUT")
    try:
        url = urlsplit(endpoint)
        host = ipaddress.ip_address(url.hostname)
        valid = (url.scheme == "http" and host == ipaddress.ip_address("127.0.0.1") and
                 url.port is not None and 1024 <= url.port <= 65535 and
                 not url.username and not url.password and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise PlatformError("INVALID_INPUT")
    return endpoint


def load_config(root, path=None):
    if path is None:
        local = Path(root) / ".local/platform/operations.json"
        if not local.exists():
            return validate_config({})
        path = local
    policy = AccessPolicy(Path(root))
    resolved = policy.authorize(path, operation="config_read")
    if resolved.stat().st_size > 65536:
        raise PlatformError("INVALID_INPUT")
    return validate_config(json.loads(resolved.read_bytes()))


def runtime_pins(dependencies):
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError()
    pins = {}
    for dependency in dependencies:
        requirement = Requirement(dependency)
        specifiers = list(requirement.specifier)
        if requirement.url is not None or len(specifiers) != 1:
            raise ValueError()
        pin = specifiers[0]
        if pin.operator != "==" or pin.version.endswith(".*"):
            raise ValueError()
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        if name in pins:
            raise ValueError()
        pins[name] = (pin.version, requirement.extras)
    return pins


def extra_pins(package, extras, optional_dependencies, locked):
    distribution = importlib.metadata.distribution(package)
    available = {canonicalize_name(extra) for extra in distribution.metadata.get_all("Provides-Extra", [])}
    requirements = [Requirement(value) for value in distribution.requires or ()]
    for extra in sorted(canonicalize_name(value) for value in extras):
        if extra not in available or extra not in optional_dependencies:
            raise ValueError()
        edges = {canonicalize_name(edge["name"]): edge for edge in optional_dependencies[extra]
                 if "marker" not in edge or Marker(edge["marker"]).evaluate()}
        required = {canonicalize_name(item.name): item for item in requirements
                    if item.marker is not None and item.marker.evaluate({"extra": extra})
                    and not item.marker.evaluate()}
        # Check both sources so an absent extra edge in either one cannot pass.
        for name in sorted(edges.keys() | required.keys()):
            item = required.get(name)
            pins = sorted(locked.get(name, ()))
            exact = list(item.specifier) if item is not None else []
            if len(exact) == 1 and exact[0].operator == "==" and not exact[0].version.endswith(".*"):
                pin = exact[0].version
            elif len(pins) == 1:
                pin = pins[0]
            else:
                raise ValueError()
            valid_edge = name in edges and edges[name].get("version", pin) == pin
            valid_requirement = item is not None and item.url is None and item.specifier.contains(pin, prereleases=True)
            yield name, pin, valid_edge and valid_requirement


def distribution_check(package, pin, locked, *, valid_edge=True):
    try:
        version = importlib.metadata.version(package)
        status = "OK" if version == pin else "VERSION_MISMATCH"
    except importlib.metadata.PackageNotFoundError:
        version, status = None, "MISSING"
    locked_versions = sorted(locked.get(package, ()))
    lock_matches = valid_edge and locked_versions == [pin]
    return {"check": "dependency:" + package, "ok": status == "OK" and lock_matches,
            "status": status if status != "OK" or lock_matches else "LOCK_MISMATCH",
            "observed": version, "expected": pin, "locked_versions": locked_versions,
            "lock_matches": lock_matches}


def doctor(root, config=None):
    config = validate_config(config or {})
    root = Path(root).resolve()
    checks = [{"check": "python", "ok": sys.version_info[:3] == (3, 12, 14), "observed": platform.python_version()},
              {"check": "loopback", "ok": True}, {"check": "live_disabled", "ok": True},
              {"check": "single_sending_worker", "ok": True}]
    expected, locked, optional = {}, {}, {}
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        expected = runtime_pins(project["dependencies"])
        checks.append({"check": "runtime_manifest", "ok": True, "status": "OK"})
    except (OSError, ValueError, KeyError, TypeError):
        checks.append({"check": "runtime_manifest", "ok": False, "status": "MISSING_OR_INVALID_PIN_CONTRACT"})
    try:
        lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
        for package in lock["package"]:
            if "version" in package:
                name, version = canonicalize_name(package["name"]), package["version"]
                locked.setdefault(name, set()).add(version)
                optional[name, version] = package.get("optional-dependencies", {})
        checks.append({"check": "lockfile", "ok": True, "status": "OK"})
    except (OSError, ValueError, KeyError, TypeError):
        checks.append({"check": "lockfile", "ok": False, "status": "MISSING_OR_INVALID_LOCK"})
    for package, (pin, extras) in sorted(expected.items()):
        row = distribution_check(package, pin, locked)
        checks.append(row)
        if not extras or not row["ok"]:
            continue
        try:
            for name, extra_pin, valid_edge in extra_pins(package, extras, optional[package, pin], locked):
                checks.append(distribution_check(name, extra_pin, locked, valid_edge=valid_edge))
        except (ValueError, KeyError, TypeError):
            checks.append({"check": "dependency_extras:" + package, "ok": False,
                           "status": "MISSING_OR_INVALID_EXTRA_CONTRACT"})
    # Do not report environment variable values, paths containing user identities or raw logs.
    return {"implementation": "IMPLEMENTED", "verification": "LOCAL_VERIFIED" if all(c["ok"] for c in checks) else "FAILED",
            "checks": checks, "target_environment": "LINUX_CONTAINER_NOT_VERIFIED",
            "dependency_scope": "ALL_DIRECT_RUNTIME_PINS_AND_LOCK_DISTRIBUTION_METADATA_ONLY",
            "online_connections": "NOT_PROBED", "research_data": "UNVERIFIED", "live_enabled": False}

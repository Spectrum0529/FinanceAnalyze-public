"""Install only this product's stdio entry in an initialized Windows profile."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import tomllib
from uuid import uuid4

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from finance_platform.core.errors import PlatformError
from finance_platform.core.storage_native import directory


SERVER = "financeanalyze"
CORDIS_ID = "mcp-financeanalyze"
CORDIS_PLUGIN = "@deepseek-ai/dsh-mcp-client"
LIMIT = 1024 * 1024
TARGETS = {
    "codex": (".codex", "CODEX_HOME", "config.toml"),
    "deepseek": (".dsh", "DSH_HOME", "profiles/desktop/cordis.patch.yml"),
}
MANUAL = "manual_configuration_required"
CONFLICT = "MCP_CLIENT_CONFIG_CONFLICT"
BUSY = "MCP_CLIENT_CONFIG_BUSY"
UNAVAILABLE = "MCP_CLIENT_CONFIG_UNAVAILABLE"
FAILED = "MCP_CLIENT_CONFIG_WRITE_FAILED"


class ConfigurationError(Exception):
    def __init__(self, code):
        self.code = code


def _status(status, code=None):
    return {"status": status, "reason_code": code}


def _endpoint(root, client, port):
    return {
        "command": str(root / ".venv/Scripts/python.exe"),
        "args": ["-I", "-B", str(root / "tools/platform_ops/mcp_stdio.py"),
                 "--port", str(port), "--client", client],
    }


def _same_endpoint(command, args, endpoint):
    return (isinstance(command, str) and Path(command) == Path(endpoint["command"])
            and isinstance(args, list) and len(args) == len(endpoint["args"])
            and args[:2] == endpoint["args"][:2] and isinstance(args[2], str)
            and Path(args[2]) == Path(endpoint["args"][2]) and args[3:] == endpoint["args"][3:])


def _line(raw):
    return b"\r\n" if b"\r\n" in raw else b"\n"


def _append(raw, fragment):
    newline = _line(raw)
    return raw + (2 * newline if raw else b"") + fragment.replace(b"\n", newline)


def _codex(raw, endpoint):
    original = tomllib.loads(raw.decode("utf-8-sig"))
    entries = original.get("mcp_servers", {})
    if type(entries) is not dict:
        raise ConfigurationError(UNAVAILABLE)
    if any(name != SERVER and name.casefold() == SERVER for name in entries):
        raise ConfigurationError(CONFLICT)
    if SERVER in entries:
        item = entries[SERVER]
        if (type(item) is dict and item.get("enabled", True) is True and not item.get("url")
                and not item.get("env") and not item.get("env_vars")
                and _same_endpoint(item.get("command"), item.get("args"), endpoint)):
            return None
        raise ConfigurationError(CONFLICT)
    fragment = ("[mcp_servers.financeanalyze]\n"
                "command = " + json.dumps(endpoint["command"], ensure_ascii=False) + "\n"
                "args = " + json.dumps(endpoint["args"], ensure_ascii=False) + "\n").encode()
    proposed = _append(raw, fragment)
    installed = tomllib.loads(proposed.decode("utf-8-sig"))
    entry = installed["mcp_servers"].pop(SERVER)
    if "mcp_servers" not in original:
        del installed["mcp_servers"]
    if entry != endpoint or installed != original:
        raise ConfigurationError(UNAVAILABLE)
    return proposed


def _mapping(node):
    if not isinstance(node, MappingNode):
        return {}
    result = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.value in result:
            raise ConfigurationError(UNAVAILABLE)
        result[key.value] = value
    return result


def _scalar(node):
    return node.value if isinstance(node, ScalarNode) else None


def _sequence(raw):
    node = yaml.compose(raw.decode("utf-8-sig"), Loader=yaml.SafeLoader)
    if node is not None and not isinstance(node, SequenceNode):
        raise ConfigurationError(UNAVAILABLE)
    return node


def _cordis_entry(node, endpoint):
    item = _mapping(node)
    settings = _mapping(item.get("config"))
    if _scalar(item.get("id")) != CORDIS_ID and _scalar(settings.get("serverName")) != SERVER:
        return False
    arguments = settings.get("args")
    if (str(_scalar(item.get("disabled"))).lower() == "true"
            or str(_scalar(item.get("enabled"))).lower() == "false"
            or str(_scalar(settings.get("enabled"))).lower() == "false"
            or _scalar(item.get("id")) != CORDIS_ID or _scalar(item.get("name")) != CORDIS_PLUGIN
            or _scalar(settings.get("serverName")) != SERVER or _scalar(settings.get("transport")) != "stdio"
            or not isinstance(arguments, SequenceNode)
            or not _same_endpoint(_scalar(settings.get("command")), [_scalar(x) for x in arguments.value], endpoint)):
        raise ConfigurationError(CONFLICT)
    return True


def _deepseek(raw, endpoint):
    original = _sequence(raw)
    entries = original.value if original is not None else []
    matching = sum(_cordis_entry(item, endpoint) for item in entries)
    if matching > 1:
        raise ConfigurationError(CONFLICT)
    if matching == 1:
        return None
    item = {"id": CORDIS_ID, "name": CORDIS_PLUGIN,
            "config": {"serverName": SERVER, "transport": "stdio", **endpoint}}
    fragment = yaml.safe_dump([item], allow_unicode=True, sort_keys=False).encode()
    if original is not None and original.flow_style and not entries:
        # Keep whitespace and inline comments. Replace only the [] token.
        text = raw.decode("utf-8-sig")
        newline = _line(raw)
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        first, last = original.start_mark.index, original.end_mark.index
        proposed = (bom + text[:first].encode() + newline + fragment.replace(b"\n", newline)
                    + text[last:].encode())
    else:
        proposed = _append(raw, fragment)
    installed = _sequence(proposed)
    if installed is None or len(installed.value) != len(entries) + 1 or not _cordis_entry(installed.value.pop(), endpoint):
        raise ConfigurationError(UNAVAILABLE)
    if entries and yaml.serialize(installed) != yaml.serialize(original):
        raise ConfigurationError(UNAVAILABLE)
    return proposed


@contextmanager
def _lock(path):
    import win32api
    import win32event

    token = sha256(str(path).casefold().encode()).hexdigest()
    mutex = win32event.CreateMutex(None, False, "Local\\FinanceAnalyzeMcpConfig-" + token)
    acquired = False
    try:
        value = win32event.WaitForSingleObject(mutex, 3000)
        if value not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise ConfigurationError(BUSY)
        acquired = True
        yield
    finally:
        if acquired:
            win32event.ReleaseMutex(mutex)
        win32api.CloseHandle(mutex)


def _read(parent, name):
    try:
        with parent.open_file(name) as opened:
            return opened.read(LIMIT)
    except PlatformError as error:
        if error.code == "STORAGE_OBJECT_MISSING":
            return None
        raise


def _replace_file(target, temporary, backup):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
    replace = kernel.ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.c_void_p, ctypes.c_void_p]
    replace.restype = wintypes.BOOL
    if not replace(str(target), str(temporary), str(backup), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _read_acl(opened):
    import win32security

    flags = (win32security.OWNER_SECURITY_INFORMATION | win32security.GROUP_SECURITY_INFORMATION
             | win32security.DACL_SECURITY_INFORMATION)
    # The held handle pins the regular file. GetFileSecurity retains its actual
    # DACL flags, which the handle-based descriptor may normalize as inherited.
    value = win32security.GetFileSecurity(str(opened.canonical()), flags)
    text = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(value, 1, flags)
    return value, text


def _retain_acl(parent, target, expected, prior):
    import win32security

    with parent.open_file(target.name) as opened:
        if opened.read(LIMIT) != expected:
            raise ConfigurationError(BUSY)
        if _read_acl(opened)[1] != prior[1]:
            win32security.SetFileSecurity(str(target), win32security.DACL_SECURITY_INFORMATION, prior[0])
        if _read_acl(opened)[1] != prior[1]:
            raise ConfigurationError(FAILED)


def _reconcile(parent, target, backup, displaced, proposed, original, prior):
    body = _read(parent, backup.name)
    if body is None:
        return
    if body != original:
        with parent.open_file(backup.name) as opened:
            prior = _read_acl(opened)
    current = _read(parent, target.name)
    if current is None:
        backup.rename(target)
        _retain_acl(parent, target, body, prior)
    elif current == proposed:
        _replace_file(target, backup, displaced)
        _retain_acl(parent, target, body, prior)
        # The client can save again between the check and replacement. Preserve
        # any bytes displaced by rollback other than our staged configuration.
        if _read(parent, displaced.name) == proposed:
            displaced.unlink()
    # A newer editor took over. Keep its file and the original backup.


def _publish(parent, target, original, proposed):
    from pywintypes import error as WindowsError

    suffix = ".financeanalyze-" + uuid4().hex
    stage = target.with_name(target.name + suffix + ".tmp")
    backup = target.with_name(target.name + suffix + ".bak")
    displaced = target.with_name(target.name + suffix + ".recovery.bak")
    try:
        with parent.create_file(stage.name) as output:
            output.write_flush(proposed)
            if output.read(LIMIT) != proposed:
                raise ConfigurationError(FAILED)
        if _read(parent, target.name) != original:
            raise ConfigurationError(BUSY)
        if original is None:
            # Windows rename to an existing destination fails without replacement.
            stage.rename(target)
            return
        with parent.open_file(target.name) as existing:
            if existing.read(LIMIT) != original:
                raise ConfigurationError(BUSY)
            prior = _read_acl(existing)
        try:
            _replace_file(target, stage, backup)
            if _read(parent, backup.name) != original or _read(parent, target.name) != proposed:
                raise ConfigurationError(BUSY)
            _retain_acl(parent, target, proposed, prior)
        except (OSError, WindowsError, PlatformError, ConfigurationError):
            _reconcile(parent, target, backup, displaced, proposed, original, prior)
            raise
        backup.unlink(missing_ok=True)
    finally:
        stage.unlink(missing_ok=True)


def ensure_client_config(root, client_id, port):
    """Called after native listener, browser account and recent proof checks."""
    if os.name != "nt" or client_id not in TARGETS or type(port) is not int or not 1024 <= port <= 65535:
        return _status(MANUAL, UNAVAILABLE)
    base, override_name, relative = TARGETS[client_id]
    default_home = Path.home() / base
    override = os.environ.get(override_name)
    if override and Path(override) != default_home:
        return _status(MANUAL, UNAVAILABLE)
    target = default_home / relative
    endpoint = _endpoint(Path(root).resolve(), client_id, port)
    from pywintypes import error as WindowsError

    try:
        with _lock(target), directory(target.parent, update_children=True) as parent:
            if parent.canonical() != target.parent:
                raise ConfigurationError(UNAVAILABLE)
            original = _read(parent, target.name)
            proposed = (_codex if client_id == "codex" else _deepseek)(original or b"", endpoint)
            if proposed is None:
                return _status("already_configured")
            if len(proposed) > LIMIT:
                raise ConfigurationError(UNAVAILABLE)
            _publish(parent, target, original, proposed)
            return _status("configured")
    except ConfigurationError as error:
        return _status(MANUAL, error.code)
    except (UnicodeError, ValueError, yaml.YAMLError, PlatformError, RecursionError):
        return _status(MANUAL, UNAVAILABLE)
    except OSError as error:
        return _status(MANUAL, BUSY if getattr(error, "winerror", None) in {32, 33, 80, 183} else FAILED)
    except WindowsError as error:
        return _status(MANUAL, BUSY if error.winerror in {32, 33, 80, 183} else FAILED)

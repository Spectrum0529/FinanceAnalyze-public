"""Direct TA base Python -I -S bootstrap. No package import before admission."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def reject():
    raise RuntimeError("TA_BOOTSTRAP_REJECTED")


def load(pin, name):
    path = Path(pin["path"]).absolute()
    for item in (path, *path.parents):
        stat = item.lstat()
        if item.is_symlink() or getattr(stat, "st_file_attributes", 0)&0x400 or (item.is_file() and stat.st_nlink != 1):
            reject()
    raw = path.read_bytes()
    if len(raw) != pin["bytes"] or hashlib.sha256(raw).hexdigest() != pin["sha256"]:
        reject()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    if sys.version_info[:3] != (3, 12, 14) or not sys.flags.isolated or not sys.flags.no_site:
        reject()
    if sys.stdin.buffer.readline(4) != b"GO\n":
        reject()
    raw = sys.stdin.buffer.readline(524289)
    if len(raw) > 524288 or not raw.endswith(b"\n"):
        reject()
    packet = json.loads(raw)
    if packet.get("schema") != "ta-product-admission-1" or packet["admission"]["case"] != "SERVER_PROFILE":
        reject()
    protocol = load(packet["pins"]["protocol"], "fa013_ta_private_protocol")
    # Reject duplicates/nonfinite values even in this initial private packet.
    packet = protocol.checked(raw)
    identity_helper = load(packet["pins"]["identity"], "fa013_ta_private_identity")
    for name,pin in packet["pins"].items():
        if name != "probe_stderr":
            identity_helper.verify_pin(pin)
            continue
        # Only this completion artifact may be empty. Keep alias/nlink checks.
        if (type(pin)is not dict or set(pin)!={"path","bytes","sha256"}
                or type(pin["bytes"])is not int or pin["bytes"]!=0
                or pin["sha256"]!=hashlib.sha256(b"").hexdigest()
                or not Path(pin["path"]).is_absolute()):reject()
        path=Path(pin["path"]).absolute()
        for item in (path,*path.parents):
            info=item.lstat()
            if item.is_symlink() or getattr(info,"st_file_attributes",0)&0x400 or (item.is_file() and info.st_nlink!=1):reject()
        if not path.is_file() or path.read_bytes()!=b"":reject()
    if Path(__file__).resolve() != Path(packet["pins"]["bootstrap"]["path"]).resolve():
        reject()
    attempt = Path(packet["attempt_root"]).absolute()
    if attempt != Path.cwd().resolve() or packet["marker"] != protocol.checked((attempt/"owner-marker.json").read_bytes()):
        reject()
    if packet["marker"]["admission_hash"] != protocol.digest(packet["admission"]):
        reject()
    cache=_owned_bytecode_prefix(attempt,attempt.parent.parent,packet["admission"]["binding"]["owner_id"],packet["admission"]["binding"]["run_id"],create=False)
    if sys.pycache_prefix!=str(cache) or sys.dont_write_bytecode is not True or sys._xoptions.get("pycache_prefix")!=str(cache):reject()
    identity = identity_helper.own_identity(packet["job_handle"])
    if (identity["actual_pid"] != packet["expected_pid"]
            or Path(identity["actual_image"]) != Path(packet["pins"]["base_python"]["path"])
            or Path(identity["actual_python_dll"]) != Path(packet["pins"]["base_dll"]["path"])):
        reject()
    if any(name.split(".", 1)[0] in {"tradingagents", "langchain_core", "langgraph", "numpy", "pandas"} for name in sys.modules):
        reject()
    site, prefix = Path(packet["site_root"]).resolve(), Path(packet["venv_prefix"]).resolve()
    if site != prefix/"Lib/site-packages" or not site.is_dir():
        reject()
    # No .pth processing, site.addsitedir or venv redirector. sys.executable and
    # kernel image remain the actual base Python; only exact TA site is added.
    sys.path.insert(0, str(site))
    sys.prefix = sys.exec_prefix = str(prefix)
    channel = protocol.Channel(sys.stdin.buffer, sys.stdout.buffer, bytes.fromhex(packet.pop("session_key")), side="child")
    channel.exchange("READY", {**identity, "actual_python_dll_sha256":packet["pins"]["base_dll"]["sha256"],
        "source_identity":packet["source_identity"], "automatic_site_startup":False, "native_imported":False,
        "sys_executable":sys.executable,"site_root":str(site),"prefix":sys.prefix,
        "base_prefix":sys.base_prefix,"version":list(sys.version_info[:3]),"owned_bytecode_prefix_verified":True,"owned_bytecode_prefix_initially_empty":True,"dont_write_bytecode":sys.dont_write_bytecode})
    guard = load(packet["pins"]["guard"], "fa013_ta_private_guard")
    child = load(packet["pins"]["child"], "fa013_ta_private_child")
    helpers = SimpleNamespace(verify_pin=identity_helper.verify_pin,verify_source=identity_helper.verify_source,digest=protocol.digest,load=load)
    # Third-party diagnostics have a separate bounded stderr pipe. Only the
    # private Channel writes framed stdout; no result/ACK is parsed from prints.
    sys.stdout = sys.stderr
    child.run(packet, channel, identity, helpers, guard)


def _owned_bytecode_prefix(attempt,attempt_base,owner_id,run_id,*,create):
    import stat
    root=Path(attempt);base=Path(attempt_base)
    if (type(create)is not bool or not root.is_absolute() or not base.is_absolute() or ".."in root.parts or ".."in base.parts
            or type(owner_id)is not str or not owner_id or type(run_id)is not str or not run_id or len(run_id)>128 or not run_id[0].isalnum()
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in run_id)
            or root!=base/hashlib.sha256(owner_id.encode()).hexdigest()/run_id):
        raise RuntimeError("TA_BYTECODE_PREFIX_OWNER_RUN_INVALID")
    parent=root/"tmp"
    for item in reversed((parent,*parent.parents)):
        info=item.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
            raise RuntimeError("TA_BYTECODE_PREFIX_ALIAS_OR_NON_DIRECTORY")
    prefix=parent/"python-bytecode"
    if create:prefix.mkdir(exist_ok=False)
    info=prefix.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400 or any(prefix.iterdir()):
        raise RuntimeError("TA_BYTECODE_PREFIX_NOT_FRESH_EMPTY_DIRECTORY")
    return prefix

if __name__ == "__main__":
    try:
        main()
    except BaseException:
        raise SystemExit(71)

"""Review candidate: Windows portable program removal, never recursive Root deletion.

Run with an external Python. No product imports, global environment changes, or
process termination. Stop the installation with its frozen stop.ps1 beforehand.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from datetime import datetime, timezone

CONTROL = Path(__file__).resolve().parent / 'receipts'
MARKER = '.financeanalyze-install-id'
SCHEMA = 'FA018PortableInstallationV1'
REPARSE = 0x400
DIRECTORY = 0x10
ALLOWED_PREFIXES = ('src/', 'tools/platform_ops/', 'web/dist/')
ALLOWED_TOP = {'pyproject.toml', 'uv.lock', '.python-version'}
ESSENTIAL = {'pyproject.toml', 'uv.lock', 'src/finance_platform/__init__.py',
             'tools/platform_ops/start.ps1', 'tools/platform_ops/stop.ps1'}


class Refuse(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Refuse(reason)


def lexical(value):
    require(bool(re.fullmatch(r'[A-Za-z]:\\[^\x00-\x1f]*', value)), 'LOCAL_ABSOLUTE_PATH_REQUIRED')
    require('/' not in value, 'NON_CANONICAL_SEPARATOR')
    p = PureWindowsPath(value)
    require(len(p.parts) > 1, 'DRIVE_ROOT_FORBIDDEN')
    for part in value[3:].split('\\'):
        require(part and part not in {'.', '..'} and part == part.rstrip(' .'), 'PATH_ALIAS_FORBIDDEN')
        require(not re.search(r'[:<>"|?*]', part), 'PATH_ALIAS_FORBIDDEN')
        require(not re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I), 'DEVICE_PATH_FORBIDDEN')
    return Path(value)


def relative(value):
    require(isinstance(value, str) and '\\' not in value and ':' not in value, 'BAD_INVENTORY_PATH')
    p = PurePosixPath(value)
    require(not p.is_absolute() and str(p) == value and all(x not in {'.', '..'} for x in p.parts), 'BAD_INVENTORY_PATH')
    require(value in ALLOWED_TOP or value.startswith(ALLOWED_PREFIXES), 'NON_PROGRAM_PATH_FORBIDDEN')
    lexical('Z:\\' + value.replace('/', '\\'))
    return p


def overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def no_git_ancestor(path):
    for node in (path, *path.parents):
        require(not os.path.lexists(node / '.git'), 'GIT_WORKTREE_FORBIDDEN')


class FileInfo(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('creation', W.FILETIME), ('access', W.FILETIME),
                ('write', W.FILETIME), ('volume', W.DWORD), ('size_hi', W.DWORD),
                ('size_lo', W.DWORD), ('links', W.DWORD), ('index_hi', W.DWORD), ('index_lo', W.DWORD)]


class Disposition(C.Structure):
    _fields_ = [('delete', W.BOOLEAN)]


class Win:
    def __init__(self):
        require(sys.platform == 'win32', 'WINDOWS_ONLY')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
        self.k.CreateFileW.restype = W.HANDLE
        self.k.CloseHandle.argtypes = [W.HANDLE]
        self.k.CloseHandle.restype = W.BOOL
        self.k.GetFileInformationByHandle.argtypes = [W.HANDLE, C.POINTER(FileInfo)]
        self.k.GetFileInformationByHandle.restype = W.BOOL
        self.k.GetFinalPathNameByHandleW.argtypes = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD]
        self.k.GetFinalPathNameByHandleW.restype = W.DWORD
        self.k.ReadFile.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
        self.k.ReadFile.restype = W.BOOL
        self.k.SetFileInformationByHandle.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
        self.k.SetFileInformationByHandle.restype = W.BOOL
        self.k.GetDriveTypeW.argtypes = [W.LPCWSTR]
        self.k.GetDriveTypeW.restype = W.UINT

    def check(self, ok):
        if not ok:
            raise C.WinError(C.get_last_error())

    def open(self, path, directory=False, deletion=False, writable=False):
        # Directory handles forbid rename/delete, but permit normal file activity.
        # Payload handles forbid writes/replacement through the hash+delete interval.
        access = 0x80 if directory else 0x80000000 | (0x10000 if deletion else 0) | (0x40000000 if writable else 0)
        share = 3 if directory or writable else 1
        h = self.k.CreateFileW(str(path), access, share, None, 3, 0x02000000 | 0x00200000, None)
        if h == W.HANDLE(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            meta = FileInfo()
            self.check(self.k.GetFileInformationByHandle(h, C.byref(meta)))
            require(not meta.attributes & REPARSE, 'REPARSE_POINT_FORBIDDEN')
            require(bool(meta.attributes & DIRECTORY) == directory, 'FILE_KIND_CHANGED')
            if not directory:
                require(meta.links == 1, 'HARDLINK_FORBIDDEN')
            buf = C.create_unicode_buffer(32768)
            n = self.k.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
            self.check(n and n < len(buf))
            final = buf.value
            if final.startswith('\\\\?\\'):
                final = final[4:]
            require(final.casefold() == str(path).casefold(), 'FINAL_PATH_ALIAS_FORBIDDEN')
            return h, meta
        except BaseException:
            self.k.CloseHandle(h)
            raise

    def digest(self, h):
        value = hashlib.sha256()
        buf = C.create_string_buffer(1024 * 1024)
        count = W.DWORD()
        size = 0
        while True:
            self.check(self.k.ReadFile(h, buf, len(buf), C.byref(count), None))
            if not count.value:
                return value.hexdigest(), size
            value.update(buf.raw[:count.value])
            size += count.value

    def delete_on_close(self, h):
        flag = Disposition(1)
        self.check(self.k.SetFileInformationByHandle(h, 4, C.byref(flag), C.sizeof(flag)))


def identity(meta):
    return [meta.volume, meta.index_hi, meta.index_lo]


class Installation:
    def __init__(self, win, root, protected):
        self.win, self.root, self.handles = win, lexical(root), []
        self.lock_fd = None
        for value in protected:
            p = lexical(value)
            require(not overlap(self.root, p), 'PROTECTED_ROOT_OVERLAP')
        require(not overlap(self.root, CONTROL.parent), 'CONTROL_ROOT_OVERLAP')

    def pin(self, path):
        require(path == self.root or self.root in path.parents, 'TARGET_OUTSIDE_INSTALLATION')
        for node in reversed((path, *path.parents)):
            if node in self.handles_by_path:
                continue
            h, meta = self.win.open(node, directory=True)
            self.handles.append(h)
            self.handles_by_path[node] = meta

    def __enter__(self):
        self.handles_by_path = {}
        try:
            require(self.win.k.GetDriveTypeW(self.root.anchor) == 3, 'FIXED_LOCAL_DRIVE_REQUIRED')
            no_git_ancestor(self.root)
            self.pin(self.root)
            no_git_ancestor(self.root)
            self.root_id = identity(self.handles_by_path[self.root])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        try:
            if self.lock_fd is not None:
                import msvcrt
                os.lseek(self.lock_fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self.lock_fd, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(self.lock_fd)
                    self.lock_fd = None
        finally:
            for h in reversed(self.handles):
                self.win.k.CloseHandle(h)

    def payload(self, name, deletion=False):
        p = relative(name)
        target = self.root.joinpath(*p.parts)
        require(target != self.root and self.root in target.parents, 'TARGET_OUTSIDE_INSTALLATION')
        self.pin(target.parent)
        return self.win.open(target, deletion=deletion)

    def marker(self):
        # Marker is installation ownership metadata, never a deletion target.
        h, meta = self.win.open(self.root / MARKER)
        try:
            digest, size = self.win.digest(h)
            require(size == 37, 'BAD_INSTALLATION_MARKER')
            return digest
        finally:
            self.win.k.CloseHandle(h)

    def offline(self):
        # Start/stop architecture publishes this state. Never terminate any PID here.
        # Caller must also stop foreground/manual processes and prevent concurrent start.
        for path in (self.root / '.local', self.root / '.local/platform', self.root / '.local/platform/processes'):
            if os.path.lexists(path):
                require(not path.lstat().st_file_attributes & REPARSE, 'RUNTIME_METADATA_ALIAS_FORBIDDEN')
        native = self.root / '.local/platform/processes/native.json'
        require(not os.path.lexists(native), 'STOP_MANAGED_INSTALLATION_FIRST')

    def hold_worker_lock(self):
        # Existing kernel byte lock only; never create/truncate/change runtime files.
        path = self.root / '.local/platform/sending-worker.lock'
        if not os.path.lexists(path):
            return
        self.pin(path.parent)
        h, meta = self.win.open(path, writable=True)
        fd = None
        try:
            require(meta.size_hi or meta.size_lo, 'EMPTY_WORKER_LOCK_REFUSED')
            import msvcrt
            fd = msvcrt.open_osfhandle(h, os.O_RDWR | os.O_BINARY)
            h = None
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            self.lock_fd = fd
            fd = None
        finally:
            if fd is not None:
                os.close(fd)
            if h is not None:
                self.win.k.CloseHandle(h)


def control_path(name):
    require(bool(re.fullmatch(r'[a-z0-9_-]+\.json', name)), 'BAD_CONTROL_FILENAME')
    CONTROL.mkdir(exist_ok=True)
    for path in (CONTROL, CONTROL.parent):
        require(not path.is_symlink() and not path.is_junction(), 'CONTROL_ALIAS_FORBIDDEN')
    return CONTROL / name


def write_control(name, value):
    with control_path(name).open('x', encoding='utf-8', newline='\n') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['register', 'remove'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--protected-root', action='append', required=True)
    parser.add_argument('--receipt-name', required=True)
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--result-name')
    args = parser.parse_args(argv)
    win = Win()
    with Installation(win, args.root, args.protected_root) as install:
        marker = install.marker()
        if args.action == 'register':
            require(args.inventory is not None, 'EXPLICIT_INVENTORY_REQUIRED')
            require(args.inventory.resolve().parent == CONTROL.parent and not args.inventory.is_symlink() and not args.inventory.is_junction(), 'INVENTORY_MUST_BE_OWNED_REVIEW_FILE')
            inventory = json.loads(args.inventory.read_text(encoding='utf-8'))
            require(isinstance(inventory, list) and all(isinstance(x, str) for x in inventory), 'BAD_INVENTORY')
            require(len(inventory) == len(set(x.casefold() for x in inventory)), 'DUPLICATE_INVENTORY_PATH')
            require(ESSENTIAL <= set(inventory), 'INCOMPLETE_PROGRAM_INVENTORY')
            install.offline()
            entries = []
            for name in sorted(inventory):
                h, meta = install.payload(name)
                try:
                    digest, size = win.digest(h)
                    entries.append({'path': name, 'sha256': digest, 'bytes': size})
                finally:
                    win.k.CloseHandle(h)
            value = {'schema': SCHEMA, 'root': str(install.root), 'root_identity': install.root_id,
                     'marker_sha256': marker, 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                     'scope': 'Explicit newly-owned installation payload only; no directories or data/environment entries.',
                     'entries': entries}
            write_control(args.receipt_name, value)
            print(json.dumps({'status': 'REGISTERED', 'receipt': str(control_path(args.receipt_name)), 'files': len(entries)}))
            return 0
        require(args.result_name and args.result_name != args.receipt_name, 'DISTINCT_RESULT_REQUIRED')
        require(not control_path(args.result_name).exists(), 'RESULT_ALREADY_EXISTS')
        require(bool(re.fullmatch(r'[0-9a-f]{64}', args.manifest_sha256 or '')), 'MANIFEST_HASH_REQUIRED')
        raw = control_path(args.receipt_name).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == args.manifest_sha256, 'MANIFEST_CHANGED')
        receipt = json.loads(raw)
        require(receipt['schema'] == SCHEMA and receipt['root'] == str(install.root), 'INSTALLATION_ROOT_MISMATCH')
        require(receipt['root_identity'] == install.root_id and receipt['marker_sha256'] == marker, 'INSTALLATION_REPLACED')
        entries = receipt['entries']
        require(isinstance(entries, list) and entries, 'BAD_MANIFEST')
        names = [row['path'] for row in entries]
        require(len(names) == len(set(x.casefold() for x in names)), 'DUPLICATE_MANIFEST_PATH')
        for row in entries:
            relative(row['path'])
            require(bool(re.fullmatch(r'[0-9a-f]{64}', row['sha256'])) and type(row['bytes']) is int and row['bytes'] >= 0, 'BAD_MANIFEST_ENTRY')
        install.offline()
        install.hold_worker_lock()
        result = {'schema': 'FA018UninstallResultV1', 'root': str(install.root), 'root_identity': install.root_id,
                  'manifest_sha256': args.manifest_sha256, 'scope': 'Program files only; no directory pruning.',
                  'started_at_utc': datetime.now(timezone.utc).isoformat(), 'files': [], 'status': 'RUNNING'}
        code, mutation_started = 0, False
        prepared = []
        try:
            # Validate and lock the complete target set before the first deletion.
            # Each actual removal uses that same locked handle, never a reopened path.
            for row in entries:
                name = row['path']
                item = {'handle': None, 'outcome': {'path': name}}
                prepared.append(item)
                install.offline()
                no_git_ancestor(install.root)
                try:
                    item['handle'], meta = install.payload(name, deletion=True)
                except FileNotFoundError:
                    item['outcome']['outcome'] = 'ALREADY_ABSENT'
                    continue
                digest, size = win.digest(item['handle'])
                item['outcome']['outcome'] = 'PRESERVED_MODIFIED' if digest != row['sha256'] or size != row['bytes'] else 'READY_UNCHANGED'
            for item in prepared:
                if item['outcome']['outcome'] != 'READY_UNCHANGED':
                    continue
                install.offline()
                no_git_ancestor(install.root)
                win.delete_on_close(item['handle'])
                mutation_started = True
                win.k.CloseHandle(item['handle'])
                item['handle'] = None
                require(not os.path.lexists(install.root / Path(item['outcome']['path'])), 'DELETE_NOT_OBSERVED')
                item['outcome']['outcome'] = 'REMOVED'
        except (OSError, Refuse) as error:
            code = 2
            result['error'] = {'reason': type(error).__name__, 'detail': str(error)}
        finally:
            for item in prepared:
                if item['handle'] is not None:
                    win.k.CloseHandle(item['handle'])
            result['files'] = [item['outcome'] for item in prepared]
        result.update(status=('PARTIAL_REFUSED' if mutation_started else 'REFUSED_BEFORE_REMOVAL') if code else 'PROGRAM_REMOVAL_COMPLETED_WITH_RETAINED_FILES',
                      finished_at_utc=datetime.now(timezone.utc).isoformat())
        write_control(args.result_name, result)
        print(json.dumps({'status': result['status'], 'result': str(control_path(args.result_name))}))
        return code


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Refuse, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'REFUSED', 'reason': type(error).__name__, 'detail': str(error)}), file=sys.stderr)
        raise SystemExit(2)

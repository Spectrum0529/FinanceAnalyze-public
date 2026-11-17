"""Trusted Linux QA state for one deployment window, never a request path."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import time

from .protocol import encoded, require
from .qa_scope_contract import ColdScopeIdentity, linux_qa_base, SCOPE_FILE, ScopeRejected, parse_scope, socket_path, valid_run, valid_sha
from .protocol import Rejected


from .paths import executor_root
QA_BASE = Path(linux_qa_base(str(executor_root())))
JOURNAL = "controller-journal.sqlite3"
SIDECARS = ("-wal", "-shm", "-journal")


def _directory(path, *, private=False):
    try:
        info = path.lstat()
    except OSError as error:
        raise Rejected("QA_DIRECTORY_INVALID") from error
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), "QA_DIRECTORY_INVALID")
    if private:
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "QA_DIRECTORY_INVALID")
    return info.st_dev, info.st_ino


def _file_identity(info):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) & 0o077 == 0, "QA_FILE_IDENTITY_INVALID")
    return info.st_dev, info.st_ino


@contextmanager
def _file(path, flags=os.O_RDONLY):
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as error:
        raise Rejected("QA_FILE_UNAVAILABLE") from error
    try:
        _file_identity(os.fstat(fd))
        yield fd
    finally:
        os.close(fd)


def _read(path, limit):
    with _file(path) as fd:
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(limit + 1)
    require(0 < len(raw) <= limit, "QA_FILE_SIZE_INVALID")
    return raw


class QAState:
    def __init__(self, run_root, identity, root_identity):
        self.run_root = run_root
        self.identity = identity
        self.root_identity = root_identity
        self.staging = run_root / "staging"
        self.journal = self.staging / JOURNAL
        self.socket = Path(socket_path(identity.run_id))
        self.daemon_url = "unix://" + socket_path(identity.run_id)
        self.docker_data = self.run_root / "docker-data"
        self._claimed = False
        self.cold = None
        if type(identity) is ColdScopeIdentity:
            from .cold_daemon import ColdDaemon
            self.cold = ColdDaemon(self)

    def _check_directories(self):
        require(_directory(self.run_root, private=True) == self.root_identity
                and self.run_root.resolve() == self.run_root, "QA_SCOPE_DIRECTORY_CHANGED")
        require(_directory(self.staging, private=True) ==
                (self.identity.staging_device, self.identity.staging_inode), "QA_STAGING_IDENTITY_MISMATCH")

    def config_and_manifest(self):
        config = _read(self.run_root / "executor-config.json", 65536)
        require(hashlib.sha256(config).hexdigest() == self.identity.config_sha256, "QA_CONFIG_DIGEST_MISMATCH")
        return config, _read(self.run_root / "runtime-manifest.json", 1048576)

    def verify_daemon_socket(self, timeout=1):
        require(self._claimed, "QA_STATE_LOCK_REQUIRED")
        if self.cold is not None:
            self.cold.verify(timeout)
            return
        _directory(self.socket.parent, private=True)
        require(self.socket.parent.resolve() == self.socket.parent, "QA_DAEMON_SOCKET_MISMATCH")
        try:
            info = self.socket.lstat()
        except OSError as error:
            raise Rejected("QA_DAEMON_SOCKET_MISMATCH") from error
        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid() == 1000
                and (info.st_dev, info.st_ino) == (self.identity.socket_device, self.identity.socket_inode),
                "QA_DAEMON_SOCKET_MISMATCH")
        _directory(self.docker_data, private=True)

    def _journal_identity(self, file_identity):
        return encoded({"schema": "fa-native-qa-journal-1", "run_id": self.identity.run_id,
                        "scope_sha256": self.identity.sha256, "staging": [self.identity.staging_device,
                                                                        self.identity.staging_inode],
                        "journal": list(file_identity)})

    def _prepare_journal(self):
        marker = self.run_root / "journal-identity.json"
        try:
            marker.lstat()
        except FileNotFoundError:
            require(next(self.staging.iterdir(), None) is None, "QA_STAGING_NOT_EMPTY")
            with _file(self.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
                record = self._journal_identity(_file_identity(os.fstat(fd)))
                os.fsync(fd)
            with _file(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
                with os.fdopen(os.dup(fd), "wb") as stream:
                    stream.write(record)
                    stream.flush()
                os.fsync(fd)
        self.verify_journal()

    def verify_journal(self):
        require(self._claimed, "QA_STATE_LOCK_REQUIRED")
        self._check_directories()
        expected = _read(self.run_root / "journal-identity.json", 8192)
        with _file(self.journal) as fd:
            actual = self._journal_identity(_file_identity(os.fstat(fd)))
        require(actual == expected, "QA_JOURNAL_BINDING_MISMATCH")
        for suffix in SIDECARS:
            path = self.staging / (JOURNAL + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            _file_identity(info)

    @contextmanager
    def claim(self):
        import fcntl

        require(not self._claimed and time.time() < self.identity.expires_at, "QA_SCOPE_EXPIRED")
        self._check_directories()
        with _file(self.run_root / "scope.lock", os.O_RDWR) as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Rejected("QA_SCOPE_BUSY") from error
            self._claimed = True
            try:
                self._prepare_journal()
                yield self
            finally:
                self._claimed = False
                if self.cold is not None:
                    self.cold.release()


def load_qa_state(args):
    require(len(args) == 2 and valid_run(args[0]) and valid_sha(args[1]), "QA_SCOPE_ARGUMENT_INVALID")
    run_id, pin = args
    for ancestor in reversed(QA_BASE.parents):
        _directory(ancestor)
    _directory(QA_BASE, private=True)
    root = QA_BASE / run_id
    root_identity = _directory(root, private=True)
    require(root.resolve() == root, "QA_DIRECTORY_INVALID")
    try:
        identity = parse_scope(_read(root / SCOPE_FILE, 8192), run_id, pin, now=time.time())
    except ScopeRejected as error:
        raise Rejected(str(error)) from error
    state = QAState(root, identity, root_identity)
    state._check_directories()
    return state

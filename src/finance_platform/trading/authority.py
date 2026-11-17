"""Single-host process mutex plus DB lease. Fencing cannot protect external HTTP."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import threading

_host_guard = threading.Lock()
_host_held = False

from finance_platform.core.contracts import identifier, parse_instant
from .accounting import TradingError
from .service import stamp


class SenderAuthority:
    def __init__(self, service, principal, account_id, worker_id, lock_directory: Path | None = None, lease_seconds=30):
        identifier(worker_id)
        if not 1 <= lease_seconds <= 300:
            raise TradingError("INVALID_LEASE")
        self.service, self.principal = service, principal
        self.account_id, self.worker_id = account_id, worker_id
        fixed = service.database.path.parent / "trading_sender_locks"
        if lock_directory is not None and Path(lock_directory).resolve() != fixed.resolve():
            raise TradingError("SENDER_LOCK_DIRECTORY_FIXED")
        self.lock_directory, self.lease_seconds = fixed, lease_seconds
        self.handle, self.fencing = None, None
        self.host_handle = None

    def _acquire_host(self):
        """One paper sender on this host, even across independent restored DB paths."""
        global _host_held
        with _host_guard:
            if _host_held:
                raise TradingError("SECOND_SENDER_REJECTED")
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
                kernel.CreateMutexW.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.WaitForSingleObject.restype = wintypes.DWORD
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
                handle = kernel.CreateMutexW(None, False, "Global\\FinanceAnalyze.SinglePaperSender.v1")
                if not handle:
                    raise TradingError("SENDER_HOST_MUTEX_UNAVAILABLE")
                result = kernel.WaitForSingleObject(handle, 0)
                if result not in {0, 0x80}:
                    kernel.CloseHandle(handle)
                    raise TradingError("SECOND_SENDER_REJECTED" if result == 0x102 else "SENDER_HOST_MUTEX_UNAVAILABLE")
                self.host_handle = (kernel, handle)
            else:
                import fcntl
                import tempfile
                import stat
                flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(str(Path(tempfile.gettempdir()) / "finance-platform-single-paper-sender-v1.lock"), flags, 0o600)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    os.close(fd)
                    raise TradingError("SENDER_HOST_MUTEX_UNAVAILABLE")
                handle = os.fdopen(fd, "a+b")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    handle.close()
                    raise TradingError("SECOND_SENDER_REJECTED") from None
                self.host_handle = handle
            _host_held = True

    def __enter__(self):
        identifier(self.account_id)
        self.lock_directory.mkdir(parents=True, exist_ok=True)
        self.handle = (self.lock_directory / (self.account_id + ".sender.lock")).open("a+b")
        if os.fstat(self.handle.fileno()).st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise TradingError("SECOND_SENDER_REJECTED") from None
        try:
            self._acquire_host()
            now = datetime.now(timezone.utc)
            with self.service.database.transaction() as con:
                self.service._account(con, self.account_id, self.principal, True)
                old = con.execute("SELECT * FROM trading_leases WHERE account_id=?", (self.account_id,)).fetchone()
                if old and parse_instant(old["expires_at"]) > now:
                    raise TradingError("ACTIVE_SENDER_LEASE")
                self.fencing = 1 if old is None else old["fencing"] + 1
                con.execute("INSERT INTO trading_leases VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET worker_id=excluded.worker_id,fencing=excluded.fencing,expires_at=excluded.expires_at",
                    (self.account_id, self.worker_id, self.fencing, stamp(now + timedelta(seconds=self.lease_seconds))))
                pending = con.execute("SELECT o.client_order_id FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? AND o.status='sending'", (self.account_id,)).fetchall()
                for row in pending:
                    con.execute("UPDATE trading_orders SET status='unknown' WHERE client_order_id=?", (row["client_order_id"],))
                    con.execute("UPDATE trading_outbox SET status='unknown' WHERE client_order_id=? AND status='sending'", (row["client_order_id"],))
                if pending:
                    self.service._freeze(con, self.account_id, "SENDER_RESTART_UNKNOWN")
            return self
        except Exception:
            self._unlock()
            raise

    def require(self, con):
        if self.handle is None:
            raise TradingError("SENDER_AUTHORITY_REQUIRED")
        row = con.execute("SELECT * FROM trading_leases WHERE account_id=?", (self.account_id,)).fetchone()
        if row is None or row["worker_id"] != self.worker_id or row["fencing"] != self.fencing or parse_instant(row["expires_at"]) <= datetime.now(timezone.utc):
            raise TradingError("SENDER_FENCED")

    def heartbeat(self):
        with self.service.database.transaction() as con:
            self.require(con)
            con.execute("UPDATE trading_leases SET expires_at=? WHERE account_id=?", (stamp(datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)), self.account_id))

    def _unlock(self):
        global _host_held
        if self.host_handle is not None:
            with _host_guard:
                if os.name == "nt":
                    kernel, handle = self.host_handle
                    kernel.ReleaseMutex(handle)
                    kernel.CloseHandle(handle)
                else:
                    import fcntl
                    fcntl.flock(self.host_handle, fcntl.LOCK_UN)
                    self.host_handle.close()
                self.host_handle = None
                _host_held = False
        if self.handle is not None:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __exit__(self, *exc):
        try:
            with self.service.database.transaction() as con:
                con.execute("UPDATE trading_leases SET expires_at=? WHERE account_id=? AND worker_id=? AND fencing=?", (stamp(datetime.now(timezone.utc)), self.account_id, self.worker_id, self.fencing))
        finally:
            self._unlock()

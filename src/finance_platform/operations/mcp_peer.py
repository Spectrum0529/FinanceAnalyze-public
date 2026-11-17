"""Verify the launcher's actual Windows API socket before sending MCP proof."""
from contextlib import ExitStack, contextmanager
import ctypes as C
from ctypes import wintypes as W
from http import client as http
import json
import os
from pathlib import Path
import re
from socket import AF_INET, AF_INET6
import struct
import sys
from time import time

import anyio
import httpx

from finance_platform.core.errors import PlatformError
from finance_platform.core.source_binding import BindingError, checked_regular_file

ADDRESS = 0x0100007F
FILETIME_EPOCH = 116444736000000000
DOTNET_FILETIME_EPOCH = 504911232000000000
TCP_LISTEN, TCP_ESTABLISHED = 2, 5
MAX_RESPONSE = 8 * 1024 * 1024
TCP4_ROW = struct.Struct("<6I")
TCP6_ROW = struct.Struct("<16sII16sIIII")


class _ListenerMayBeOffline(ValueError):
    """A missing listener or process needs a passive ownership check."""


def _rejected():
    return PlatformError("MCP_MANAGED_LISTENER_REQUIRED", 503)


def _record(root, name):
    relative = ".local/platform/processes/" + name
    try:
        path = checked_regular_file(root, relative)
    except BindingError as error:
        if error.code == "BINDING_FILE_UNAVAILABLE":
            # Distinguish missing records from inaccessible files. The binding
            # check above still governs all paths that the verifier may open.
            (Path(root) / relative).lstat()
        raise
    with path.open("rb") as stream:
        raw = stream.read(524289)
    if len(raw) > 524288:
        raise ValueError
    return json.loads(raw.decode("utf-8-sig"))


def _function(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


class WindowsPeer:
    def __init__(self, root, expected, port):
        self.root, self.expected, self.port = root, expected, port
        if sys.platform != "win32":
            raise _rejected()
        self.kernel = C.WinDLL("kernel32", use_last_error=True, winmode=0x800)
        self.native = C.WinDLL("ntdll", winmode=0x800)
        ip = C.WinDLL("iphlpapi", winmode=0x800)
        self.tcp_table = _function(ip, "GetExtendedTcpTable",
                                   [C.c_void_p, C.POINTER(W.DWORD), W.BOOL, W.ULONG, C.c_int, W.ULONG], W.DWORD)
        self.wait = _function(self.kernel, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD)

    def _token_user(self, process):
        advapi = C.WinDLL("advapi32", use_last_error=True, winmode=0x800)
        open_token = _function(advapi, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
        info = _function(advapi, "GetTokenInformation",
                         [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
        length_sid = _function(advapi, "GetLengthSid", [C.c_void_p], W.DWORD)
        close = _function(self.kernel, "CloseHandle", [W.HANDLE], W.BOOL)
        token = W.HANDLE()
        if not open_token(process, 8, C.byref(token)):
            raise ValueError
        try:
            length = W.DWORD()
            info(token, 1, None, 0, C.byref(length))  # TokenUser
            if not 0 < length.value <= 65536:
                raise ValueError
            buffer = C.create_string_buffer(length.value)
            if not info(token, 1, buffer, length.value, C.byref(length)):
                raise ValueError
            sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            size = length_sid(sid)
            if not 8 <= size <= 68:
                raise ValueError
            return C.string_at(sid, size)
        finally:
            close(token)

    @contextmanager
    def _process(self, pid):
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError
        open_process = _function(self.kernel, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
        close = _function(self.kernel, "CloseHandle", [W.HANDLE], W.BOOL)
        handle = open_process(0x101000, False, pid)  # Query limited information and synchronize.
        if not handle:
            if C.get_last_error() == 87:  # ERROR_INVALID_PARAMETER requires a TCP table check.
                raise _ListenerMayBeOffline
            raise ValueError
        try:
            self._require_running(handle)
            times = _function(self.kernel, "GetProcessTimes", [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL)
            created, exited, kernel, user = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
            query_image = _function(self.kernel, "QueryFullProcessImageNameW",
                                    [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL)
            capacity = W.DWORD(32768)
            image = C.create_unicode_buffer(capacity.value)
            if not times(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
                raise ValueError
            if not query_image(handle, 0, image, C.byref(capacity)):
                raise ValueError
            basic = (C.c_size_t * 6)()
            query = _function(self.native, "NtQueryInformationProcess",
                              [W.HANDLE, C.c_int, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)], C.c_long)
            if query(handle, 0, C.byref(basic), C.sizeof(basic), None) != 0:
                raise ValueError
            born = (created.dwHighDateTime << 32) | created.dwLowDateTime
            yield handle, born, Path(image.value).resolve(), basic[5]
        finally:
            close(handle)

    def _require_running(self, handle):
        state = self.wait(handle, 0)
        if state == 0:  # WAIT_OBJECT_0: the process object is signaled.
            raise _ListenerMayBeOffline
        if state != 258:  # WAIT_TIMEOUT: no process exit.
            raise ValueError

    def _tcp(self):
        return self._tcp_rows(AF_INET, TCP4_ROW)

    def _tcp_rows(self, family, layout):
        size = W.DWORD()
        for _ in range(3):
            if self.tcp_table(None, C.byref(size), False, family, 5, 0) != 122 or not 4 <= size.value <= 4 * 1024**2:
                raise ValueError
            buffer = C.create_string_buffer(size.value)
            status = self.tcp_table(buffer, C.byref(size), False, family, 5, 0)
            if status == 122:
                continue
            if status != 0 or not 4 <= size.value <= len(buffer):
                raise ValueError
            count, = struct.unpack_from("<I", buffer)
            if count > (size.value - 4) // layout.size:
                raise ValueError
            return [layout.unpack_from(buffer, 4 + layout.size * index) for index in range(count)]
        raise ValueError

    def _port_has_no_listener(self):
        try:
            if any(state == TCP_LISTEN and self._port(port) == self.port
                   for state, _, port, _, _, _ in self._tcp()):
                return False
            # The IPv6 table can include dual-stack listeners for this port.
            return not any(state == TCP_LISTEN and self._port(port) == self.port
                           for _, _, port, _, _, _, state, _ in self._tcp_rows(AF_INET6, TCP6_ROW))
        except (ValueError, OSError):
            return False

    @staticmethod
    def _port(value):
        return (value & 255) << 8 | (value >> 8) & 255

    def _require_socket(self, pid, local_port=None, *, rows=None):
        rows = self._tcp() if rows is None else rows
        listeners = [(address, owner) for state, address, port, _, _, owner in rows
                     if state == TCP_LISTEN and self._port(port) == self.port]
        if not listeners:
            raise _ListenerMayBeOffline
        if listeners != [(ADDRESS, pid)]:
            raise ValueError
        if local_port is not None:
            owners = [owner for state, address, port, peer, peer_port, owner in rows
                      if state == TCP_ESTABLISHED and address == peer == ADDRESS
                      and self._port(port) == self.port and self._port(peer_port) == local_port]
            if owners != [pid]:
                raise ValueError

    def _require_browser(self, pid, process, caller):
        address, port = caller
        if pid != os.getpid() or address != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        rows = self._tcp()
        self._require_socket(pid, port, rows=rows)
        client_pid, = [owner for state, local, source, remote, target, owner in rows
                       if state == TCP_ESTABLISHED and local == remote == ADDRESS
                       and self._port(source) == port and self._port(target) == self.port]
        with self._process(client_pid) as (browser, _, _, _):
            if self._token_user(browser) != self._token_user(process):
                raise PlatformError("MCP_WINDOWS_USER_MISMATCH", 403)

    @contextmanager
    def verified(self, *, caller=None):
        """Hold process identity and verify the kernel owner of each TCP connection."""
        verified_before_connect = False
        try:
            entries = _record(self.root, "native.json")
            api_entries = [entry for entry in entries if entry["component"] == "api"]
            if not api_entries and isinstance(entries, list):
                raise _ListenerMayBeOffline
            row, = api_entries
            binding = _record(self.root, "api.binding.json")
            if (Path(row["root"]).resolve() != self.root or Path(binding["root"]).resolve() != self.root
                    or binding["role"] != "api" or binding["installation"] != "SOURCE"
                    or not re.fullmatch(r"[0-9a-f]{64}", binding["auth_database_binding"])):
                raise ValueError
            for name in ("source_hash", "lock_sha256"):
                if row[name] != self.expected[name] or binding[name] != self.expected[name]:
                    raise ValueError
            if binding["root_hash"] != self.expected["root_hash"]:
                raise ValueError
            if ("auth_database_binding" in self.expected
                    and binding["auth_database_binding"] != self.expected["auth_database_binding"]):
                raise ValueError
            with ExitStack() as stack:
                parent_pid, pid = row["process_id"], binding["process_id"]
                parent, parent_born, parent_image, _ = stack.enter_context(self._process(parent_pid))
                if (parent_born + DOTNET_FILETIME_EPOCH != row["started_ticks"]
                        or parent_image != Path(row["executable"]).resolve()
                        or parent_image != (self.root / ".venv/Scripts/python.exe").resolve()):
                    raise ValueError
                process, born, image, owner = stack.enter_context(self._process(pid))
                if pid != parent_pid and owner != parent_pid:
                    raise ValueError
                if image not in {parent_image, Path(sys._base_executable).resolve()}:
                    raise ValueError
                started_at = (born - FILETIME_EPOCH) / 10**7
                if not started_at - 2 <= binding["recorded_at"] <= time() + 10:
                    raise ValueError
                own = _function(self.kernel, "GetCurrentProcess", [], W.HANDLE)
                if self._token_user(process) != self._token_user(own()):
                    raise PlatformError("MCP_WINDOWS_USER_MISMATCH", 403)

                def verify(local_port=None):
                    self._require_running(parent)
                    self._require_running(process)
                    self._require_socket(pid, local_port)

                if caller is None:
                    verify()
                    verified_before_connect = True
                else:
                    self._require_browser(pid, process, caller)
                yield verify
                if caller is None:
                    verify()
                else:
                    self._require_running(process)
        except (FileNotFoundError, _ListenerMayBeOffline):
            if caller is None and not verified_before_connect and self._port_has_no_listener():
                raise PlatformError("MCP_PRODUCT_UNAVAILABLE", 503) from None
            raise _rejected() from None
        except (KeyError, OSError, TypeError, ValueError):
            raise _rejected() from None


class ManagedMcpTransport(httpx.AsyncBaseTransport):
    def __init__(self, peer, timeout):
        self.peer, self.timeout = peer, timeout

    async def handle_async_request(self, request):
        return await anyio.to_thread.run_sync(self._request, request)

    def _request(self, request):
        if request.url.scheme != "http" or request.url.host != "127.0.0.1" or request.url.port != self.peer.port:
            raise PlatformError("MCP_LOCAL_ORIGIN_REQUIRED", 422)
        connection = http.HTTPConnection("127.0.0.1", self.peer.port, timeout=self.timeout)
        try:
            with self.peer.verified() as verify:
                try:
                    connection.connect()
                    verify(connection.sock.getsockname()[1])
                    connection.auto_open = False
                    headers = {**dict(request.headers), "accept-encoding": "identity", "connection": "close"}
                    connection.request(request.method, request.url.raw_path.decode("ascii"),
                                       body=request.content, headers=headers)
                    response = connection.getresponse()
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise PlatformError("MCP_RESPONSE_TOO_LARGE", 413)
                    fields = [(name, value) for name, value in response.getheaders() if name.lower() != "set-cookie"]
                    return httpx.Response(response.status, headers=fields, content=raw)
                except (OSError, http.HTTPException):
                    raise PlatformError("MCP_PRODUCT_UNAVAILABLE", 503) from None
        finally:
            connection.close()

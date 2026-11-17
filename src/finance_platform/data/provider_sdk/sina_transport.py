"""One HTTPS Sina full-history entity; no factor/catalog/fallback/proxy/cookies."""
import http.client
import re
import time
from types import SimpleNamespace

from finance_platform.data.contracts import DataError
from finance_platform.data.network import _PinnedHTTPS
from .contracts import LIMITS, SINA_INTERFACE, valid_sina_symbol, now
from .http_entity import BoundedEntity
from .network_gate import _attempt, _encoding_kind, _exception_kind, _protocol_subtype
from .history_contracts import SINA_QFQ_INTERFACE, NATIVE_INTERFACE


class SinaHTTP:
    def __init__(self, call, budget, *, fixture_exchange=None, kind="prices"):
        self.call, self.budget, self.fixture_exchange = call, budget, fixture_exchange
        self.used = False
        if kind not in {"prices", "qfq"}: budget.abort("SDK_DESTINATION_FORBIDDEN")
        self.kind = kind

    def get(self, url, **unknown):
        call, budget = self.call, self.budget
        symbol = call.get("provider_symbol")
        path = "/staticdata/us/" + symbol if valid_sina_symbol(symbol) else ""
        if self.kind == "qfq": path = "/us_stock/company/reinstatement/" + symbol + "_qfq.js" if valid_sina_symbol(symbol) else ""
        if (unknown or self.used or call.get("provider") != "akshare" or call.get("interface") not in {SINA_INTERFACE, SINA_QFQ_INTERFACE, NATIVE_INTERFACE}
                or (self.kind == "qfq" and call.get("interface") not in {SINA_QFQ_INTERFACE, NATIVE_INTERFACE})
                or not valid_sina_symbol(symbol) or url != "https://finance.sina.com.cn" + path):
            budget.abort("SDK_DESTINATION_FORBIDDEN")
        self.used = True
        budget.begin()
        diagnostic = _attempt(budget, "sina_qfq_factors" if self.kind == "qfq" else "sina_us_full_history", "finance.sina.com.cn", path)
        deadline = min(time.monotonic() + 5, budget.deadline)
        entity = BoundedEntity(budget, deadline=deadline, allow_gzip=True,
                               encoded_limit=256*1024 if self.kind == "qfq" else None,
                               decoded_limit=512*1024 if self.kind == "qfq" else None)
        connection = None
        response = None
        framing = "IDENTITY"
        control_bytes = 0

        def check():
            budget.check()
            if time.monotonic() >= deadline:
                budget.abort("SDK_TIMEOUT")

        try:
            if self.fixture_exchange is not None:
                # Trusted offline constructor injection only, never request JSON.
                raw = self.fixture_exchange(url, {})
                entity.feed(raw)
                status = 200
            else:
                addresses = budget.public_addresses("finance.sina.com.cn")
                check()
                class Response(http.client.HTTPResponse):
                    def begin(self):
                        original = self.fp
                        class Reader:
                            count = 0
                            mode = "HEADERS"
                            def before(self):
                                check()
                                request_socket.settimeout(max(0.001, deadline - time.monotonic()))
                            def control(self, chunk):
                                self.count += len(chunk)
                                budget.header_bytes += len(chunk)
                                if self.count > LIMITS["header_bytes"] or budget.header_bytes > LIMITS["header_bytes"] * LIMITS["requests"]:
                                    budget.abort("SDK_HEADER_LIMIT_EXCEEDED")
                            def readline(self, maximum=-1):
                                # One bounded read per byte: BufferedReader.readline
                                # could otherwise hide slow-drip reads past deadline.
                                allowance = LIMITS["header_bytes"] - self.count
                                cap = min(maximum if maximum >= 0 else allowance + 1, allowance + 1)
                                line = bytearray()
                                while len(line) < cap:
                                    self.before()
                                    byte = original.read1(1)
                                    check()
                                    self.control(byte)
                                    line.extend(byte)
                                    if not byte or byte == b"\n":
                                        break
                                chunk = bytes(line)
                                if self.mode == "CHUNK_SIZE" and not re.fullmatch(rb"[0-9a-fA-F]{1,8}\r\n", chunk):
                                    # Deliberately reject extensions, signs and LF-only.
                                    budget.abort("SDK_FRAMING_UNSUPPORTED")
                                if self.mode == "TRAILER":
                                    if not chunk.endswith(b"\r\n"):
                                        budget.abort("SDK_FRAMING_UNSUPPORTED")
                                    if chunk != b"\r\n":
                                        if not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[\t\x20-\x7e]*\r\n", chunk):
                                            budget.abort("SDK_FRAMING_UNSUPPORTED")
                                        if chunk.split(b":", 1)[0].lower() in {b"content-length", b"transfer-encoding", b"content-encoding", b"host", b"trailer"}:
                                            budget.abort("SDK_FRAMING_UNSUPPORTED")
                                return chunk
                            def read(self, size=-1):
                                if not 0 <= size <= 65536:
                                    budget.abort("SDK_FRAMING_UNSUPPORTED")
                                result = bytearray()
                                while len(result) < size:
                                    self.before()
                                    piece = original.read1(size - len(result))
                                    check()
                                    if not piece:
                                        break
                                    result.extend(piece)
                                chunk = bytes(result)
                                if self.mode == "CHUNK_CRLF":
                                    self.control(chunk)
                                return chunk
                            def read1(self, size=-1):
                                if not 0 <= size <= 65536:
                                    budget.abort("SDK_FRAMING_UNSUPPORTED")
                                self.before()
                                chunk = original.read1(size)
                                check()
                                return chunk
                            def __getattr__(self, name):
                                return getattr(original, name)
                        self._bounded_reader = self.fp = Reader()
                        super().begin()

                    def _read_next_chunk_size(self):
                        reader = self.fp
                        reader.mode = "CHUNK_SIZE"
                        try:
                            size = super()._read_next_chunk_size()
                        finally:
                            reader.mode = "BODY"
                        if entity.encoded + size > entity.encoded_limit or budget.encoded_bytes + size > LIMITS["unit_bytes"]:
                            budget.abort("SDK_RESPONSE_LIMIT_EXCEEDED")
                        return size

                    def _safe_read(self, size):
                        # Standard chunk parser discards, but does not validate,
                        # the two-byte separator; require its exact CRLF here.
                        separator = self.chunked and self.chunk_left == 0 and size == 2
                        reader = self.fp
                        if separator:
                            reader.mode = "CHUNK_CRLF"
                        try:
                            chunk = super()._safe_read(size)
                        finally:
                            reader.mode = "BODY"
                        if separator and chunk != b"\r\n":
                            budget.abort("SDK_FRAMING_UNSUPPORTED")
                        return chunk

                    def _read_and_discard_trailer(self):
                        reader = self.fp
                        reader.mode = "TRAILER"
                        try:
                            super()._read_and_discard_trailer()
                        finally:
                            reader.mode = "BODY"
                connection = _PinnedHTTPS("finance.sina.com.cn", addresses[0], timeout=min(3, max(0.001, deadline-time.monotonic())))
                connection.response_class = Response
                diagnostic["stage"] = "CONNECT_TLS_OR_REQUEST"
                connection.request("GET", path, headers={
                    "User-Agent": "python-requests/2.34.2", "Accept": "*/*",
                    "Connection": "close", "Accept-Encoding": "identity"})
                check()
                request_socket = connection.sock
                request_socket.settimeout(max(0.001, deadline-time.monotonic()))
                diagnostic["stage"] = "READ_HEADERS"
                response = connection.getresponse()
                check()
                status = response.status
                diagnostic["status"] = status
                diagnostic["stage"] = "RESPONSE_CHECK"
                if 300 <= status < 400:
                    budget.abort("SDK_REDIRECT_FORBIDDEN")
                if status != 200:
                    budget.abort("PROVIDER_RATE_LIMITED" if status == 429 else "PROVIDER_ACCESS_DENIED" if status in {401, 403} else "SDK_UPSTREAM_HTTP_ERROR")
                transfers = response.headers.get_all("Transfer-Encoding", [])
                lengths = response.headers.get_all("Content-Length", [])
                if transfers:
                    if len(transfers) != 1 or transfers[0].strip().lower() != "chunked" or lengths or not response.chunked:
                        budget.abort("SDK_FRAMING_UNSUPPORTED")
                    framing = "CHUNKED"
                else:
                    framing = "CONTENT_LENGTH" if lengths else "CONNECTION_CLOSE"
                encodings = response.headers.get_all("Content-Encoding", [])
                if len(encodings) > 1:
                    budget.abort("SDK_ENCODING_UNSUPPORTED")
                encoding = encodings[0] if encodings else "identity"
                diagnostic["content_encoding"] = _encoding_kind(encoding)
                entity.set_encoding(encoding.encode("ascii", errors="strict"))
                if lengths and (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                                or len(lengths[0]) > 8 or int(lengths[0]) > entity.encoded_limit):
                    budget.abort("SDK_RESPONSE_LIMIT_EXCEEDED")
                while True:
                    check()
                    diagnostic["stage"] = "READ_BODY"
                    # Reader.before owns each I/O timeout. read1 may now be a
                    # no-I/O EOF after HTTPResponse closed its exhausted file.
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    entity.feed(chunk)
                    diagnostic.update(encoded_bytes=entity.encoded, decoded_bytes=entity.decoded)
                if lengths and int(lengths[0]) != entity.encoded:
                    budget.abort("SDK_FRAMING_UNSUPPORTED")
                control_bytes = response._bounded_reader.count
            raw = entity.finish()
            check()
            received = now()  # Complete HTTP entity receipt, never inferred historical availability.
            diagnostic.update(status=status, content_encoding=entity.encoding, encoded_bytes=entity.encoded,
                              decoded_bytes=entity.decoded, stage="COMPLETE")
            return SimpleNamespace(body=raw, received_at=received, decoding={**entity.capture_metadata(),
                                   "http_framing": framing, "framing_control_bytes": control_bytes})
        except DataError as error:
            diagnostic["exception_type"] = _exception_kind(error)
            raise
        except (OSError, http.client.HTTPException, UnicodeError) as error:
            diagnostic["exception_type"] = _exception_kind(error)
            diagnostic["exception_subtype"] = _protocol_subtype(error)
            budget.abort("SDK_TRANSPORT_FAILED")
        finally:
            diagnostic.update(encoded_bytes=entity.encoded, decoded_bytes=entity.decoded)
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

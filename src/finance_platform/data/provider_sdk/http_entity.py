"""Bounded identity/single-gzip entity decoding; never automatic libcurl decode."""
import hashlib
import time
import zlib

from .contracts import LIMITS


class BoundedEntity:
    OUTPUT_CHUNK = 65536

    def __init__(self, budget, *, deadline, allow_gzip=False, encoded_limit=None, decoded_limit=None):
        self.budget, self.deadline, self.allow_gzip = budget, deadline, allow_gzip
        self.encoding = "identity"
        self.encoded_limit = LIMITS["response_bytes"] if encoded_limit is None else encoded_limit
        self.decoded_limit = LIMITS["response_bytes"] if decoded_limit is None else decoded_limit
        if any(type(n) is not int or not 0 < n <= LIMITS["response_bytes"] for n in (self.encoded_limit, self.decoded_limit)):
            budget.abort("SDK_RESPONSE_LIMIT_EXCEEDED")
        self.encoded = self.decoded = 0
        self._header_seen = self._finished = False
        self._decoder = None
        self._body = bytearray()
        self._encoded_hash = hashlib.sha256()

    def _check(self):
        self.budget.check()
        if time.monotonic() >= self.deadline:
            self.budget.abort("SDK_TIMEOUT")

    def set_encoding(self, value):
        self._check()
        if self._header_seen or self.encoded or self._finished:
            self.budget.abort("SDK_ENCODING_UNSUPPORTED")
        self._header_seen = True
        value = value.strip().lower()
        if value in {b"", b"identity"}:
            return
        if value != b"gzip" or not self.allow_gzip:
            self.budget.abort("SDK_ENCODING_UNSUPPORTED")
        self.encoding = "gzip"
        # gzip header/trailer mode validates header, CRC32 and uncompressed size.
        self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def _accept(self, output):
        self._check()
        if self.decoded+len(output) > self.decoded_limit:
            self.budget.abort("SDK_RESPONSE_LIMIT_EXCEEDED")
        self.budget.consume(output, self.decoded)
        self.decoded += len(output)
        self._body.extend(output)

    def feed(self, raw):
        self._check()
        if self._finished or type(raw) is not bytes:
            self.budget.abort("SDK_CONTENT_DECODING_INVALID")
        # Independent compressed/entity input budget, in addition to existing
        # decoded financial bytes budget. No compressed body is buffered.
        if (self.encoded + len(raw) > self.encoded_limit
                or self.budget.encoded_bytes + len(raw) > LIMITS["unit_bytes"]):
            self.budget.abort("SDK_RESPONSE_LIMIT_EXCEEDED")
        self.encoded += len(raw)
        self.budget.encoded_bytes += len(raw)
        self._encoded_hash.update(raw)
        if self.encoding == "identity":
            self._accept(raw)
            return
        for offset in range(0, len(raw), self.OUTPUT_CHUNK):
            pending = raw[offset:offset + self.OUTPUT_CHUNK]
            while pending:
                self._check()
                # max_length=0 would mean unlimited. Always permit at most one
                # byte beyond remaining allowance to detect an overrun safely.
                remaining = min(self.decoded_limit - self.decoded,
                                LIMITS["unit_bytes"] - self.budget.bytes)
                maximum = min(self.OUTPUT_CHUNK, remaining + 1)
                try:
                    output = self._decoder.decompress(pending, maximum)
                except zlib.error:
                    self.budget.abort("SDK_CONTENT_DECODING_INVALID")
                self._check()
                if self._decoder.unused_data:
                    # Reject concatenated gzip members, padding and trailing junk.
                    self.budget.abort("SDK_CONTENT_DECODING_INVALID")
                tail = self._decoder.unconsumed_tail
                if not output and len(tail) >= len(pending):
                    self.budget.abort("SDK_CONTENT_DECODING_INVALID")
                self._accept(output)
                pending = tail

    def finish(self):
        self._check()
        if self._finished:
            self.budget.abort("SDK_CONTENT_DECODING_INVALID")
        if self._decoder is not None and (not self._decoder.eof
                or self._decoder.unused_data or self._decoder.unconsumed_tail):
            self.budget.abort("SDK_CONTENT_DECODING_INVALID")
        # No flush()/gzip.decompress() call with an unbounded output allocation.
        self._finished = True
        return bytes(self._body)

    def capture_metadata(self):
        if not self._finished:
            self.budget.abort("SDK_CONTENT_DECODING_INVALID")
        if self.encoding == "identity":
            return {}
        return {"content_decoding": "GZIP_DECODED_ENTITY_NOT_TLS_WIRE",
                "content_encoding": "gzip", "encoded_entity_bytes": self.encoded,
                "decoded_entity_bytes": self.decoded,
                "encoded_entity_sha256": self._encoded_hash.hexdigest(),
                "encoded_entity_archived": False}

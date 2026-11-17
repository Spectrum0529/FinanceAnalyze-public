"""One private, sequenced, bounded TA session; no credential/environment inputs."""
import hashlib
import hmac
import json
import struct
import threading

MAX_FRAME = 2621440
MAX_WIRE = 8388608


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def optional_trace_payload(value, maximum):
    """Optional nested numeric observations never raise the old payload cap.

    Missing observations are explicitly counted by the trusted parent reader.
    No required trace key, row, result, effect or binding is removed.
    """
    if type(value) is not dict or type(value.get('trace')) is not list:
        return value
    try:
        if len(encoded(value)) <= maximum:return value
    except (TypeError,ValueError):
        pass  # Only optional telemetry is stripped; invalid required data fails below.
    rows=[{k:v for k,v in row.items() if k!='model_observation'} if type(row)is dict else row for row in value['trace']]
    return dict(value,trace=rows)


def checked(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("TA_DUPLICATE_FIELD")
            value[key] = item
        return value
    def constant(value):
        raise ValueError("TA_NONFINITE_NUMBER")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


class Channel:
    def __init__(self, reader, writer, key, *, side):
        if type(key) is not bytes or len(key) != 32 or side not in {"parent", "child"}:
            raise ValueError("TA_CHANNEL_INVALID")
        self.reader, self.writer, self.key, self.side = reader, writer, key, side
        self.sent = self.received = self.tx = self.rx = 0
        self.lock = threading.RLock()

    def send(self, kind, payload):
        if kind in {'STAGED','FAILURE'}:
            overhead=len(encoded({'side':self.side,'seq':self.sent,'kind':kind,'payload':None}))-4
            maximum=min(MAX_FRAME,MAX_WIRE-self.tx)-overhead
            if kind=='FAILURE':maximum=min(maximum,524288)
            payload=optional_trace_payload(payload,maximum)
        body = {"side": self.side, "seq": self.sent, "kind": kind, "payload": payload}
        raw = encoded(body)
        if len(raw) > MAX_FRAME or self.tx + len(raw) > MAX_WIRE:
            raise ValueError("TA_CHANNEL_BUDGET")
        mac = hmac.digest(self.key, raw, "sha256")
        self.writer.write(struct.pack("!I", len(raw)) + raw + mac)
        self.writer.flush()
        self.sent += 1
        self.tx += len(raw)

    def _read(self, count):
        parts = bytearray()
        while len(parts) < count:
            chunk = self.reader.read(count - len(parts))
            if not chunk:
                raise EOFError("TA_CHANNEL_EOF")
            parts.extend(chunk)
        return bytes(parts)

    def receive(self):
        size = struct.unpack("!I", self._read(4))[0]
        if size > MAX_FRAME or self.rx + size > MAX_WIRE:
            raise ValueError("TA_CHANNEL_BUDGET")
        raw, mac = self._read(size), self._read(32)
        if not hmac.compare_digest(mac, hmac.digest(self.key, raw, "sha256")):
            raise ValueError("TA_CHANNEL_AUTHENTICATION_FAILED")
        body = checked(raw)
        expected = "child" if self.side == "parent" else "parent"
        if (type(body) is not dict or set(body) != {"side", "seq", "kind", "payload"}
                or body["side"] != expected or type(body["seq"]) is not int
                or body["seq"] != self.received or type(body["kind"]) is not str):
            raise ValueError("TA_CHANNEL_REPLAY_OR_SCHEMA")
        self.received += 1
        self.rx += len(raw)
        return body["kind"], body["payload"]

    def exchange(self, kind, payload):
        with self.lock:
            self.send(kind, payload)
            reply, value = self.receive()
            if reply != kind + "_OK":
                raise ValueError("TA_HOST_CONTROL_REFUSED")
            return value

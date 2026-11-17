"""Alpaca JSON WebSocket control flow and minute/updated-bar persistence."""
from contextlib import closing
import json
import socket
import ssl
from urllib.parse import urlsplit

from finance_platform.core.contracts import fingerprint, payload, parse_instant
from .contracts import DataError, load_json, safe_bytes, now
from .network import ConnectionConfig
from .providers import AlpacaBars


class AlpacaStreamState:
    def __init__(self, connection, symbols):
        self.config = ConnectionConfig.from_mapping(connection, "alpaca")
        self.bars = AlpacaBars(connection)
        self.symbols = tuple(symbols)
        if not 1 <= len(self.symbols) <= 30 or len(set(self.symbols)) != len(self.symbols):
            raise DataError("STREAM_SYMBOL_LIMIT")
        self.bars.initial({"symbols": list(symbols), "start": "2020-01-01T00:00:00Z", "end": "2020-01-01T00:00:00Z", "timeframe": "1Min", "limit": 1})
        self.state = "CONNECTING"

    def auth_message(self):
        if self.state != "CONNECTED":
            raise DataError("STREAM_STATE_INVALID")
        headers = self.config.headers()
        # Credential-bearing outbound message stays in memory, never archived/checkpointed.
        return json.dumps({"action": "auth", "key": headers["APCA-API-KEY-ID"], "secret": headers["APCA-API-SECRET-KEY"]})

    def subscription_message(self):
        if self.state != "AUTHENTICATED":
            raise DataError("STREAM_STATE_INVALID")
        return json.dumps({"action": "subscribe", "bars": self.symbols, "updatedBars": self.symbols})

    def receive(self, raw, dataset, license_id, received_at):
        messages = load_json(raw)
        if not isinstance(messages, list) or not 1 <= len(messages) <= 10000:
            raise DataError("STREAM_MESSAGE_INVALID")
        records = []
        for message in messages:
            kind = message.get("T")
            if kind == "error":
                code = message.get("code")
                raise DataError({402: "STREAM_AUTHENTICATION_FAILED", 403: "STREAM_AUTHENTICATION_FAILED",
                                 405: "STREAM_SYMBOL_LIMIT", 406: "STREAM_CONNECTION_LIMIT", 407: "STREAM_SLOW_CLIENT",
                                 409: "STREAM_PERMISSION_DENIED"}.get(code, "STREAM_PROTOCOL_ERROR"))
            if kind == "success":
                expected = {"connected": ("CONNECTING", "CONNECTED"), "authenticated": ("CONNECTED", "AUTHENTICATED")}
                transition = expected.get(message.get("msg"))
                if not transition or self.state != transition[0]:
                    raise DataError("STREAM_STATE_INVALID")
                self.state = transition[1]
            elif kind == "subscription":
                if self.state != "AUTHENTICATED" or set(message.get("bars", [])) != set(self.symbols) or set(message.get("updatedBars", [])) != set(self.symbols):
                    raise DataError("STREAM_SUBSCRIPTION_MISMATCH")
                self.state = "SUBSCRIBED"
            elif kind in {"b", "u"}:
                if self.state != "SUBSCRIBED" or message.get("S") not in self.symbols:
                    raise DataError("STREAM_UNSUBSCRIBED_DATA")
                # Updated-bar flag is transport lineage; identical OHLC does not invent a revision.
                bar = {k: v for k, v in message.items() if k not in {"T", "S"}}
                cursor = self.bars.initial({"symbols": [message["S"]], "start": message["t"], "end": message["t"], "timeframe": "1Min", "limit": 1})
                parsed, _ = self.bars.parse(json.dumps({"bars": {message["S"]: [payload(bar)]}, "next_page_token": None}).encode(),
                                           cursor=cursor, dataset=dataset, license_id=license_id, received_at=received_at)
                records.extend(parsed)
            else:
                raise DataError("STREAM_CHANNEL_UNSUPPORTED")
        return records


def replay_stream(service, owner_id, dataset_id, connection, symbols, frames, *, context=None):
    """Recorded/synthetic frames with explicit observed fixture timestamps, no network."""
    dataset = service.dataset(owner_id, dataset_id)
    if dataset["scope"] != "SYNTHETIC":
        raise DataError("RECORDED_FIXTURE_SCOPE_REQUIRED")
    source = service.source(owner_id, dataset["source_id"], dataset["source_version"])
    state = AlpacaStreamState(connection, symbols)
    appended = 0
    for idx, frame in enumerate(frames):
        if context:
            context.require_active()
        received = frame["received_at"]
        raw = frame["raw"]
        reference = service.archive(owner_id, source, raw, parameters={"protocol": "alpaca-ws-json-v2", "frame": idx},
                                    received_at=received, scope=dataset["scope"], schema="alpaca-ws-json-v2")
        records = state.receive(raw, dataset, source["license"]["license_id"], received)
        appended += service.append_records(owner_id, dataset_id, records, lineage=reference)["appended"]
    return {"state": state.state, "appended": appended, "verification": "LOCAL_VERIFIED", "gap_status": "FIXTURE_ONLY"}


def stream_capture(context, service, owner_id, dataset_id, connection, symbols, *, max_frames=100, websocket_factory=None):
    """Bounded real WebSocket path. Disconnects preserve bytes; no lossless claim."""
    if type(max_frames) is not int or not 1 <= max_frames <= 10000:
        raise DataError("STREAM_FRAME_LIMIT")
    state = AlpacaStreamState(connection, symbols)
    if state.config.missing():
        raise DataError("CONNECTION_NOT_CONFIGURED")
    try:
        from websockets.sync.client import connect
    except ImportError:
        raise DataError("DEPENDENCY_MISSING_WEBSOCKETS") from None
    dataset = service.dataset(owner_id, dataset_id)
    source = service.source(owner_id, dataset["source_id"], dataset["source_version"])
    service._rights(source, dataset["scope"], "development")
    host = "stream.data.alpaca.markets"
    import ipaddress
    context.require_active()
    sock = None
    url = "wss://" + host + "/v2/" + state.config.feed
    if websocket_factory is None:
        try:
            addresses = list(dict.fromkeys(i[4][0] for i in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise DataError("SSRF_DNS_FORBIDDEN")
            sock = socket.create_connection((addresses[0], 443), 10)
        except OSError:
            raise DataError("STREAM_TRANSPORT_ERROR") from None
        websocket = connect(url, sock=sock, ssl=ssl.create_default_context(), server_hostname=host,
                            proxy=None, open_timeout=10, max_size=16 * 1024 * 1024)
    else:
        # Test-only injection absent from config/API/jobs; exercises real RFC6455 on loopback.
        websocket = websocket_factory(url)
    count, appended = 0, 0
    try:
        with websocket as ws:
            while count < max_frames:
                context.require_active()
                try:
                    message = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                raw = message.encode() if isinstance(message, str) else message
                safe_bytes(raw)
                received = now()
                reference = service.archive(owner_id, source, raw, parameters={"protocol": "alpaca-ws-json-v2", "session": context.run.run_id, "frame": count},
                                            received_at=received, scope=dataset["scope"], schema="alpaca-ws-json-v2")
                previous = state.state
                records = state.receive(raw, dataset, source["license"]["license_id"], received)
                context.require_active()
                appended += service.append_records(owner_id, dataset_id, records, lineage=reference)["appended"]
                if previous == "CONNECTING" and state.state == "CONNECTED":
                    ws.send(state.auth_message())
                elif previous == "CONNECTED" and state.state == "AUTHENTICATED":
                    ws.send(state.subscription_message())
                count += 1
                context.checkpoint(min(99, count), {"stream_state": state.state, "frames": count})
        return {"frames": count, "appended": appended, "gap_status": "OBSERVED_SESSION_ONLY_NO_REPLAY_GUARANTEE"}
    except DataError:
        raise
    except PlatformError:
        raise
    except Exception:
        raise DataError("STREAM_DISCONNECTED_BACKFILL_REQUIRED") from None
    finally:
        if sock is not None:
            sock.close()


from finance_platform.core.errors import PlatformError

"""Alpaca paper-only REST and trade_updates protocol. No live endpoint exists."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from finance_platform.core.contracts import RunMode, canonical, identifier
from .accounting import TradingError, decimal
from .models import OrderIntent

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
PAPER_STREAM = "wss://paper-api.alpaca.markets/stream"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise TradingError("BROKER_REDIRECT_REJECTED")


@dataclass(frozen=True)
class AlpacaPaperConfig:
    account_id: str
    broker_account_id: str
    key_env: str
    secret_env: str
    mode: RunMode = RunMode.BROKER_PAPER

    def __post_init__(self):
        identifier(self.account_id)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.account_id):
            raise TradingError("PAPER_ACCOUNT_SCOPE_FORMAT")
        identifier(self.broker_account_id)
        scope = "FINANCE_PLATFORM_ALPACA_PAPER_" + self.account_id.upper().replace("-", "_").replace(".", "_").replace(":", "_")
        if self.key_env != scope + "_API_KEY" or self.secret_env != scope + "_API_SECRET":
            raise TradingError("PAPER_SECRET_SCOPE_REQUIRED")
        if self.mode is not RunMode.BROKER_PAPER:
            raise TradingError("LIVE_DISABLED")


class AlpacaPaperBroker:
    supported_order_types = ("market", "limit")
    supported_time_in_force = ("day",)

    def __init__(self, config: AlpacaPaperConfig, *, timeout=5):
        if type(config) is not AlpacaPaperConfig or not 0 < timeout <= 15:
            raise TradingError("INVALID_PAPER_CONFIG")
        self.config, self.timeout = config, timeout
        self.endpoint, self.stream_endpoint = PAPER_ENDPOINT, PAPER_STREAM
        self._offline = False
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    @classmethod
    def offline_server(cls, config, endpoint, *, timeout=1):
        """Explicit test factory, literal loopback only; never a production config field."""
        parts = urlsplit(endpoint)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"} or parts.username or parts.password or parts.query or parts.fragment or parts.path or not parts.port:
            raise TradingError("FAKE_SERVER_LOOPBACK_REQUIRED")
        broker = cls(config, timeout=timeout)
        broker.endpoint, broker._offline = endpoint, True
        return broker

    def _secrets(self):
        key, secret = os.environ.get(self.config.key_env), os.environ.get(self.config.secret_env)
        if not key or not secret:
            raise TradingError("PAPER_CREDENTIALS_NOT_CONFIGURED")
        return key, secret

    def _request(self, method, path, body=None, query=None):
        if not path.startswith("/v2/") or ".." in path or "?" in path:
            raise TradingError("BROKER_PATH_REJECTED")
        if not self._offline and self.endpoint != PAPER_ENDPOINT:
            raise TradingError("LIVE_DISABLED")
        key, secret = self._secrets()
        url = self.endpoint + path + ("?" + urlencode(query) if query else "")
        encoded = None if body is None else canonical(body).encode()
        req = Request(url, data=encoded, method=method, headers={"APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise TradingError("BROKER_RESPONSE_TOO_LARGE")
                return None if not raw else json.loads(raw)
        except HTTPError as exc:
            if exc.code == 404:
                raise TradingError("BROKER_NOT_FOUND") from None
            if exc.code in {401, 403}:
                raise TradingError("BROKER_AUTH_OR_PERMISSION") from None
            if exc.code == 429:
                raise TradingError("BROKER_RATE_LIMIT") from None
            if exc.code in {400, 422}:
                raise TradingError("BROKER_ORDER_REJECTED") from None
            raise TradingError("BROKER_HTTP_UNCERTAIN") from None
        except (TimeoutError, OSError, URLError):
            raise TradingError("BROKER_TRANSPORT_UNCERTAIN") from None
        except (ValueError, UnicodeError):
            raise TradingError("BROKER_PROTOCOL_INVALID") from None

    def account(self):
        result = self._request("GET", "/v2/account")
        if not isinstance(result, dict) or result.get("id") != self.config.broker_account_id:
            raise TradingError("BROKER_ACCOUNT_MISMATCH")
        if result.get("currency") != "USD" or result.get("status") != "ACTIVE" or result.get("trading_blocked") or result.get("account_blocked") or result.get("trade_suspended_by_user"):
            raise TradingError("BROKER_ACCOUNT_BLOCKED")
        if decimal(result["cash"]) < 0:
            raise TradingError("FINANCING_DISABLED")
        return result

    def probe(self):
        account = self.account()
        return {"mode": "BROKER_PAPER", "authentication": "PASSED", "account_binding": "PASSED",
                "currency": account["currency"], "provider_verification": "READ_ONLY_ACCOUNT_ONLY",
                "supported_order_types": list(self.supported_order_types), "live": "DISABLED"}

    def lookup(self, client_order_id):
        identifier(client_order_id)
        try:
            result = self._request("GET", "/v2/orders:by_client_order_id", query={"client_order_id": client_order_id})
        except TradingError as exc:
            if exc.code == "BROKER_NOT_FOUND":
                return None
            raise
        if not isinstance(result, dict) or result.get("client_order_id") != client_order_id:
            raise TradingError("BROKER_PROTOCOL_INVALID")
        return result

    def send_order(self, intent: OrderIntent, ticker, authority):
        self._require_send(intent, authority, "submit")
        self.account()  # bind the credentials to the configured paper account, read-only
        body = {"client_order_id": intent.client_order_id, "symbol": ticker, "side": intent.side,
                "qty": str(intent.quantity), "type": intent.order_type, "time_in_force": "day",
                "extended_hours": False, "order_class": "simple"}
        if intent.limit_price is not None:
            body["limit_price"] = str(intent.limit_price)
        return self._request("POST", "/v2/orders", body)

    def cancel_order(self, intent: OrderIntent, broker_id, authority):
        self._require_send(intent, authority, "cancel")
        identifier(broker_id)
        self.account()
        return self._request("DELETE", "/v2/orders/" + quote(broker_id, safe=""))

    def _require_send(self, intent, authority, action):
        if intent.mode is not RunMode.BROKER_PAPER or intent.account_id != self.config.account_id or authority.account_id != intent.account_id:
            raise TradingError("PAPER_ACCOUNT_MODE_MISMATCH")
        with authority.service.database.transaction() as con:
            authority.require(con)
            row = authority.service._order(con, intent.account_id, intent.client_order_id)
            permits = con.execute("SELECT * FROM trading_outbox WHERE client_order_id=? AND action=? AND status='sending' AND fencing=?",
                                  (intent.client_order_id, action, authority.fencing)).fetchall()
            if row["body"] != canonical(intent) or len(permits) != 1:
                raise TradingError("RISK_OMS_PERMIT_REQUIRED")

    def snapshot(self, after: datetime):
        account = self.account()
        positions = self._request("GET", "/v2/positions")
        orders = self._request("GET", "/v2/orders", query={"status": "open", "limit": 500, "direction": "asc", "nested": "false"})
        if not isinstance(positions, list) or not isinstance(orders, list) or len(orders) >= 500:
            raise TradingError("BROKER_SNAPSHOT_INCOMPLETE")
        fills, token = [], None
        for _ in range(100):
            query = {"after": after.isoformat(), "direction": "asc", "page_size": 100}
            if token:
                query["page_token"] = token
            page = self._request("GET", "/v2/account/activities/FILL", query=query)
            if not isinstance(page, list):
                raise TradingError("BROKER_PROTOCOL_INVALID")
            fills.extend(page)
            if len(page) < 100:
                break
            new_token = page[-1].get("id")
            if not new_token or new_token == token:
                raise TradingError("BROKER_SNAPSHOT_INCOMPLETE")
            token = new_token
        else:
            raise TradingError("BROKER_SNAPSHOT_INCOMPLETE")
        return {"cash": account["cash"], "positions": positions, "open_orders": orders, "fills": fills}

    def stream(self, callback, disconnected, *, stop, socket_factory=None):
        """One connection. Caller reconciles before reconnect, no implicit send/retry."""
        if socket_factory is None:
            from websockets.sync.client import connect
            socket_factory = lambda endpoint: connect(endpoint, open_timeout=self.timeout,
                close_timeout=self.timeout, max_size=2 * 1024 * 1024, proxy=None)
        if self.stream_endpoint != PAPER_STREAM:
            raise TradingError("LIVE_DISABLED")
        key, secret = self._secrets()
        try:
            with socket_factory(self.stream_endpoint) as socket:
                socket.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
                response = json.loads(socket.recv(timeout=self.timeout))
                if response.get("stream") != "authorization" or response.get("data", {}).get("status") != "authorized":
                    raise TradingError("BROKER_STREAM_AUTH")
                socket.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
                response = json.loads(socket.recv(timeout=self.timeout))
                if response.get("stream") != "listening" or response.get("data", {}).get("streams") != ["trade_updates"]:
                    raise TradingError("BROKER_STREAM_SUBSCRIPTION")
                while not stop():
                    try:
                        raw = socket.recv(timeout=self.timeout)
                    except TimeoutError:
                        continue  # idle connection is not a disconnect
                    event = json.loads(raw)
                    if event.get("stream") == "trade_updates":
                        callback(event["data"])
        except Exception:
            disconnected()
            raise TradingError("BROKER_STREAM_DISCONNECTED") from None

"""Read-only pinned-destination HTTP; configuration never supplies credentials."""
from dataclasses import dataclass
import http.client
import ipaddress
import os
import re
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit, unquote, parse_qsl, urlencode

from .contracts import DataError, safe_bytes

HOSTS = {"alpaca": ("data.alpaca.markets",), "sec": ("data.sec.gov",),
         "local": (), "synthetic": (), "nyse": (), "rest": ()}


@dataclass(frozen=True)
class ConnectionConfig:
    provider: str
    endpoint: str | None
    key_env: str | None
    secret_env: str | None
    user_agent_env: str | None
    feed: str | None

    @classmethod
    def from_mapping(cls, value, provider):
        required = {"endpoint", "key_env", "secret_env", "user_agent_env", "feed"}
        if not isinstance(value, dict) or set(value) != required:
            raise DataError("CONNECTION_SCHEMA_INVALID")
        if provider not in HOSTS:
            raise DataError("PROVIDER_UNSUPPORTED")
        for name in ("key_env", "secret_env", "user_agent_env"):
            ref = value[name]
            if ref is not None and (not isinstance(ref, str) or not re.fullmatch(r"FINANCE_PLATFORM_DATA_[A-Z0-9_]{1,80}", ref)):
                raise DataError("SECRET_REFERENCE_INVALID")
        if provider == "alpaca":
            if value["endpoint"] not in (None, "https://data.alpaca.markets") or value["feed"] not in {"iex", "sip"}:
                raise DataError("ALPACA_ENDPOINT_OR_FEED_INVALID")
        elif provider == "sec":
            if value["endpoint"] not in (None, "https://data.sec.gov") or value["key_env"] or value["secret_env"] or value["feed"]:
                raise DataError("SEC_CONNECTION_INVALID")
        elif provider in {"local", "synthetic", "nyse"}:
            if any(v is not None for v in value.values()):
                raise DataError("OFFLINE_CONNECTION_REQUIRED")
        else:
            # Generic REST is non-secret and bound to a server-maintained host/path allowlist.
            if value["key_env"] or value["secret_env"] or value["user_agent_env"] or value["feed"]:
                raise DataError("REST_SECRET_FORBIDDEN")
            validate_url(value["endpoint"], ()) if value["endpoint"] else None
        return cls(provider, **value)

    def missing(self):
        required = ("key_env", "secret_env") if self.provider == "alpaca" else ("user_agent_env",) if self.provider == "sec" else ()
        result = []
        if self.provider in {"alpaca", "sec", "rest"} and not self.endpoint:
            result.append("endpoint")
        for key in required:
            ref = getattr(self, key)
            if not ref or not os.environ.get(ref):
                result.append(key)
        return result

    def headers(self):
        if self.missing():
            raise DataError("CONNECTION_NOT_CONFIGURED")
        headers = {"Accept": "application/json"}
        if self.provider == "alpaca":
            headers.update({"APCA-API-KEY-ID": os.environ[self.key_env], "APCA-API-SECRET-KEY": os.environ[self.secret_env]})
        if self.provider == "sec":
            agent = os.environ[self.user_agent_env]
            if not re.fullmatch(r"[A-Za-z0-9_. -]{1,100} [A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,20}", agent):
                raise DataError("SEC_USER_AGENT_INVALID")
            headers["User-Agent"] = agent
        if any(not isinstance(v, str) or not v or len(v) > 1024 or "\r" in v or "\n" in v for v in headers.values()):
            raise DataError("HEADER_VALUE_INVALID")
        return headers


def validate_url(url, allowed_hosts, *, path_prefixes=("/",), allow_page_token=False):
    if type(allow_page_token) is not bool:
        raise DataError("URL_QUERY_POLICY_INVALID")
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise DataError("URL_INVALID")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise DataError("URL_INVALID") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or port not in {None, 443}:
        raise DataError("HTTPS_DESTINATION_REQUIRED")
    if allowed_hosts and parsed.hostname not in allowed_hosts:
        raise DataError("DESTINATION_NOT_ALLOWED")
    path = unquote(parsed.path or "/")
    if "\\" in path or any(part in {"..", "."} for part in path.split("/")) or "%" in path:
        raise DataError("PATH_INVALID")
    if not any(path == p or (p.endswith("/") and path.startswith(p)) for p in path_prefixes):
        raise DataError("PATH_NOT_ALLOWED")
    for key, value in parse_qsl(parsed.query):
        if allow_page_token and key == "page_token":
            # Installed connector capability, absent from public connection config.
            # Cursor values cannot carry any known process-scoped credential.
            safe_bytes(value.encode("utf-8"), limit=2048)
        elif re.search(r"key|secret|token|password|authorization", key, re.I):
            raise DataError("URL_CREDENTIAL_FORBIDDEN")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        ip = None
    if parsed.hostname in {"localhost", "metadata.google.internal"} or (ip is not None and not ip.is_global):
        raise DataError("SSRF_ADDRESS_FORBIDDEN")
    return parsed


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, *, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


class SafeHTTP:
    """No proxies/redirects, TLS hostname verified, resolved public IP pinned."""
    def __init__(self, allowed_hosts, path_prefixes, *, timeout=10, allow_page_token=False):
        if not allowed_hosts:
            raise DataError("SERVER_ALLOWLIST_REQUIRED")
        self.allowed_hosts, self.path_prefixes = tuple(allowed_hosts), tuple(path_prefixes)
        self.timeout = timeout
        if type(allow_page_token) is not bool:
            raise DataError("URL_QUERY_POLICY_INVALID")
        self.allow_page_token = allow_page_token

    def get(self, url, headers):
        parsed = validate_url(url, self.allowed_hosts, path_prefixes=self.path_prefixes, allow_page_token=self.allow_page_token)
        if any(not isinstance(k, str) or not isinstance(v, str) or "\r" in k+v or "\n" in k+v for k, v in headers.items()):
            raise DataError("HEADER_VALUE_INVALID")
        try:
            addresses = list(dict.fromkeys(i[4][0] for i in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)))
        except OSError:
            raise DataError("TRANSPORT_ERROR") from None
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise DataError("SSRF_DNS_FORBIDDEN")
        try:
            conn = _PinnedHTTPS(parsed.hostname, addresses[0], timeout=self.timeout)
            try:
                conn.request("GET", urlunsplit(("", "", parsed.path or "/", parsed.query, "")), headers=headers)
                r = conn.getresponse()
                if 300 <= r.status < 400:
                    raise DataError("REDIRECT_FORBIDDEN")
                body = r.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise DataError("RESPONSE_LIMIT_EXCEEDED")
                return Response(r.status, {k.lower(): v for k, v in r.getheaders() if k.lower() in
                                {"retry-after", "x-ratelimit-remaining", "x-ratelimit-reset", "content-type"}}, body)
            finally:
                conn.close()
        except (OSError, http.client.HTTPException):
            raise DataError("TRANSPORT_ERROR") from None


def http_error(status):
    return {400: "REQUEST_INVALID", 401: "AUTHENTICATION_FAILED", 403: "PERMISSION_DENIED",
            404: "SOURCE_NOT_FOUND", 429: "RATE_LIMITED"}.get(status, "PROVIDER_UNAVAILABLE" if status >= 500 else "HTTP_ERROR")


class LoopbackHTTP:
    """Explicit test transport, never constructible via public connection config."""
    def __init__(self, port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise DataError("TEST_PORT_INVALID")
        self.port = port

    def get(self, url, headers):
        # Preserve official URL path while routing only to the injected local fake server.
        parsed = urlsplit(url)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            conn.request("GET", urlunsplit(("", "", parsed.path, parsed.query, "")), headers=headers)
            r = conn.getresponse()
            if 300 <= r.status < 400:
                raise DataError("REDIRECT_FORBIDDEN")
            return Response(r.status, dict(r.getheaders()), r.read(16 * 1024 * 1024 + 1))
        finally:
            conn.close()

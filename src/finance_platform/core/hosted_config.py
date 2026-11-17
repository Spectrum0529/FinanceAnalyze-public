"""Validation for the proposed single-host HTTPS account profile."""

from collections import defaultdict
from dataclasses import dataclass
from ipaddress import ip_address
import re


_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DNS_ORIGIN = re.compile(rf"https://(?:{_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?")
_PRIVATE_PATH = re.compile(
    r"^/(?:docs|redoc|openapi\.json|api/v1/(?:health|docs|redoc|openapi\.json|"
    r"auth/(?:initialize|local-session|recover/local)|operations/(?:health|backups|diagnostics|mcp-connection)))"
    r"(?:/|$)",
    re.IGNORECASE,
)
_CREDENTIAL_PATH = re.compile(r"^/api/v1/operations/provider-credentials(?:/|$)", re.IGNORECASE)
_FORWARDED = {b"x-forwarded-for", b"x-forwarded-proto"}
_PROXY_ALIASES = {b"forwarded", b"x-real-ip", b"x-original-url", b"x-rewrite-url"}
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class HostedConfig:
    origin: str

    def __post_init__(self):
        if not _DNS_ORIGIN.fullmatch(self.origin) or len(self.host) > 253:
            raise ValueError("Hosted mode requires one canonical HTTPS DNS origin")

    @property
    def host(self) -> str:
        return self.origin.removeprefix("https://")

    def validate_runtime(self, *, access_mode: str, bind_host: str, trusted_proxy: str,
                         secure_cookies: bool, initialized: bool) -> None:
        if (access_mode != "accounts" or bind_host != "127.0.0.1"
                or trusted_proxy != "127.0.0.1" or not secure_cookies or not initialized):
            raise ValueError("Invalid hosted accounts runtime or missing initial admin")

    def validate_request(self, *, scheme: str, client: str | None, method: str,
                         path: str, headers: list[tuple[bytes, bytes]]) -> str:
        """Return the normalized effective client IP after trusted-proxy handling."""
        if scheme != "https":
            raise ValueError("Trusted HTTPS is required")
        values = defaultdict(list)
        for name, value in headers:
            name = name.lower()
            if name in _PROXY_ALIASES or (name.startswith(b"x-forwarded-") and name not in _FORWARDED):
                raise ValueError("Unexpected proxy header")
            if name in _FORWARDED or name in {b"host", b"origin"}:
                values[name].append(value)

        if values[b"host"] != [self.host.encode("ascii")]:
            raise ValueError("Invalid Host")
        origin = values[b"origin"]
        if (origin or method not in _READ_METHODS) and origin != [self.origin.encode("ascii")]:
            raise ValueError("Invalid Origin")
        if values[b"x-forwarded-proto"] != [b"https"]:
            raise ValueError("Missing or invalid HTTPS proxy assertion")
        addresses = values[b"x-forwarded-for"]
        if len(addresses) != 1 or b"%" in addresses[0]:
            raise ValueError("Expected one proxy client IP")
        client_ip = ip_address(client)
        if client_ip != ip_address(addresses[0].decode("ascii")):
            raise ValueError("Effective client IP does not match the proxy assertion")

        if _PRIVATE_PATH.match(path) or (_CREDENTIAL_PATH.match(path) and method not in _READ_METHODS):
            raise PermissionError("Route is unavailable in hosted mode")
        return str(client_ip)

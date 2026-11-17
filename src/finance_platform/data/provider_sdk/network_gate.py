"""Per-instance SDK gates. No SDK imports or network occur at module import."""
from datetime import date, datetime, time as dtime, timezone
from io import BytesIO
import http.client
import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from finance_platform.data.contracts import DataError
from finance_platform.data.network import SafeHTTP, validate_url, _PinnedHTTPS
from .contracts import LIMITS, SINA_INTERFACE, valid_sina_symbol, checked_json, now
from .http_entity import BoundedEntity
from .history_contracts import HISTORY_PROFILE, SINA_QFQ_INTERFACE, NATIVE_INTERFACE


STAGES = {"DNS", "CONNECT_TLS_OR_REQUEST", "READ_HEADERS", "READ_BODY", "RESPONSE_CHECK", "JSON_CAPTURE", "COMPLETE"}
ENCODINGS = {"NOT_OBSERVED", "identity", "gzip", "br", "deflate", "zstd", "OTHER"}
EXCEPTIONS = {None, "DATA_GUARD", "TIMEOUT", "TLS", "CONNECTION", "DNS", "HTTP_PROTOCOL", "CURL_REQUEST", "OTHER"}
PROTOCOL_SUBTYPES = {None, "REMOTE_DISCONNECTED", "BAD_STATUS_LINE", "LINE_TOO_LONG",
                     "INCOMPLETE_READ", "UNKNOWN_PROTOCOL", "HTTP_EXCEPTION_OTHER"}


def _protocol_subtype(error):
    # Only fixed class categories, no args/status line/partial response text.
    for cls, label in ((http.client.RemoteDisconnected, "REMOTE_DISCONNECTED"),
                       (http.client.BadStatusLine, "BAD_STATUS_LINE"),
                       (http.client.LineTooLong, "LINE_TOO_LONG"),
                       (http.client.IncompleteRead, "INCOMPLETE_READ"),
                       (http.client.UnknownProtocol, "UNKNOWN_PROTOCOL")):
        if isinstance(error, cls):
            return label
    return "HTTP_EXCEPTION_OTHER" if isinstance(error, http.client.HTTPException) else None


def _exception_kind(error):
    # Enum output only: never copy exception text, URL, errno arguments or env.
    if isinstance(error, DataError):
        return "DATA_GUARD"
    name = type(error).__name__
    if name in {"Timeout", "TimeoutError", "ConnectTimeout", "ReadTimeout"}:
        return "TIMEOUT"
    if name in {"SSLError", "CertificateVerifyError", "SSLCertVerificationError"}:
        return "TLS"
    if name in {"DNSError", "gaierror"}:
        return "DNS"
    if name in {"ConnectionError", "ConnectionResetError", "ConnectionRefusedError", "BrokenPipeError"}:
        return "CONNECTION"
    if isinstance(error, http.client.HTTPException):
        return "HTTP_PROTOCOL"
    if name in {"RequestException", "CurlError", "HTTPError"}:
        return "CURL_REQUEST"
    return "OTHER"


def _encoding_kind(value):
    token = value.strip().lower()
    if isinstance(token, bytes):
        token = {b"": "identity", b"identity": "identity", b"gzip": "gzip", b"br": "br",
                 b"deflate": "deflate", b"zstd": "zstd"}.get(token, "OTHER")
    return (token or "identity") if token in ENCODINGS or token == "" else "OTHER"


def _attempt(budget, role, host, path):
    item = {"role": role, "host": host, "path": path, "status": None,
            "content_encoding": "NOT_OBSERVED", "encoded_bytes": 0, "decoded_bytes": 0,
            "stage": "DNS", "exception_type": None, "exception_subtype": None}
    budget.attempts.append(item)
    return item


SINA_SHAPE_ENUMS = {
    "encoding": {"ASCII", "UTF8_BOM_ASCII", "NON_ASCII", "EMPTY"},
    "content_class": {"EMPTY", "HTML_OR_MARKUP", "ASSIGNMENT_PREFIX", "OTHER"},
    "declaration": {"VAR", "NONE", "UNPARSED"},
    "label_binding": {"EXACT_KLC_KL", "EXACT_KLC_K2", "CASE_VARIANT_KNOWN", "SUFFIX_ONLY_UNTRUSTED", "MISMATCH", "UNPARSED"},
    "quote": {"DOUBLE", "SINGLE", "NONE", "UNPARSED"},
    "terminator": {"SEMICOLON", "NONE", "TRAILING_OTHER", "UNPARSED"},
    "payload_class": {"BASE64", "BASE64_PADDED", "EMPTY", "OTHER", "UNPARSED"},
}


def safe_sina_assignment_shape(value, body_bytes):
    """Finite schema only; no labels, payloads, excerpts, hashes or error text."""
    required = set(SINA_SHAPE_ENUMS) | {"schema_version", "bytes", "single_assignment", "literal_has_escapes"}
    if (type(value) is not dict or set(value) != required
            or value.get("schema_version") != "sina-assignment-shape-1"
            or type(value.get("bytes")) is not int or not 0 <= value["bytes"] <= LIMITS["response_bytes"]
            or value["bytes"] != body_bytes
            or any(type(value.get(k)) is not bool for k in ("single_assignment", "literal_has_escapes"))
            or any(type(value.get(k)) is not str or value[k] not in choices for k, choices in SINA_SHAPE_ENUMS.items())):
        return None
    return {k: value[k] for k in sorted(required)}


def safe_transport_summary(attempts, call):
    """Strict parent-side projection of a bounded child manifest; no raw fields."""
    if type(attempts) is not list or type(call) is not dict:
        return []
    symbol = call.get("provider_symbol")
    safe = []
    for item in attempts[:LIMITS["requests"]]:
        if type(item) is not dict:
            continue
        role, host, path = (item.get(k) for k in ("role", "host", "path"))
        if any(type(v) is not str for v in (role, host, path)):
            continue
        if (type(item.get("content_encoding")) is not str or type(item.get("stage")) is not str
                or (item.get("exception_type") is not None and type(item.get("exception_type")) is not str)):
            continue
        if call.get("provider") == "akshare" and call.get("interface") in {SINA_INTERFACE, SINA_QFQ_INTERFACE, NATIVE_INTERFACE}:
            valid = valid_sina_symbol(symbol) and ((role, host, path) == (
                "sina_us_full_history", "finance.sina.com.cn", "/staticdata/us/" + symbol)
                or (call.get("interface") in {SINA_QFQ_INTERFACE, NATIVE_INTERFACE} and (role, host, path) == (
                    "sina_qfq_factors", "finance.sina.com.cn", "/us_stock/company/reinstatement/" + symbol + "_qfq.js")))
        elif call.get('provider')=='openbb_cboe':
            fixed={'/api/global/us_indices/definitions/all_indices.json','/api/global/au_indices/definitions/all-definitions.json','/api/global/us_indices/definitions/GlobalIndices.csv'}
            valid=(host=='cdn.cboe.com' and ((role=='openbb_cboe_reference' and path in fixed) or
                (role=='openbb_cboe_daily' and type(symbol)is str and re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,15}',symbol) and path=='/api/global/delayed_quotes/charts/historical/'+symbol+'.json')))
        elif call.get('provider') in {'akshare_cn','tushare','openbb_nasdaq'}:
            provider=call['provider']
            if provider=='tushare':
                valid=(role,host,path)==('goal_tushare_daily','api.tushare.pro','/')
            elif provider=='akshare_cn' and call.get('operation')=='exchange_list':
                valid=role=='goal_akshare_exchange_list' and (host,path) in {
                    ('query.sse.com.cn','/sseQuery/commonQuery.do'),('www.szse.cn','/api/report/ShowReport')}
            elif provider=='akshare_cn':
                valid=(role,host,path)==('goal_akshare_cn_daily','push2his.eastmoney.com','/api/qt/stock/kline/get')
            else:
                valid=role=='goal_openbb_nasdaq_daily' and host=='api.nasdaq.com' and re.fullmatch(
                    r'/api/quote/[A-Z][A-Z0-9.-]{0,15}/historical',path) is not None
        elif call.get("provider") == "akshare":
            valid = (role, host, path) == ("ak_daily_klines", "63.push2his.eastmoney.com", "/api/qt/stock/kline/get")
        else:
            valid = call.get("provider") == "yfinance" and (
                (role, host, path) == ("cookie_control", "fc.yahoo.com", "/")
                or (call.get("operation") == "search" and (role, host, path) == ("security_search", "query2.finance.yahoo.com", "/v1/finance/search"))
                or (host in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"} and (
                    (role, path) == ("crumb_control", "/v1/test/getcrumb")
                    or (type(symbol) is str and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,17}", symbol)
                        and role in {"metadata_chart", "history_chart"}
                        and path == "/v8/finance/chart/" + symbol))))
        if not valid or item.get("content_encoding") not in ENCODINGS or item.get("stage") not in STAGES or item.get("exception_type") not in EXCEPTIONS:
            continue
        subtype = item.get("exception_subtype")
        if (subtype is not None and type(subtype) is not str) or subtype not in PROTOCOL_SUBTYPES:
            continue
        status = item.get("status")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            continue
        if any(type(item.get(k)) is not int or not 0 <= item[k] <= 2 * LIMITS["unit_bytes"]
               for k in ("encoded_bytes", "decoded_bytes")):
            continue
        safe.append({**{k: item[k] for k in ("role", "host", "path", "status", "content_encoding",
                                           "encoded_bytes", "decoded_bytes", "stage", "exception_type")},
                     "exception_subtype": subtype})
        if call.get("provider") == "akshare" and call.get("interface") == SINA_INTERFACE and item["stage"] == "COMPLETE":
            shape = safe_sina_assignment_shape(item.get("sina_assignment_shape"), item["decoded_bytes"])
            if shape is not None:
                safe[-1]["sina_assignment_shape"] = shape
    return safe


class Budget:
    def __init__(self, call, *, deadline=None):
        self.call = call
        self.deadline = deadline if deadline is not None else time.monotonic()+LIMITS["seconds"]
        self.requests = 0
        self.bytes = 0
        self.encoded_bytes = 0
        self.header_bytes = 0
        self.fatal = None
        self.entities = []
        self.attempts = []
        self.ephemeral_secrets = set()

    def abort(self, code):
        self.fatal = self.fatal or code
        raise DataError(self.fatal)

    def check(self):
        if self.fatal:
            raise DataError(self.fatal)
        if time.monotonic() >= self.deadline:
            self.abort("SDK_TIMEOUT")

    def begin(self):
        self.check()
        self.requests += 1
        if self.requests > LIMITS["requests"]:
            self.abort("SDK_REQUEST_BUDGET_EXCEEDED")

    def consume(self, chunk, so_far):
        self.check()
        if so_far+len(chunk) > LIMITS["response_bytes"] or self.bytes+len(chunk) > LIMITS["unit_bytes"]:
            self.abort("SDK_RESPONSE_LIMIT_EXCEEDED")
        self.bytes += len(chunk)
        return len(chunk)

    def public_addresses(self, host):
        self.check()
        try:
            addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except OSError as error:
            if self.attempts:
                self.attempts[-1]["exception_type"] = _exception_kind(error)
            self.abort("SDK_DNS_FAILED")
        try:
            global_only = addresses and all(ipaddress.ip_address(x).is_global for x in addresses)
        except ValueError:
            global_only = False
        if not global_only:
            self.abort("SDK_DESTINATION_FORBIDDEN")
        self.check()
        return addresses

    def capture(self, url, params, body, *, role, decoding=None):
        value = checked_json(body)
        if any(secret.encode() in body for secret in self.ephemeral_secrets if secret):
            self.abort("SDK_CREDENTIAL_IN_FINANCIAL_BODY")
        if role in {"history_chart", "metadata_chart"}:
            try:
                data = value["chart"]["result"][0]
                meta = data["meta"]
                timestamps = data.get("timestamp", [])
                expected_types = {"EQUITY", "ETF"} if self.call.get("operation") == "identity" else {"ETF" if self.call["asset_class"] == "US_ETF" else "EQUITY"}
                if len(timestamps) > self.call["max_rows"] or meta.get("currency") != "USD" or meta.get("instrumentType") not in expected_types or meta.get("symbol") != self.call["provider_symbol"]:
                    self.abort("PROVIDER_IDENTITY_OR_ROWS_INVALID")
                if role == "history_chart" and self.call.get("profile") == HISTORY_PROFILE:
                    from .security_identity import EXCHANGES
                    binding = self.call["identity_binding"]
                    if meta.get("exchangeName") not in EXCHANGES or EXCHANGES[meta["exchangeName"]][0] != binding["mic"]:
                        self.abort("SECURITY_IDENTITY_CHANGED_REFRESH_REQUIRED")
            except (KeyError, TypeError, IndexError):
                self.abort("PROVIDER_EMPTY_OR_FIELDS_MISSING")
        self.entities.append({"role": role, "origin": urlsplit(url).hostname, "path": urlsplit(url).path,
                              "parameters": {k:v for k,v in params.items() if k != "crumb"},
                              "observed_received_at": now(), "content_decoding": "IDENTITY_ENTITY_NOT_TLS_WIRE",
                              "body": body, **(decoding or {})})


def yahoo_request(budget, method, url, params):
    if method.upper() != "GET":
        budget.abort("SDK_AUTH_FLOW_UNSUPPORTED")
    if type(params) is not dict:
        budget.abort("SDK_INTERFACE_CHANGED")
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        budget.abort("SDK_DESTINATION_FORBIDDEN")
    host, path = parsed.hostname, parsed.path or "/"
    symbol = budget.call["provider_symbol"]
    if budget.call.get("operation") == "search" and host == "query2.finance.yahoo.com" and path == "/v1/finance/search":
        expected = {"q": budget.call["query"], "quotesCount": 10, "enableFuzzyQuery": False, "newsCount": 0,
                    "quotesQueryId": "tss_match_phrase_query", "newsQueryId": "news_cie_vespa", "listsCount": 0,
                    "enableCb": False, "enableNavLinks": False, "enableResearchReports": False,
                    "enableCulturalAssets": False, "recommendedCount": 0}
        if {k: v for k, v in params.items() if k != "crumb"} != expected:
            budget.abort("SDK_INTERFACE_CHANGED")
        allowed, role = set(expected) | {"crumb"}, "security_search"
    elif host == "fc.yahoo.com" and path == "/":
        allowed, role = set(), "cookie_control"
    elif host in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"} and path == "/v1/test/getcrumb":
        allowed, role = set(), "crumb_control"
    elif host in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"} and path == "/v8/finance/chart/"+symbol:
        allowed = {"range", "interval", "period1", "period2", "includePrePost", "events", "crumb"}
        role = "metadata_chart" if "range" in params else "history_chart"
        if params.get("interval") != "1d" or ("range" in params and params["range"] != "1d"):
            budget.abort("SDK_INTERFACE_CHANGED")
        if "range" not in params:
            start = datetime.combine(date.fromisoformat(budget.call["start"]), dtime(), timezone.utc).timestamp()
            end = datetime.combine(date.fromisoformat(budget.call["end"]), dtime(), timezone.utc).timestamp()
            if type(params.get("period1")) is not int or type(params.get("period2")) is not int or not start-86400 <= params["period1"] <= start+86400 or not end-86400 <= params["period2"] <= end+86400:
                budget.abort("SDK_DATE_RANGE_VIOLATION")
    else:
        budget.abort("SDK_AUTH_FLOW_UNSUPPORTED")
    try:
        validate_url(url, (host,), path_prefixes=(path,))
    except DataError:
        budget.abort("SDK_DESTINATION_FORBIDDEN")
    if type(params) is not dict or set(params)-allowed or any(type(v) not in {str, int, bool} or len(str(v)) > 2048 for v in params.values()):
        budget.abort("SDK_INTERFACE_CHANGED")
    if "events" in params and params["events"] != "div,splits,capitalGains":
        budget.abort("SDK_INTERFACE_CHANGED")
    if params.get("includePrePost", False) is not False:
        budget.abort("SDK_INTERFACE_CHANGED")
    return host, path, role


YAHOO_PRIVATE_CA = "cache/yahoo-ca.pem"


def gated_yahoo_session(budget, *, private_ca=False, fixture_exchange=None):
    # Only invoked in the admitted short-lived child, never API/worker import.
    from importlib.metadata import version
    if version("curl_cffi") != "0.17.0":
        raise DataError("SDK_VERSION_UNREVIEWED")
    from curl_cffi import requests as curl_requests, CurlOpt

    class GatedSession(curl_requests.Session):
        def __init__(self):
            super().__init__(impersonate="chrome", trust_env=False, retry=0, verify=True, allow_redirects=False)
            self._bounded_headers = bytearray()

        def _parse_response(self, curl, buffer, header_buffer, default_encoding, discard_cookies):
            # HEADERFUNCTION replaced the original unbounded HEADERDATA buffer.
            return super()._parse_response(curl, buffer, BytesIO(bytes(self._bounded_headers)), default_encoding, discard_cookies)

        def request(self, method, url, **kwargs):
            budget.check()
            if set(kwargs)-{"params", "timeout", "allow_redirects", "headers"}:
                budget.abort("SDK_REQUEST_OVERRIDE_FORBIDDEN")
            params = kwargs.get("params") or {}
            host, path, role = yahoo_request(budget, method, url, params)
            if self.params or self.base_url or self.auth or self.proxy_auth or self.cert or self.verify is not True:
                budget.abort("SDK_REQUEST_OVERRIDE_FORBIDDEN")
            headers = kwargs.get("headers") or {}
            if set(k.lower() for k in headers)-{"accept", "user-agent", "accept-language"} or any(len(str(v)) > 512 or "\r" in str(v) or "\n" in str(v) for v in headers.values()):
                budget.abort("SDK_REQUEST_OVERRIDE_FORBIDDEN")
            budget.begin()
            diagnostic = _attempt(budget, role, host, path)
            addresses = budget.public_addresses(host)
            pinned = addresses[0]
            numeric = "["+pinned+"]" if ":" in pinned else pinned
            entity = BoundedEntity(budget, deadline=min(time.monotonic()+5, budget.deadline),
                                   allow_gzip=role in {"history_chart", "metadata_chart", "security_search"})
            self._bounded_headers.clear()

            def body_callback(chunk):
                diagnostic["stage"] = "READ_BODY"
                try:
                    entity.feed(chunk)
                finally:
                    diagnostic["encoded_bytes"] = entity.encoded
                    diagnostic["decoded_bytes"] = entity.decoded
                return len(chunk)  # Overflow raises sticky fixed error; never return0.

            def header_callback(chunk):
                diagnostic["stage"] = "READ_HEADERS"
                budget.check()
                if len(self._bounded_headers)+len(chunk) > LIMITS["header_bytes"]:
                    budget.abort("SDK_HEADER_LIMIT_EXCEEDED")
                budget.header_bytes += len(chunk)
                if budget.header_bytes > LIMITS["header_bytes"]*LIMITS["requests"]:
                    budget.abort("SDK_HEADER_LIMIT_EXCEEDED")
                if chunk.startswith(b"HTTP/"):
                    parts = chunk[:100].split(b" ", 2)
                    if len(parts) > 1 and len(parts[1]) == 3 and parts[1].isdigit():
                        diagnostic["status"] = int(parts[1])
                if chunk.lower().startswith(b"content-encoding:"):
                    diagnostic["content_encoding"] = _encoding_kind(chunk.split(b":", 1)[1])
                    entity.set_encoding(chunk.split(b":", 1)[1])
                self._bounded_headers.extend(chunk)
                return len(chunk)

            # Tagged0.17.0 retains options after normal settings/impersonation;
            # native callback/override behavior requires fixed-runtime checks.
            self.curl_options = {CurlOpt.RESOLVE: [host+":443:"+numeric], CurlOpt.PROXY: "", CurlOpt.PRE_PROXY: "", CurlOpt.NOPROXY: "*",
                CurlOpt.SSL_VERIFYPEER: 1, CurlOpt.SSL_VERIFYHOST: 2, CurlOpt.FOLLOWLOCATION: 0, CurlOpt.MAXREDIRS: 0,
                CurlOpt.FRESH_CONNECT: 1, CurlOpt.FORBID_REUSE: 1, CurlOpt.CONNECTTIMEOUT_MS: 3000, CurlOpt.TIMEOUT_MS: 5000,
                CurlOpt.ACCEPT_ENCODING: "identity", CurlOpt.HTTP_CONTENT_DECODING: 0, CurlOpt.HEADERFUNCTION: header_callback,
                CurlOpt.VERBOSE: 0, CurlOpt.PROTOCOLS_STR: "https", CurlOpt.REDIR_PROTOCOLS_STR: "https"}
            if private_ca:
                self.curl_options.update({CurlOpt.CAINFO: YAHOO_PRIVATE_CA, CurlOpt.PROXY_CAINFO: YAHOO_PRIVATE_CA})
            try:
                diagnostic["stage"] = "CONNECT_TLS_OR_REQUEST"
                if fixture_exchange is None:
                    response = super().request("GET", url, params=params, timeout=5, allow_redirects=False,
                        max_redirects=0, verify=True, proxy="", stream=False, accept_encoding="identity", headers=headers,
                        content_callback=body_callback)
                else:
                    response = fixture_exchange(self, url, params, body_callback, header_callback)
                budget.check()
                diagnostic["stage"] = "RESPONSE_CHECK"
                diagnostic["status"] = response.status_code
                if 300 <= response.status_code < 400:
                    budget.abort("SDK_REDIRECT_FORBIDDEN")
                primary = getattr(response, "primary_ip", None)
                if primary is None or ipaddress.ip_address(primary) != ipaddress.ip_address(pinned):
                    budget.abort("SDK_DESTINATION_FORBIDDEN")
                final = urlsplit(response.url)
                if (final.scheme, final.hostname, final.path or "/") != ("https", host, path):
                    budget.abort("SDK_DESTINATION_FORBIDDEN")
                cookie_size = sum(len(str(c.name))+len(str(c.value))+len(str(c.domain))+len(str(c.path)) for c in self.cookies.jar)
                if cookie_size > LIMITS["cookie_bytes"]:
                    budget.abort("SDK_COOKIE_LIMIT_EXCEEDED")
                response.content = entity.finish()
                budget.ephemeral_secrets.update(str(c.value) for c in self.cookies.jar if c.value)
                if role == "crumb_control" and response.status_code == 200:
                    try:
                        crumb = response.content.decode("utf-8").strip()
                    except UnicodeError:
                        budget.abort("SDK_AUTH_FLOW_UNSUPPORTED")
                    if not 1 <= len(crumb) <= 2048:
                        budget.abort("SDK_AUTH_FLOW_UNSUPPORTED")
                    budget.ephemeral_secrets.add(crumb)
                if role in {"history_chart", "metadata_chart", "security_search"} and response.status_code == 200:
                    diagnostic["stage"] = "JSON_CAPTURE"
                    budget.capture(url, params, response.content, role=role,
                                   decoding=entity.capture_metadata())
                # fc404 may legitimately set the anonymous cookie; it is not financial success.
                diagnostic["stage"] = "COMPLETE"
                return response
            except DataError as error:
                diagnostic["exception_type"] = _exception_kind(error)
                raise
            except Exception as error:
                diagnostic["exception_type"] = _exception_kind(error)
                diagnostic["exception_subtype"] = _protocol_subtype(error)
                budget.check()
                budget.abort("SDK_TRANSPORT_FAILED")

    return GatedSession()


class _HeaderReader:
    def __init__(self, original, budget):
        self.original, self.budget, self.count = original, budget, 0
    def readline(self, maximum=-1):
        self.budget.check()
        allowance = LIMITS["header_bytes"]-self.count
        chunk = self.original.readline(min(maximum if maximum >= 0 else allowance+1, allowance+1))
        self.count += len(chunk)
        if self.count > LIMITS["header_bytes"]:
            self.budget.abort("SDK_HEADER_LIMIT_EXCEEDED")
        return chunk
    def __getattr__(self, name):
        return getattr(self.original, name)


class BoundedSafeHTTP(SafeHTTP):
    """SDK-only streamed variant; legacy SafeHTTP defaults remain unchanged."""
    def __init__(self, budget, *, fixture_exchange=None):
        super().__init__(("63.push2his.eastmoney.com",), ("/api/qt/stock/kline/get",), timeout=3)
        self.budget, self.fixture_exchange = budget, fixture_exchange

    def fetch(self, url, params, *, json_body=None, role='ak_daily_klines', referer=None, **limits):
        from types import SimpleNamespace
        budget = self.budget
        parsed = validate_url(url, self.allowed_hosts, path_prefixes=self.path_prefixes)
        budget.begin()
        diagnostic = _attempt(budget, role, parsed.hostname, parsed.path)
        pinned = budget.public_addresses(parsed.hostname)[0]
        if self.fixture_exchange is not None:
            # Test-only constructor injection; never configured through source/request JSON.
            raw = self.fixture_exchange(url, params)
            budget.consume(raw, 0)
            return SimpleNamespace(status=200, body=raw)
        class Response(http.client.HTTPResponse):
            def begin(self):
                original = self.fp
                self.fp = _HeaderReader(original, budget)
                try:
                    super().begin()
                finally:
                    self.fp = original
        connection = _PinnedHTTPS(parsed.hostname, pinned, timeout=3)
        connection.response_class = Response
        request_deadline = min(time.monotonic()+5, budget.deadline)
        try:
            diagnostic["stage"] = "CONNECT_TLS_OR_REQUEST"
            headers={"User-Agent":"python-requests/2.34.2","Accept":"*/*",
                     "Connection":"keep-alive","Accept-Encoding":"identity"}
            if referer is not None:
                if (parsed.hostname, parsed.path, role, referer) != ('query.sse.com.cn','/sseQuery/commonQuery.do',
                        'goal_akshare_exchange_list','https://www.sse.com.cn/assortment/stock/list/share/'):
                    budget.abort('SDK_DESTINATION_FORBIDDEN')
                headers['Referer']=referer
            if json_body is not None:
                if (parsed.hostname, parsed.path or "/", role) != ('api.tushare.pro','/','goal_tushare_daily') or params:
                    budget.abort('SDK_DESTINATION_FORBIDDEN')
                if type(json_body) is not bytes or not 0<len(json_body)<=4096:
                    budget.abort('SDK_INPUT_INVALID')
                headers['Content-Type']='application/json'
            connection.request("POST" if json_body is not None else "GET",
                urlunsplit(("", "", parsed.path or "/", urlencode(params), "")),body=json_body,headers=headers)
            connection.sock.settimeout(max(0.01, request_deadline-time.monotonic()))
            diagnostic["stage"] = "READ_HEADERS"
            response = connection.getresponse()
            diagnostic["status"] = response.status
            diagnostic["content_encoding"] = _encoding_kind(response.getheader("Content-Encoding", "identity"))
            if 300 <= response.status < 400:
                budget.abort("SDK_REDIRECT_FORBIDDEN")
            if response.getheader("Transfer-Encoding"):
                budget.abort("SDK_FRAMING_UNSUPPORTED")
            if response.getheader("Content-Encoding", "identity").lower() not in {"identity", ""}:
                budget.abort("SDK_ENCODING_UNSUPPORTED")
            chunks, total = [], 0
            while True:
                diagnostic["stage"] = "READ_BODY"
                budget.check()
                if time.monotonic() >= request_deadline:
                    budget.abort("SDK_TIMEOUT")
                if connection.sock:
                    connection.sock.settimeout(max(0.01, request_deadline-time.monotonic()))
                chunk = response.read1(65536)
                if not chunk:
                    break
                diagnostic["encoded_bytes"] += len(chunk)
                budget.consume(chunk, total)
                total += len(chunk)
                diagnostic["decoded_bytes"] = total
                chunks.append(chunk)
            diagnostic["stage"] = "COMPLETE"
            return SimpleNamespace(status=response.status, body=b"".join(chunks))
        except DataError as error:
            diagnostic["exception_type"] = _exception_kind(error)
            raise
        except (OSError, http.client.HTTPException) as error:
            diagnostic["exception_type"] = _exception_kind(error)
            diagnostic["exception_subtype"] = _protocol_subtype(error)
            budget.abort("SDK_TRANSPORT_FAILED")
        finally:
            connection.close()

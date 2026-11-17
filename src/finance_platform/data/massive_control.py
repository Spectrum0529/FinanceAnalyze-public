"""Persistent conservative quota and resumable units around the official SDK.

No credential values, response bodies, external URLs or user file paths persist.
One local Stocks bucket deliberately covers all owners/credential aliases. Paid
rate uplift needs a separate trusted entitlement design; a UI claim cannot lift it.
"""
from contextlib import closing
from datetime import date, datetime, time as midnight, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import time
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from finance_platform.core.contracts import canonical, fingerprint, identifier
from finance_platform.core.database import immutable
from .contracts import DataError as BaseDataError

CHINESE_ERRORS = {
    'MASSIVE_AUTH_REQUIRED': '密钥未通过验证，请检查本地密钥配置。',
    'MASSIVE_PERMISSION_OR_RANGE': '当前账号无此数据或日期范围权限，请核对套餐、数据类别和起止日期。',
    'MASSIVE_RATE_WAIT': '已达到共享请求额度，进度已保留，请等待后续传。',
    'MASSIVE_REQUEST_BUDGET': '累计请求预算已用完，续传不会重置预算。',
    'MASSIVE_RESUME_BUSY': '上一下载仍在运行或已完成，请刷新任务状态。',
    'MASSIVE_RESUME_MISMATCH': '续传参数与原任务不一致，请使用原下载范围。',
    'MASSIVE_TRANSPORT_FAILED': '数据连接失败，已保存的完成段仍保留。',
    'MASSIVE_HTTP_FAILED': '数据服务未正常返回，请查看任务状态后再处理。',
    'MASSIVE_ENDPOINT_REJECTED': '数据服务目标不符合本地安全限制。',
    'MASSIVE_RESPONSE_INVALID': '数据服务返回了无效或未完整读取的数据页，未发布此次数据集。',
    'MASSIVE_CURSOR_REJECTED': '数据服务返回的下一页链接超出当前请求范围，已停止此次下载。',
    'MASSIVE_RESUME_PAGE_UNVERIFIED': '原下载分段没有可核验的分页记录，请新建下载任务。原记录仍保留。',
    'MASSIVE_PROBE_EMPTY': '所选日期未返回日线，暂不能确认数据权限。',
    'MASSIVE_PROBE_RANGE': '连接检查最多选择七个日历日。',
}


class MassiveError(BaseDataError):
    def __init__(self, code):
        super().__init__(code)
        self.message = CHINESE_ERRORS.get(code, self.message)


DataError = MassiveError
PAGE_VALIDATION = 'massive-http-page-v2'
_AGG_PATH = re.compile(r'/v2/aggs/ticker/([A-Z][A-Z0-9.\-]{0,15})/range/1/day/([^/]+)/([^/]+)')
_DETAIL_PATH = re.compile(r'/v3/reference/tickers/[A-Z][A-Z0-9.\-]{0,15}')
_LIST_PATHS = {'/v3/reference/tickers', '/etf-global/v1/constituents'}
_MAX_PAGE_BYTES = 16 * 1024**2


def _massive_url(url, code):
    if type(url) is not str or not url or len(url) > 8192 or any(ord(c) < 33 or ord(c) > 126 or c == '\\' for c in url):
        raise DataError(code)
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc.lower() not in {'api.massive.com', 'api.massive.com:443'} or '#' in url:
            raise ValueError
    except ValueError:
        raise DataError(code) from None
    return parsed


def _day_millis(value, *, following=False):
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError
    if following:
        day += timedelta(days=1)
    return int(datetime.combine(day, midnight.min, tzinfo=ZoneInfo('America/New_York')).timestamp() * 1000)


def _continuation(url, initial_path, current_path):
    code = 'MASSIVE_CURSOR_REJECTED'
    parsed = _massive_url(url, code)
    try:
        fields = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
        if len(fields) != 1 or fields[0][0] != 'cursor' or not re.fullmatch(r'[A-Za-z0-9_+\-/=]{1,4096}', fields[0][1]):
            raise ValueError
        if parsed.path != current_path:
            original, current, upcoming = (_AGG_PATH.fullmatch(path) for path in (initial_path, current_path, parsed.path))
            if not (original and current and upcoming) or (upcoming[1], upcoming[3]) != (original[1], original[3]):
                raise ValueError
            # The official bars cursor may advance the lower path bound to
            # epoch milliseconds. Keep the same ticker, interval and end date.
            def lower(value):
                return int(value) if re.fullmatch(r'[0-9]{1,16}', value) else _day_millis(value)
            end = _day_millis(original[3], following=True)
            if not lower(original[2]) <= lower(current[2]) <= lower(upcoming[2]) < end:
                raise ValueError
    except (ValueError, OverflowError, OSError):
        raise DataError(code) from None
    return parsed.path, parsed.query


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError


def _requested_adjustment(fields):
    # The official SDK serializes bool query options to "true"/"false".
    # Massive's bars endpoint defaults to adjusted when the option is absent.
    value = (fields or {}).get('adjusted', True)
    if value is True or value == 'true':
        return True
    if value is False or value == 'false':
        return False
    raise DataError('MASSIVE_OPTIONS_INVALID')


def _page_next(response, initial_path, current_path, requested_adjustment):
    """Check the original envelope before the SDK can swallow or rewrite it."""
    invalid = 'MASSIVE_RESPONSE_INVALID'
    try:
        raw = response.data
        if type(raw) is not bytes or not 0 < len(raw) <= _MAX_PAGE_BYTES:
            raise ValueError
        body = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_json_object,
                          parse_constant=_reject_constant)
    except (AttributeError, OSError, ValueError, RecursionError):
        raise DataError(invalid) from None
    aggregate = _AGG_PATH.fullmatch(initial_path)
    statuses = {'OK', 'DELAYED'} if aggregate else {'OK'}
    if type(body) is not dict or type(body.get('status')) is not str or body['status'] not in statuses or body.get('error'):
        raise DataError(invalid)
    if _DETAIL_PATH.fullmatch(initial_path):
        if type(body.get('results')) is not dict or 'next_url' in body:
            raise DataError(invalid)
        return None
    if aggregate:
        if 'ticker' in body and body['ticker'] != aggregate[1]:
            raise DataError(invalid)
        if 'adjusted' in body and body['adjusted'] is not requested_adjustment:
            raise DataError(invalid)
    results = body.get('results')
    count = body.get('resultsCount')
    if 'resultsCount' in body and (type(count) is not int or count < 0):
        raise DataError(invalid)
    if 'results' not in body:
        # The bars endpoint may omit the optional array for a proven empty
        # terminal page. Missing results on other pages can truncate the SDK.
        if not aggregate or count != 0 or 'next_url' in body:
            raise DataError(invalid)
    else:
        if type(results) is not list or any(type(row) is not dict for row in results):
            raise DataError(invalid)
        if count is not None:
            if aggregate and count != len(results):
                raise DataError(invalid)
            if not aggregate and count < len(results):
                raise DataError(invalid)
    if 'next_url' in body:
        return _continuation(body['next_url'], initial_path, current_path)
    return None


def _resume_gaps(con, family):
    return con.execute("""SELECT
        coalesce(sum(json_extract(u.body,'$.row_count')>0 AND s.unit IS NULL),0),
        coalesce(sum(json_extract(u.body,'$.page_validation') IS NOT ?),0)
        FROM massive_units u LEFT JOIN massive_unit_locations s ON s.family=u.family AND s.unit=u.unit
        WHERE u.family=? AND u.unit NOT LIKE 'overview%'""", (PAGE_VALIDATION, family)).fetchone()

MIGRATIONS = ('''
CREATE TABLE massive_families(id TEXT PRIMARY KEY,owner TEXT NOT NULL,request TEXT NOT NULL,hash TEXT NOT NULL,maximum INTEGER NOT NULL);
CREATE TABLE massive_attempts(seq INTEGER PRIMARY KEY,family TEXT NOT NULL REFERENCES massive_families(id),job TEXT NOT NULL UNIQUE);
CREATE TABLE massive_requests(seq INTEGER PRIMARY KEY,family TEXT NOT NULL REFERENCES massive_families(id),job TEXT NOT NULL,recorded REAL NOT NULL);
CREATE INDEX massive_request_time ON massive_requests(recorded);
CREATE TABLE massive_cooldowns(seq INTEGER PRIMARY KEY,until_time REAL NOT NULL);
CREATE TABLE massive_units(family TEXT NOT NULL REFERENCES massive_families(id),unit TEXT NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(family,unit));
''',)


class MassiveControl:
    def __init__(self, database, *, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.db, self.clock, self.monotonic, self.sleep = database, clock, monotonic, sleep
        database.apply_module_migrations('massive_control', MIGRATIONS)
        database.apply_module_migrations('massive_unit_storage_scope',('CREATE TABLE massive_unit_locations(family TEXT NOT NULL,unit TEXT NOT NULL,owner TEXT NOT NULL,location TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(family,unit));',))
        with database.transaction() as con:
            for table, keys in [('massive_families', ('id',)), ('massive_attempts', ('seq',)),
                                ('massive_requests', ('seq',)), ('massive_cooldowns', ('seq',)),
                                ('massive_units', ('family', 'unit'))]:
                immutable(con, table, keys)
            immutable(con,'massive_unit_locations',('family','unit'))

    def begin(self, owner, request, context, *, resume_id=None, maximum=4096):
        identifier(owner)
        if type(maximum) is not int or not 1 <= maximum <= 4096:
            raise DataError('MASSIVE_REQUEST_BUDGET')
        encoded, digest = canonical(request), fingerprint(request)
        if len(encoded.encode()) > 1024 * 1024:
            raise DataError('IMPORT_BYTES_LIMIT')
        context.require_active()
        with self.db.transaction() as con:
            context.assert_fenced(con)
            parent = getattr(context.run, 'parent_run_id', None)
            if parent:
                inherited = con.execute('SELECT a.family FROM massive_attempts a JOIN jobs j ON j.job_id=a.job WHERE j.run_id=? AND j.owner_id=?', (parent, owner)).fetchone()
                if inherited:
                    if resume_id is not None and resume_id != inherited[0]:
                        raise DataError('MASSIVE_RESUME_MISMATCH')
                    resume_id = inherited[0]
            if resume_id is None:
                family = 'massive-' + uuid4().hex
                con.execute('INSERT INTO massive_families VALUES(?,?,?,?,?)', (family, owner, encoded, digest, maximum))
            else:
                identifier(resume_id)
                family = resume_id
                row = con.execute('SELECT request,hash,maximum FROM massive_families WHERE id=? AND owner=?', (family, owner)).fetchone()
                if not row:
                    raise DataError('RESOURCE_NOT_FOUND')
                if fingerprint(json.loads(row[0])) != row[1] or digest != row[1] or maximum != row[2]:
                    raise DataError('MASSIVE_RESUME_MISMATCH')
                last = con.execute('SELECT j.status FROM massive_attempts a JOIN jobs j ON j.job_id=a.job WHERE a.family=? ORDER BY a.seq DESC LIMIT 1', (family,)).fetchone()
                if not last or last[0] not in {'FAILED', 'CANCELLED'}:
                    raise DataError('MASSIVE_RESUME_BUSY')
                unmapped, unverified = _resume_gaps(con, family)
                if unmapped:
                    raise DataError('DOWNLOAD_RESUME_STORAGE_UNBOUND')
                if unverified:
                    raise DataError('MASSIVE_RESUME_PAGE_UNVERIFIED')
            con.execute('INSERT INTO massive_attempts(family,job) VALUES(?,?)', (family, context.job_id))
        return family

    def status(self, owner, job):
        identifier(owner); identifier(job)
        with closing(self.db.connect()) as con:
            row = con.execute('SELECT f.id,f.request,f.maximum,f.hash FROM massive_families f JOIN massive_attempts a ON a.family=f.id WHERE f.owner=? AND a.job=?', (owner, job)).fetchone()
            if not row:
                raise DataError('RESOURCE_NOT_FOUND')
            used = con.execute('SELECT count(*) FROM massive_requests WHERE family=?', (row[0],)).fetchone()[0]
            units = con.execute('SELECT unit FROM massive_units WHERE family=? ORDER BY unit', (row[0],)).fetchall()
            unmapped, unverified = _resume_gaps(con, row[0])
            cooldown = con.execute('SELECT max(until_time) FROM massive_cooldowns').fetchone()[0]
            latest = con.execute('SELECT a.job,j.status FROM massive_attempts a JOIN jobs j ON j.job_id=a.job WHERE a.family=? ORDER BY a.seq DESC LIMIT 1', (row[0],)).fetchone()
            times = [x[0] for x in con.execute('SELECT recorded FROM massive_requests WHERE recorded>? ORDER BY recorded', (self.clock()-60,))]
        request = json.loads(row[1])
        if fingerprint(request) != row[3]:
            raise DataError('DATASET_HASH_MISMATCH')
        daily = 'universe' in request
        remaining = max(0, math.ceil((cooldown or 0) - self.clock()))
        reason = ('NO_DAILY_DOWNLOAD' if not daily else 'STALE_ATTEMPT' if latest[0] != job else
                  'COMPLETED' if latest[1] == 'SUCCEEDED' else
                  'NOT_TERMINAL' if latest[1] not in {'FAILED', 'CANCELLED'} else
                  'STORAGE_SCOPE_UNBOUND' if unmapped else 'PAGE_VALIDATION_UNVERIFIED' if unverified else
                  'BUDGET_EXHAUSTED' if used >= row[2] else 'COOLDOWN_ACTIVE' if remaining else 'READY')
        ready = max(cooldown or 0, times[-5] + 60 if len(times) >= 5 else 0, times[-1] if times else 0)
        return {'job_id': job, 'latest_job_id': latest[0], 'latest_status': latest[1],
                'resume_id': row[0], 'eligible_resume': reason == 'READY', 'resume_reason': reason,
                'original_request': request if daily else None,
                'completed_units': len([x for x in units if not x[0].startswith('overview')]),
                'completed_identity_units': len([x for x in units if x[0].startswith('overview:')]),
                'total_units': len(daily_units(request)) if daily else None,
                'unmapped_storage_units':unmapped,'unverified_page_units':unverified,
                'requests_used': used, 'requests_maximum': row[2],
                'cooldown_until': datetime.fromtimestamp(cooldown, timezone.utc).isoformat() if cooldown else None,
                'cooldown_remaining_seconds': remaining, 'next_request_after_seconds': max(0, math.ceil(ready-self.clock())),
                'rate_policy': {'mode': 'LOCAL_CONSERVATIVE_SHARED_NOT_PLAN_DETECTION', 'requests': 5, 'window_seconds': 60}}

    def unit(self, family, key):
        with closing(self.db.connect()) as con:
            row = con.execute('SELECT body,hash FROM massive_units WHERE family=? AND unit=?', (family, key)).fetchone()
        if not row:
            return None
        result = json.loads(row[0])
        if fingerprint(result) != row[1]:
            raise DataError('DATASET_HASH_MISMATCH')
        return result

    def save_unit(self, family, key, body, context):
        encoded = canonical(body)
        if len(encoded.encode()) > 2 * 1024**2:
            raise DataError('IMPORT_BYTES_LIMIT')
        binding=context.storage_binding
        if binding.get('owner_id')!=context.run.owner_id or binding.get('binding_key')!='job:'+context.job_id:
            raise DataError('STORAGE_BINDING_INVALID')
        location=binding['locations']['market_data']['location_id']
        with self.db.transaction() as con:
            context.assert_fenced(con)
            bound = con.execute('SELECT f.owner FROM massive_families f JOIN massive_attempts a ON a.family=f.id WHERE a.job=? AND f.id=?', (context.job_id, family)).fetchone()
            if not bound or bound[0] != context.run.owner_id:
                raise DataError('RESOURCE_NOT_FOUND')
            prior = con.execute('SELECT hash FROM massive_units WHERE family=? AND unit=?', (family, key)).fetchone()
            if prior:
                if prior[0] != fingerprint(body):
                    raise DataError('DATASET_HASH_MISMATCH')
                return
            con.execute('INSERT INTO massive_units VALUES(?,?,?,?)', (family, key, encoded, fingerprint(body)))
            con.execute('INSERT INTO massive_unit_locations VALUES(?,?,?,?,?)',(family,key,context.run.owner_id,location,fingerprint(body)))

    def unit_scope(self,owner,family,key):
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT s.owner,s.location,s.hash,u.hash FROM massive_unit_locations s JOIN massive_units u ON u.family=s.family AND u.unit=s.unit JOIN massive_families f ON f.id=s.family WHERE f.owner=? AND s.family=? AND s.unit=?',(owner,family,key)).fetchone()
        if not row or row[0]!=owner:raise DataError('DOWNLOAD_RESUME_STORAGE_UNBOUND')
        if row[2]!=row[3]:raise DataError('DATASET_HASH_MISMATCH')
        return {'owner_id':owner,'location_id':row[1]}

    def reserve(self, family, context):
        """Atomic physical-attempt reservation, including errors; never refunded."""
        now = self.clock()
        with self.db.transaction() as con:
            context.assert_fenced(con)
            row = con.execute('SELECT f.maximum,f.owner FROM massive_families f JOIN massive_attempts a ON a.family=f.id WHERE f.id=? AND a.job=?', (family, context.job_id)).fetchone()
            if not row or row[1] != context.run.owner_id:
                raise DataError('RESOURCE_NOT_FOUND')
            used = con.execute('SELECT count(*) FROM massive_requests WHERE family=?', (family,)).fetchone()[0]
            if used >= row[0]:
                raise DataError('MASSIVE_REQUEST_BUDGET')
            latest = con.execute('SELECT max(recorded) FROM massive_requests').fetchone()[0]
            # A backwards wall clock cannot release already spent reservations.
            cutoff = max(now, latest or now) - 60.0
            times = [x[0] for x in con.execute('SELECT recorded FROM massive_requests WHERE recorded>? ORDER BY recorded', (cutoff,))]
            until = con.execute('SELECT max(until_time) FROM massive_cooldowns').fetchone()[0] or 0
            ready = max(until, times[-5] + 60.0 if len(times) >= 5 else 0, latest or 0)
            if ready > now:
                return ready - now
            con.execute('INSERT INTO massive_requests(family,job,recorded) VALUES(?,?,?)', (family, context.job_id, now))
        return 0.0

    def cooldown(self, response):
        """Persist exact future Retry-After; malformed/missing falls back to 60s."""
        raw = response.headers.get('Retry-After', '')
        now = self.clock()
        try:
            delay = float(raw)
            if not math.isfinite(delay) or delay < 0:
                raise ValueError
            until = min(253402214400.0, now + delay)
        except (ValueError, TypeError, OverflowError):
            try:
                when = parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    raise ValueError
                until = min(253402214400.0, max(now, when.timestamp()))
            except (ValueError, TypeError, OverflowError):
                until = now + 60
        # Minimum free-plan window on 429; never retry aggressively on zero.
        until = max(now + 60, until)
        with self.db.transaction() as con:
            con.execute('INSERT INTO massive_cooldowns(until_time) VALUES(?)', (until,))
        return until

    def bind(self, client, family, context, *, wall_seconds=1800, charge_response=None, remaining_bytes=None):
        original = client.client
        control = self
        started = self.monotonic()
        if not 0 < wall_seconds <= 1800:
            raise DataError('MASSIVE_REQUEST_BUDGET')

        def remaining():
            context.require_active()
            return wall_seconds - (control.monotonic() - started)

        def wait(delay):
            if delay >= remaining():
                raise DataError('MASSIVE_RATE_WAIT')
            until = control.monotonic() + delay
            while control.monotonic() < until:
                if remaining() <= 0:
                    raise DataError('MASSIVE_RATE_WAIT')
                control.sleep(min(0.25, until - control.monotonic()))

        class Pool:
            def __init__(self):
                self.initial_path = None
                self.requested_adjustment = None
                self.next_page = None
                self.seen = set()

            def require_complete_aggregates(self):
                if self.initial_path is None or not _AGG_PATH.fullmatch(self.initial_path) or self.next_page is not None:
                    raise DataError('MASSIVE_RESPONSE_INVALID')
                return PAGE_VALIDATION

            def request(self, method, url, **kwargs):
                if method != 'GET':
                    raise DataError('MASSIVE_ENDPOINT_REJECTED')
                parsed = _massive_url(url, 'MASSIVE_ENDPOINT_REJECTED')
                if parsed.query:
                    if (parsed.path, parsed.query) != self.next_page or kwargs.get('fields'):
                        raise DataError('MASSIVE_CURSOR_REJECTED')
                else:
                    if parsed.path not in _LIST_PATHS and not (_DETAIL_PATH.fullmatch(parsed.path) or _AGG_PATH.fullmatch(parsed.path)):
                        raise DataError('MASSIVE_ENDPOINT_REJECTED')
                    self.initial_path = parsed.path
                    self.requested_adjustment = (
                        _requested_adjustment(kwargs.get('fields'))
                        if _AGG_PATH.fullmatch(parsed.path) else None
                    )
                    self.next_page = None
                    self.seen.clear()
                for retry in range(3):
                    if remaining_bytes is not None and remaining_bytes() <= 0:
                        raise DataError('GOAL_DATA_BUDGET_EXCEEDED')
                    if remaining() <= 0:
                        raise DataError('MASSIVE_REQUEST_BUDGET')
                    while True:
                        delay = control.reserve(family, context)
                        if not delay:
                            break
                        wait(delay)
                    # Disable internal retries and redirects per physical call:
                    # every attempted request must pass the shared reservation.
                    from urllib3.util import Timeout
                    left = remaining()
                    if left <= 0:
                        raise DataError('MASSIVE_REQUEST_BUDGET')
                    begin = getattr(context, 'begin_provider_request', None)
                    attempt = begin() if begin is not None else None
                    try:
                        response = original.request(method, url, **{**kwargs, 'retries': False, 'redirect': False,
                            'timeout': Timeout(total=left, connect=min(10, left), read=min(30, left)),
                            **({'preload_content': False} if remaining_bytes is not None else {})})
                        if remaining_bytes is not None:
                            maximum = min(_MAX_PAGE_BYTES, remaining_bytes())
                            payload = bytearray()
                            original_response = response
                            complete = False
                            try:
                                while True:
                                    if remaining() <= 0:
                                        raise DataError('MASSIVE_REQUEST_BUDGET')
                                    chunk = response.read(min(65536, maximum - len(payload) + 1))
                                    if not chunk:
                                        complete = True
                                        break
                                    payload.extend(chunk)
                                    if len(payload) > maximum:
                                        if attempt is not None:
                                            context.finish_provider_request(attempt, 'RESPONSE_RECEIVED')
                                        code = 'MASSIVE_RESPONSE_INVALID' if maximum == _MAX_PAGE_BYTES else 'GOAL_DATA_BUDGET_EXCEEDED'
                                        raise DataError(code)
                                from types import SimpleNamespace
                                response = SimpleNamespace(status=original_response.status,
                                    data=bytes(payload), headers=original_response.headers)
                            finally:
                                if not complete:
                                    original_response.close()
                                original_response.release_conn()
                    except DataError:
                        raise
                    except Exception:
                        context.require_active()
                        raise DataError('MASSIVE_TRANSPORT_FAILED') from None
                    if attempt is not None:
                        context.finish_provider_request(attempt, 'RESPONSE_RECEIVED')
                    context.require_active()
                    status = response.status
                    if charge_response is not None:
                        if type(response.data) is not bytes:raise DataError('MASSIVE_RESPONSE_INVALID')
                        charge_response(response.data)
                    if status == 429:
                        control.cooldown(response)
                        release = getattr(response, 'release_conn', None)
                        if callable(release): release()
                        if retry == 2:
                            raise DataError('MASSIVE_RATE_WAIT')
                        continue
                    if status == 401: raise DataError('MASSIVE_AUTH_REQUIRED')
                    if status in (402, 403): raise DataError('MASSIVE_PERMISSION_OR_RANGE')
                    if 300 <= status < 400: raise DataError('MASSIVE_ENDPOINT_REJECTED')
                    if status != 200: raise DataError('MASSIVE_HTTP_FAILED')
                    next_page = _page_next(response, self.initial_path, parsed.path, self.requested_adjustment)
                    if next_page in self.seen:
                        raise DataError('MASSIVE_CURSOR_REJECTED')
                    if next_page is not None:
                        self.seen.add(next_page)
                    self.next_page = next_page
                    if remaining() <= 0:
                        raise DataError('MASSIVE_REQUEST_BUDGET')
                    return response

            def clear(self):
                clear = getattr(original, 'clear', None)
                if callable(clear): clear()

        client.client = Pool()
        return client


def daily_units(request):
    """One symbol/calendar-year iterator is the resumable atomic unit."""
    from datetime import date
    start, end = date.fromisoformat(request['start']), date.fromisoformat(request['end'])
    if start > end or (end.year - start.year + 1) * len(request['universe']['tickers']) > 2000:
        raise DataError('IMPORT_SERVER_CHUNKS_LIMIT')
    units = []
    for symbol in request['universe']['tickers']:
        for year in range(start.year, end.year + 1):
            low, high = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            units.append((symbol, low.isoformat(), high.isoformat()))
    if len(units) > 2000:
        raise DataError('IMPORT_SERVER_CHUNKS_LIMIT')
    return units

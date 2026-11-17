"""Fixed WRDS endpoint and bounded, read-only CIZ queries over Psycopg."""
import asyncio
from contextlib import aclosing
import ipaddress
import os
import socket
import sys
from uuid import uuid4

from .contracts import DataError


HOST = "wrds-pgdata.wharton.upenn.edu"
PORT = 9737
DATABASE = "wrds"
TARGET = f"postgresql://{HOST}:{PORT}/{DATABASE}"
FETCH_ROWS = 512
MAX_ROWS = 20_000
MAX_IDENTITIES = 64

_SCHEMAS = ("crsp", "crsp_a_stock")
_NAMES_TABLES = ("stksecurityinfohist", "stocknames_v2")
_HISTORY_TABLES = ("dsf_v2", "stkdysecuritydata")
_NAMES = ("permno", "ticker", "secinfostartdt", "secinfoenddt", "primaryexch",
          "securitytype", "securitysubtype")
_HISTORY = ("permno", "dlycaldt", "dlyopen", "dlyhigh", "dlylow", "dlyclose",
            "dlyvol", "dlyprcflg")
_METADATA_SQL = """
SELECT n.nspname, c.relname, a.attname,
       pg_catalog.has_schema_privilege(n.oid, 'USAGE')
           AND pg_catalog.has_table_privilege(c.oid, 'SELECT')
FROM pg_catalog.pg_namespace AS n
JOIN pg_catalog.pg_class AS c ON c.relnamespace = n.oid
JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
WHERE n.nspname = ANY(%s) AND c.relname = ANY(%s)
  AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
  AND a.attname = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
"""


def connection_options(credentials, *, sslrootcert):
    return {
        "host": HOST,
        "port": PORT,
        "dbname": DATABASE,
        "user": credentials.username,
        "password": credentials.password,
        "passfile": os.devnull,
        "sslmode": "verify-full",
        "sslrootcert": sslrootcert,
        "sslcertmode": "disable",
        "gssencmode": "disable",
        "connect_timeout": 20,
        "application_name": "FinanceAnalyze-WRDS",
        "client_encoding": "UTF8",
        "options": (
            "-c default_transaction_read_only=on "
            "-c statement_timeout=20000 -c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=30000 -c search_path=pg_catalog"
        ),
    }


async def _watch(context):
    while True:
        context.require_active()
        await asyncio.sleep(.25)


async def _bounded(context, seconds, operation):
    async with asyncio.TaskGroup() as group:
        observer = group.create_task(_watch(context))
        try:
            async with asyncio.timeout(seconds):
                return await operation()
        except TimeoutError:
            raise DataError("WRDS_TIMEOUT") from None
        finally:
            observer.cancel()


def run_bounded(context, seconds, operation):
    context.require_active()
    policy = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=policy) as runner:
            return runner.run(_bounded(context, seconds, operation))
    except ExceptionGroup as group:
        # There is one operation and one stop observer. Cancellation errors
        # take precedence over the shutdown of the database connection.
        for error in group.exceptions:
            if getattr(error, "code", None) in {"JOB_CANCELLED", "LEASE_LOST"}:
                raise error from None
        first = group.exceptions[0]
        raise first from None


def _db_error(error, *, connecting=False):
    code = error.sqlstate
    if code in {"28P01", "28000"}:
        return DataError("WRDS_AUTHENTICATION_FAILED")
    if code == "42501":
        return DataError("WRDS_ACCESS_DENIED")
    if code in {"42P01", "42703", "42883", "3F000", "42804"}:
        return DataError("WRDS_SCHEMA_UNSUPPORTED")
    if code in {"57014", "55P03"}:
        return DataError("WRDS_TIMEOUT")
    # Pre-session libpq failures commonly have no SQLSTATE. Do not guess
    # whether TLS, Duo, server access policy, or network caused the failure.
    return DataError("WRDS_CONNECTION_FAILED" if connecting or code is None
                     or code.startswith("08") else "WRDS_RESPONSE_INVALID")


class PsycopgTransport:
    async def run(self, credentials, operation):
        try:
            import certifi
            import psycopg
        except (ImportError, OSError):
            raise DataError("WRDS_UNAVAILABLE") from None

        # Explicitly set hostaddr from the official hostname. libpq's ambient
        # PGHOSTADDR cannot redirect the socket; verify-full binds TLS to HOST.
        options = connection_options(credentials, sslrootcert=certifi.where())
        try:
            resolved = await asyncio.get_running_loop().getaddrinfo(
                HOST, PORT, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            addresses = sorted(
                {ipaddress.ip_address(item[4][0]) for item in resolved
                 if ipaddress.ip_address(item[4][0]).is_global},
                key=lambda address: (address.version, int(address)),
            )
            if not addresses:
                raise DataError("WRDS_CONNECTION_FAILED")
            options["hostaddr"] = str(addresses[0])
            connection = await psycopg.AsyncConnection.connect(**options)
        except OSError:
            raise DataError("WRDS_CONNECTION_FAILED") from None
        except psycopg.Error as error:
            raise _db_error(error, connecting=True) from None

        try:
            connection.read_only = True
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            return await operation(_Session(connection))
        except psycopg.Error as error:
            raise _db_error(error) from None
        finally:
            await connection.close()


class _Session:
    def __init__(self, connection):
        self.connection = connection

    async def _rows(self, sql, values, limit):
        async with self.connection.cursor() as cursor:
            await cursor.execute(sql, values)
            return await cursor.fetchmany(limit)

    async def probe(self, *, history):
        tables = [*_NAMES_TABLES, *_HISTORY_TABLES] if history else list(_NAMES_TABLES)
        columns = sorted(set(_NAMES) | (set(_HISTORY) if history else set()))
        rows = await self._rows(_METADATA_SQL, (list(_SCHEMAS), tables, columns),
                                len(_SCHEMAS) * len(tables) * len(columns))
        available, denied = {}, set()
        for schema, table, column, allowed in rows:
            if allowed:
                available.setdefault((schema, table), set()).add(column)
            else:
                denied.add((schema, table))
        for schema in _SCHEMAS:
            for names in _NAMES_TABLES:
                if not set(_NAMES) <= available.get((schema, names), set()):
                    continue
                if not history:
                    return f"{schema}.{names}", None
                for daily in _HISTORY_TABLES:
                    if set(_HISTORY) <= available.get((schema, daily), set()):
                        return f"{schema}.{names}", f"{schema}.{daily}"
        raise DataError("WRDS_ACCESS_DENIED" if denied else "WRDS_SCHEMA_UNSUPPORTED")

    async def lookup(self, table, query, as_of, limit):
        field = "permno" if type(query) is int else "ticker"
        sql = f"""
            SELECT {', '.join(_NAMES)} FROM {table}
            WHERE {field} = %s AND secinfostartdt <= %s AND secinfoenddt >= %s
              AND ((securitytype = 'EQTY' AND securitysubtype = 'COM')
                    OR (securitytype = 'FUND' AND securitysubtype = 'ETF'))
            ORDER BY permno, ticker, secinfostartdt, secinfoenddt LIMIT %s
        """
        return await self._rows(sql, (query, as_of, as_of, limit), limit)

    async def history_identities(self, table, securities, start, end):
        sql = f"""
            SELECT {', '.join(_NAMES)} FROM {table}
            WHERE permno = ANY(%s::integer[]) AND secinfostartdt <= %s AND secinfoenddt >= %s
            ORDER BY permno, secinfostartdt, secinfoenddt, ticker LIMIT %s
        """
        return await self._rows(sql, ([item.permno for item in securities],
                                      end, start, MAX_IDENTITIES + 1), MAX_IDENTITIES + 1)

    async def history_batches(self, table, securities, start, end):
        # ORDINALITY preserves the ticker order expected by DatasetChunks
        # without exporting the identifier columns or querying per symbol.
        sql = f"""
            SELECT {', '.join(f'd.{name}' for name in _HISTORY)} FROM {table} AS d
            JOIN pg_catalog.unnest(%s::integer[]) WITH ORDINALITY AS selected(permno, position)
              ON d.permno = selected.permno
            WHERE d.dlycaldt BETWEEN %s AND %s
            ORDER BY selected.position, d.dlycaldt LIMIT %s
        """
        parameters = ([item.permno for item in securities], start, end, MAX_ROWS + 1)
        cursor = self.connection.cursor(name="fa_wrds_" + uuid4().hex)
        await cursor.execute(sql, parameters)
        try:
            while batch := await cursor.fetchmany(FETCH_ROWS):
                yield batch
        finally:
            # The connection is owned by this operation and always closes.
            # During cancellation, skip an extra wire round trip to CLOSE.
            task = asyncio.current_task()
            if task is None or not task.cancelling():
                await cursor.close()

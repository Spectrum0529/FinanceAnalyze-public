"""Recoverable read-only ingestion; common worker owns job lifecycle and leases."""
from contextlib import closing
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from finance_platform.core.contracts import canonical, fingerprint
from finance_platform.core.errors import PlatformError
from .contracts import DataError, now, safe_bytes
from .network import http_error


def _wait(seconds, context, sleeper=time.sleep):
    remaining = min(max(seconds, 0), 60)
    while remaining > 0:
        context.require_active()
        portion = min(remaining, 0.1)
        sleeper(portion)
        remaining -= portion
    context.require_active()


def retry_delay(response, attempt):
    headers = {k.lower(): v for k, v in response.headers.items()}
    value = headers.get("retry-after")
    if value is not None:
        try:
            seconds = float(value)
            if seconds >= 0:
                return min(seconds, 60)
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                return min(max((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds(), 0), 60)
            except (ValueError, TypeError):
                pass
    return min(2 ** attempt, 30)


class Collector:
    def __init__(self, service):
        self.service = service

    def pages(self, owner_id, collection_id):
        with closing(self.service.db.connect()) as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT body FROM data_collection_pages WHERE collection_id=? AND owner_id=? ORDER BY ordinal", (collection_id, owner_id))]

    def event(self, owner_id, collection_id, kind, code=None):
        with self.service.db.transaction() as conn:
            conn.execute("INSERT INTO data_collection_events(collection_id,owner_id,body,created_at) VALUES(?,?,?,?)",
                         (collection_id, owner_id, canonical({"kind": kind, "code": code}), now()))

    def collect(self, context, owner_id, dataset_id, connector, parameters, *, collection_id,
                max_pages=100, retries=3, transport=None, sleeper=time.sleep):
        from finance_platform.core.contracts import identifier
        identifier(collection_id)
        if type(max_pages) is not int or not 1 <= max_pages <= 10000 or type(retries) is not int or not 0 <= retries <= 5:
            raise DataError("COLLECTION_LIMIT_INVALID")
        dataset = self.service.dataset(owner_id, dataset_id)
        source = self.service.source(owner_id, dataset["source_id"], dataset["source_version"])
        self.service._rights(source, dataset["scope"], "development")
        if connector.config.provider != source["provider"]:
            raise DataError("PROVIDER_BINDING_INVALID")
        # No configured credentials => no transport constructed or calls made.
        if connector.config.missing():
            self.event(owner_id, collection_id, "NOT_CONFIGURED", "CONNECTION_NOT_CONFIGURED")
            raise DataError("CONNECTION_NOT_CONFIGURED")
        headers = connector.config.headers()
        cursor = connector.initial(parameters)
        binding = fingerprint({"dataset_id": dataset_id, "source": source, "parameters": parameters,
                               "schema": connector.source_schema})
        previous = self.pages(owner_id, collection_id)
        for page in previous:
            if page["binding_hash"] != binding:
                raise DataError("COLLECTION_BINDING_CHANGED")
        if previous:
            cursor = previous[-1]["next_cursor"]
        transport = transport or connector.transport()
        tokens = {fingerprint(p["cursor"]) for p in previous}
        try:
            while cursor is not None:
                context.require_active()
                if len(previous) >= max_pages:
                    raise DataError("COLLECTION_PAGE_LIMIT")
                cursor_hash = fingerprint(cursor)
                if cursor_hash in tokens:
                    raise DataError("PAGINATION_CYCLE")
                response = None
                for attempt in range(retries+1):
                    _wait(connector.interval_seconds, context, sleeper)
                    try:
                        response = transport.get(connector.url(cursor), headers)
                    except DataError as exc:
                        if exc.code != "TRANSPORT_ERROR" or attempt == retries:
                            raise
                        self.event(owner_id, collection_id, "RETRY", exc.code)
                        _wait(2 ** attempt, context, sleeper)
                        continue
                    if response.status == 429 or response.status >= 500:
                        if attempt == retries:
                            raise DataError(http_error(response.status))
                        self.event(owner_id, collection_id, "RETRY", http_error(response.status))
                        _wait(retry_delay(response, attempt), context, sleeper)
                        continue
                    if response.status != 200:
                        raise DataError(http_error(response.status))
                    break
                safe_bytes(response.body)
                received_at = now()
                context.require_active()
                raw_ref = self.service.archive(owner_id, source, response.body, parameters={"cursor": cursor},
                                               received_at=received_at, scope=dataset["scope"], schema=connector.source_schema)
                # Archive malformed provider bytes before parsing; retain failure evidence.
                records, next_cursor = connector.parse(response.body, cursor=cursor, dataset=dataset,
                                                       license_id=source["license"]["license_id"], received_at=received_at)
                context.require_active()
                appended = self.service.append_records(owner_id, dataset_id, records, lineage=raw_ref)
                body = {"ordinal": len(previous), "binding_hash": binding, "cursor": cursor, "next_cursor": next_cursor,
                        "raw": raw_ref, "rows": len(records), "appended": appended["appended"]}
                with self.service.db.transaction() as conn:
                    conn.execute("INSERT INTO data_collection_pages VALUES(?,?,?,?,?)", (collection_id, len(previous), owner_id, binding, canonical(body)))
                previous.append(body)
                tokens.add(cursor_hash)
                cursor = next_cursor
                context.checkpoint(min(99, len(previous)), {"collection_id": collection_id, "committed_pages": len(previous)})
            self.event(owner_id, collection_id, "COMPLETED")
            return {"collection_id": collection_id, "pages": len(previous), "rows": sum(p["rows"] for p in previous),
                    "implementation": "IMPLEMENTED", "verification": "LOCAL_VERIFIED" if transport.__class__.__name__ == "LoopbackHTTP" else "NOT_RUN",
                    "provider_response_observed": transport.__class__.__name__ not in {"LoopbackHTTP", "Mock"},
                    "formal_research_approved": False}
        except (DataError, PlatformError) as exc:
            self.event(owner_id, collection_id, "CANCELLED" if getattr(exc, "code", "") == "JOB_CANCELLED" else "FAILED", exc.code)
            raise


def connection_test(connector, dataset, license_id, *, transport=None):
    """One read-only small request. Coverage/rights remain distinct and unverified."""
    result = {"configuration": "CONFIGURED", "authentication": "NOT_TESTED", "permission": "NOT_TESTED",
              "fields": "NOT_TESTED", "historical_coverage": "UNVERIFIED", "license": "UNVERIFIED",
              "mode": "READ_ONLY_DATA", "requests": 0}
    missing = connector.config.missing()
    if missing:
        return {**result, "configuration": "NOT_CONFIGURED", "missing": missing}
    if connector.config.provider == "alpaca":
        from datetime import timedelta
        current = datetime.now(timezone.utc) - timedelta(days=7)
        symbols = [dataset["identities"][0]["ticker"]] if dataset["identities"] else []
        if not symbols:
            return {**result, "fields": "IDENTITY_REQUIRED"}
        parameters = {"symbols": symbols, "start": (current - timedelta(days=1)).isoformat(), "end": current.isoformat(), "timeframe": "1Day", "limit": 1}
    elif connector.config.provider == "sec":
        parameters = {"cik": "0000320193", "kind": "submissions"}
    elif connector.config.provider == "rest":
        parameters = {"query": {"limit": 1}}
    else:
        raise DataError("CONNECTION_TEST_UNSUPPORTED")
    cursor = connector.initial(parameters)
    try:
        result["requests"] = 1
        response = (transport or connector.transport()).get(connector.url(cursor), connector.config.headers())
        if response.status != 200:
            code = http_error(response.status)
            result.update(authentication=code if response.status == 401 else "NOT_ESTABLISHED",
                          permission=code, error=code)
            return result
        result.update(authentication="ACCEPTED" if connector.config.provider == "alpaca" else "NOT_REQUIRED", permission="ENDPOINT_READ_OBSERVED")
        records, _ = connector.parse(response.body, cursor=cursor, dataset=dataset, license_id=license_id, received_at=now())
        result["fields"] = "VALIDATED_NONEMPTY" if records else "EMPTY_NOT_VALIDATED"
        return result
    except DataError as exc:
        return {**result, "error": exc.code, "fields": "FAILED"}

"""Bounded NYSE public sample download with immutable response chunks and resume.

This node-local spool is not the general raw store planned for S02.04. Only the
official Closing Prices sample endpoint is supported; no account credentials.
"""
from dataclasses import dataclass
from datetime import date
import hashlib
from http.client import IncompleteRead
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from predictor.access import AccessPolicy, AccessError
from predictor.logging import utc_timestamp

BASE = "https://ftp.nyse.com/Historical%20Data%20Samples/TAQ%20CLOSING%20PRICES/"
SOURCE = "nyse_taq_closing_prices"
SCHEMA = "NYSE_Closing_Prices_v2.3"


class AcquisitionError(ValueError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _target(policy, directory):
    resolved = policy.authorize(directory)
    if not resolved.is_relative_to(policy.partitions["development"]):
        raise AccessError("Acquisition output must be in the development partition")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _immutable(policy, path, body):
    policy.authorize(path)
    try:
        with path.open("xb") as stream:
            stream.write(body)
    except FileExistsError:
        if policy.read_bytes(path) != body:
            raise AcquisitionError("Immutable capture conflict; old response preserved") from None


def http_transport(url, headers, *, max_bytes, timeout):
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            if response.url != url:
                raise AcquisitionError("Unexpected sample endpoint redirect")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise AcquisitionError("Sample response exceeds byte limit")
            return Response(response.status, {k.lower(): v for k, v in response.headers.items()}, body)
    except HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, b"")
    except (TimeoutError, URLError, OSError, IncompleteRead):
        raise TimeoutError("Public sample transport failed") from None


def _parts(policy, directory, context):
    parts = []
    offset = 0
    validator = None
    total = None
    for marker in sorted(directory.glob("part-*.json")):
        part = json.loads(policy.read_bytes(marker))
        if set(part) != {"offset", "size", "total", "etag", "body_hash", "received_at_utc", "status", "response_headers", "request_headers"}:
            raise AcquisitionError("Invalid capture checkpoint")
        if part["offset"] != offset or type(part["size"]) is not int or part["size"] <= 0:
            raise AcquisitionError("Noncontiguous capture checkpoint")
        if marker.name != f"part-{offset:012d}.json" or not re.fullmatch(r"[0-9a-f]{64}", part["body_hash"]):
            raise AcquisitionError("Invalid capture checkpoint identity")
        raw = policy.read_bytes(directory / (part["body_hash"] + ".bin"))
        if sha(raw) != part["body_hash"] or len(raw) != part["size"]:
            raise AcquisitionError("Captured response hash mismatch")
        if parts and (part["etag"] != validator or part["total"] != total):
            raise AcquisitionError("Source version changed between captured ranges")
        validator, total = part["etag"], part["total"]
        if type(total) is not int or not 0 < total <= context["max_bytes"] or offset + len(raw) > total:
            raise AcquisitionError("Invalid response total")
        offset += len(raw)
        parts.append((part, raw))
    return parts, offset, total, validator


def download_sample(policy: AccessPolicy, directory, session, *, scope="public_provider_sample",
                    chunk_size=262144, max_bytes=2097152, max_attempts=3, min_interval=1.0,
                    transport=http_transport, sleep=time.sleep, clock=time.monotonic):
    if type(session) is not date or scope not in {"public_provider_sample", "synthetic_only"}:
        raise AcquisitionError("Explicit session date and sample scope required")
    if any(type(v) is not int or v <= 0 for v in (chunk_size, max_bytes, max_attempts)) or chunk_size > max_bytes:
        raise AcquisitionError("Positive bounded acquisition limits required")
    if max_bytes > 2097152 or max_attempts > 5 or (scope == "synthetic_only" and transport is http_transport):
        raise AcquisitionError("Acquisition scope or hard resource limit rejected")
    if not isinstance(min_interval, (int, float)) or isinstance(min_interval, bool) or not 1 <= min_interval <= 60:
        raise AcquisitionError("Request interval must be positive and bounded")
    directory = _target(policy, directory)
    filename = "EQY_US_NYSE_SUM_CLOSE_" + session.strftime("%Y%m%d") + ".txt"
    context = {"source_id": SOURCE, "schema_version": SCHEMA, "source_url": BASE + filename,
               "session": session.isoformat(), "transport_exchange": "XNYS", "scope": scope,
               "chunk_size": chunk_size, "max_bytes": max_bytes}
    _immutable(policy, directory / "request.json", json_bytes(context))
    parts, offset, total, validator = _parts(policy, directory, context)
    if parts and offset < total and (not validator or validator.startswith("W/")):
        raise AcquisitionError("Resume requires a strong source ETag")
    last_request = clock()  # Also rate-limit the first request after process restart.
    while total is None or offset < total:
        headers = {"User-Agent": "FinanceAnalyze public sample evaluation", "Accept-Encoding": "identity",
                   "Range": f"bytes={offset}-{offset + chunk_size - 1}"}
        if offset:
            headers["If-Range"] = validator
        response = None
        for attempt in range(max_attempts):
            sleep(max(0, min_interval - (clock() - last_request)))
            last_request = clock()
            event = {"offset": offset, "attempt": attempt + 1, "requested_at_utc": utc_timestamp()}
            try:
                response = transport(context["source_url"], headers, max_bytes=max_bytes, timeout=15)
                event.update(status=response.status, received_at_utc=utc_timestamp())
                transient = response.status in {408, 429, 500, 502, 503, 504}
                if response.status not in {200, 206} and not transient:
                    raise AcquisitionError("Public sample HTTP response rejected")
                event["outcome"] = "retryable_http" if transient else "received"
            except TimeoutError:
                event.update(status=None, outcome="timeout", received_at_utc=utc_timestamp())
                transient = True
                response = None
            except Exception:
                event.update(outcome="failed", received_at_utc=utc_timestamp())
                _immutable(policy, directory / ("attempt-" + uuid4().hex + ".json"), json_bytes(event))
                raise
            _immutable(policy, directory / ("attempt-" + uuid4().hex + ".json"), json_bytes(event))
            if not transient:
                break
            if attempt + 1 == max_attempts:
                raise AcquisitionError("Public sample retry budget exhausted")
            delay = min(2 ** attempt, 30)
            retry_after = response.headers.get("retry-after") if response else None
            if retry_after:
                if not retry_after.isdigit() or int(retry_after) > 60:
                    raise AcquisitionError("Retry-After requires a later explicit retry")
                delay = max(delay, int(retry_after))
            sleep(delay)
        if not isinstance(response.body, bytes) or not response.body or len(response.body) > max_bytes:
            raise AcquisitionError("Empty or oversized response rejected")
        body = response.body
        # Save every received body before semantic acceptance; invalid responses
        # remain available for diagnosis but never become a successful checkpoint.
        body_hash = sha(body)
        _immutable(policy, directory / (body_hash + ".bin"), body)
        length = response.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) != len(body)):
            raise AcquisitionError("Response Content-Length mismatch")
        etag = response.headers.get("etag")
        if response.status == 200:
            if offset:
                raise AcquisitionError("Server ignored resume range; old chunks retained")
            response_total = len(body)
        else:
            bounds = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
            if not bounds:
                raise AcquisitionError("Content-Range required for partial response")
            first, last, response_total = map(int, bounds.groups())
            if first != offset or last - first + 1 != len(body) or last >= response_total or len(body) > chunk_size:
                raise AcquisitionError("Repeated or invalid response range rejected")
        if response_total > max_bytes or (total is not None and (response_total != total or etag != validator)):
            raise AcquisitionError("Source version or size changed during resume")
        if offset + len(body) < response_total and (not etag or etag.startswith("W/")):
            raise AcquisitionError("Partial capture requires a strong source ETag")
        part = {"offset": offset, "size": len(body), "total": response_total, "etag": etag,
                "body_hash": body_hash, "received_at_utc": event["received_at_utc"], "status": response.status,
                "request_headers": headers, "response_headers": {k: response.headers[k] for k in (
                    "content-range", "content-length", "etag", "last-modified", "content-type") if k in response.headers}}
        _immutable(policy, directory / f"part-{offset:012d}.json", json_bytes(part))
        offset += len(body)
        total, validator = response_total, etag
        parts.append((part, body))
    raw = b"".join(body for _, body in parts)
    _immutable(policy, directory / "response.txt", raw)
    manifest = {**context, "raw_response_hash": sha(raw), "byte_count": len(raw), "part_count": len(parts),
                "source_etag": validator, "response_received_at": parts[-1][0]["received_at_utc"],
                "source_publication_at": None, "historical_first_available_at": None,
                "license_status": "PUBLIC_SAMPLE_EVALUATION_ONLY_BROADER_RIGHTS_UNVERIFIED"}
    _immutable(policy, directory / "complete.json", json_bytes(manifest))
    return manifest


def replay_response(policy, directory):
    directory = policy.authorize(directory)
    context = json.loads(policy.read_bytes(directory / "request.json"))
    manifest = json.loads(policy.read_bytes(directory / "complete.json"))
    raw = policy.read_bytes(directory / "response.txt")
    parts, offset, total, etag = _parts(policy, directory, context)
    if (not parts or offset != total or b"".join(body for _, body in parts) != raw
            or sha(raw) != manifest["raw_response_hash"] or len(raw) != manifest["byte_count"]
            or len(parts) != manifest["part_count"] or etag != manifest["source_etag"]
            or any(manifest.get(k) != value for k, value in context.items())):
        raise AcquisitionError("Offline response replay integrity failed")
    return raw, manifest

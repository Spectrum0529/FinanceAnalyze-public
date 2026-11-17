"""Unified catalog and fenced publication around existing fixed data services."""
from contextlib import closing
import json
import re

from finance_platform.core.auth import AccountDelegation
from finance_platform.core.contracts import fingerprint, identifier, parse_instant
from finance_platform.core.errors import PlatformError
from finance_platform.core.mcp_data_policy import require_dataset
from .contracts import DataError, validate_paging
from .market_lifecycle import checked_name, read_connection, revision_check
from .market_lineage import (REF_FIELDS, history_metadata, history_store, inclusive,
                             load_history, load_library, project, read_native_references)
from .market_merge import POLICIES, merge_rows
from .provider_sdk.history_contracts import QUANTITY_BASIS
from .simple_datasets import _CATALOG_SCOPE
from .simple_inputs import resolve_universe

_MAX_LINEAGE_REFERENCES = 64
_QUERY_KIND = ("dataset", "benchmark")
_DATE_FIELDS = ("start", "end")


def reference(value):
    if (type(value) is not dict or set(value) != set(REF_FIELDS) or value["source"] not in {"library", "history"}
            or type(value["version"]) is not int or not 1 <= value["version"] <= 2_147_483_647
            or type(value["content_hash"]) is not str or not re.fullmatch("[0-9a-f]{64}", value["content_hash"])):
        raise DataError("MARKET_REFERENCE_INVALID")
    identifier(value["id"])
    return {key: value[key] for key in REF_FIELDS}


def _ref(source, record):
    return {"source": source, "id": record["dataset_id" if source == "library" else "history_id"],
            "version": record["version"], "content_hash": record["content_hash"]}


def approval(principal):
    return principal.check_approval if isinstance(principal, AccountDelegation) else None


def _prices_profile(profile):
    return {key: profile[key] for key in ("currency", "adjustment", "timezone", "price_unit", "volume_unit")}


class MarketManagement:
    def __init__(self, library):
        self.library = library
        self.db = library.db

    def _require_access(self, principal, record):
        if not isinstance(principal, AccountDelegation):
            return
        owner = principal.user_id
        checked = set()
        store = None
        stack = [record]
        while stack:
            current = stack.pop()
            require_dataset(current)
            provenance = current.get("market_provenance") or {}
            refs = list(provenance.get("inputs", ()))
            if provenance.get("verification_reference") is not None:
                refs.append(provenance["verification_reference"])
            for raw in refs:
                ref = reference(raw)
                key = tuple(ref.values())
                if key in checked:
                    continue
                checked.add(key)
                if len(checked) > _MAX_LINEAGE_REFERENCES:
                    raise DataError("MARKET_LINEAGE_LIMIT")
                if ref["source"] == "library":
                    upstream = self.library.get(owner, ref["id"])
                    if _ref("library", upstream) != ref:
                        raise DataError("DATASET_REFERENCE_MISMATCH")
                    stack.append(upstream)
                else:
                    store = store or history_store(self.library)
                    upstream = store.summary(owner, ref["id"], ref["version"], include_deleted=True)
                    if _ref("history", upstream) != ref:
                        raise DataError("DATASET_REFERENCE_MISMATCH")
                    history_metadata(upstream)

    def _item(self, principal, source, record, created, *, con=None):
        owner = principal.user_id
        if source == "library":
            self._require_access(principal, record)
            provider = record["source"]["provider"]
            meta = record["metadata"]
            eligible = (record["scope"] in {"synthetic", "public"} and record["kind"] == "dataset" and (
                provider == "massive" and meta["adjustment"] == "raw"
                or bool(record.get("row_storage", {}).get("acquisition"))
                or bool(record.get("market_provenance"))))
            return {"reference": _ref(source, record), "name": record["name"], "kind": record["kind"],
                    "provider": provider, "tickers": record["universe"]["tickers"],
                    "start": record["start"], "end": record["end"], "coverage": record["quality"],
                    "metadata": meta, "trashed": record["trashed"], "revision": record["revision"],
                    "reference_count": len(record["referenced_by"]), "created_at": created,
                    "capabilities": {"reuse": not record["trashed"], "merge": eligible and not record["trashed"]}}

        try:
            meta = history_metadata(record)
        except DataError:
            if isinstance(principal, AccountDelegation):
                raise PlatformError("MCP_LOCAL_DATA_ONLY", 403) from None
            meta = None
        counts = con.execute(
            """SELECT count(*) FROM data_history_pins WHERE owner_id=? AND history_id=?""",
            (owner, record["history_id"]),
        ).fetchone()[0]
        return {"reference": _ref(source, record), "name": record["name"], "kind": "dataset",
                "provider": record["provider"], "tickers": [record["ticker"]],
                "start": record["requested_range"]["start"], "end": inclusive(record["requested_range"]["end"]),
                "coverage": record["actual_coverage"], "metadata": meta,
                "trashed": record["deleted"], "revision": record["revision"],
                "reference_count": counts, "created_at": created,
                "capabilities": {"reuse": meta is not None and not record["deleted"],
                                 "merge": meta is not None and not record["deleted"]}}

    def list(self, principal, *, q="", source="all", state="active", kind="all", provider=None, limit=50, offset=0):
        validate_paging(limit, offset)
        if (source not in {"all", "library", "history"} or state not in {"active", "trash", "all"}
                or kind not in {"all", *_QUERY_KIND} or type(q) is not str or len(q) > 120):
            raise DataError("INVALID_INPUT")
        if provider is not None and (type(provider) is not str or not re.fullmatch("[a-z_]{1,32}", provider)):
            raise DataError("INVALID_INPUT")
        needle = q.casefold().strip()
        owner = principal.user_id
        query, values = [], []
        states = (0, 1) if state == "all" else (int(state == "trash"),)
        if source != "history":
            for current_kind in _QUERY_KIND if kind == "all" else (kind,):
                for deleted in states:
                    query.append("SELECT 'library' AS source,v.id AS id,v.created AS created " + _CATALOG_SCOPE)
                    values.extend((owner, current_kind, deleted))
        have_history = source != "library" and kind != "benchmark"
        store = history_store(self.library) if have_history else None
        if have_history:
            for deleted in states:
                query.append("""SELECT 'history',s.history_id,s.created_at FROM data_history_series s
                            WHERE s.owner_id=? AND coalesce((SELECT e.deleted FROM data_history_events e
                              WHERE e.history_id=s.history_id AND e.owner_id=s.owner_id ORDER BY e.revision DESC LIMIT 1),0)=?""")
                values.extend((owner, deleted))
        if not query:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        items, total = [], 0
        with closing(self.db.connect()) as con:
            con.execute("BEGIN")
            checks = approval(principal)
            if checks:
                checks(con)
            for origin, ident, created in con.execute(" UNION ALL ".join(query) + " ORDER BY created DESC,id", values):
                record = (self.library._get(con, owner, ident) if origin == "library" else
                          store._summary(con, owner, ident, include_deleted=True))
                try:
                    item = self._item(principal, origin, record, created, con=con)
                except PlatformError as error:
                    if isinstance(principal, AccountDelegation) and error.code in {
                            "MCP_LOCAL_DATA_ONLY", "RESOURCE_NOT_FOUND", "DATASET_REFERENCE_MISMATCH",
                            "MARKET_LINEAGE_LIMIT", "MARKET_MERGE_INCOMPATIBLE"}:
                        continue
                    raise
                if provider is not None and item["provider"] != provider:
                    continue
                if needle and needle not in (" ".join([item["name"], item["provider"], *item["tickers"]])).casefold():
                    continue
                if offset <= total < offset + limit:
                    items.append(item)
                total += 1
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def detail(self, principal, source, ident, *, version=None, include_trash=False,
               version_limit=20, version_offset=0):
        identifier(ident)
        validate_paging(version_limit, version_offset)
        owner = principal.user_id
        if (source not in {"library", "history"} or type(include_trash) is not bool
                or version is not None and (type(version) is not int or version < 1)):
            raise DataError("INVALID_INPUT")
        store = history_store(self.library) if source == "history" else None
        with closing(self.db.connect()) as con:
            con.execute("BEGIN")
            checks = approval(principal)
            if checks:
                checks(con)
            if source == "library":
                record = self.library._get(con, owner, ident, selectable=not include_trash)
                created = con.execute("SELECT created FROM data_library_versions WHERE id=? AND owner=?",
                                      (ident, owner)).fetchone()[0]
                versions, total = [record], 1
            else:
                record = store._summary(con, owner, ident, version, include_deleted=include_trash)
                created = con.execute("SELECT created_at FROM data_history_series WHERE history_id=? AND owner_id=?",
                                      (ident, owner)).fetchone()[0]
                args = (ident, owner)
                total = con.execute("SELECT count(*) FROM data_history_versions WHERE history_id=? AND owner_id=?",
                                    args).fetchone()[0]
                rows = con.execute("""SELECT version FROM data_history_versions WHERE history_id=? AND owner_id=?
                                    ORDER BY version DESC LIMIT ? OFFSET ?""",
                                   (*args, version_limit, version_offset)).fetchall()
                versions = [store._summary(con, owner, ident, row[0], include_deleted=include_trash) for row in rows]
            if version is not None and record["version"] != version:
                raise DataError("RESOURCE_NOT_FOUND")
            try:
                result = self._item(principal, source, record, created, con=con)
            except PlatformError as error:
                if isinstance(principal, AccountDelegation) and error.code == "MCP_LOCAL_DATA_ONLY":
                    raise DataError("RESOURCE_NOT_FOUND") from None
                raise
        if source == "library" and version_offset:
            versions = []
        result["versions"] = {"items": [_ref(source, row) for row in versions], "total": total,
                              "limit": version_limit, "offset": version_offset}
        return result

    def lifecycle(self, principal, source, ident, request):
        expected_keys = {"action", "expected_revision", "idempotency_key"}
        if (type(request) is not dict or not expected_keys <= request.keys()
                or set(request) - expected_keys - {"name"}
                or request["action"] not in {"rename", "trash", "restore"}
                or ("name" in request) != (request["action"] == "rename")):
            raise DataError("INVALID_INPUT")
        action = request["action"]
        current = self.detail(principal, source, ident, include_trash=True)
        owner, guard = principal.user_id, approval(principal)
        if source == "history":
            result = history_store(self.library).lifecycle(
                owner, ident, action="delete" if action == "trash" else action,
                expected_revision=request["expected_revision"], idempotency_key=request["idempotency_key"],
                name=request.get("name"), publication_guard=guard,
            )
            return {"source": source, "id": ident, "name": result["name"], "trashed": result["deleted"],
                    "revision": result["revision"], "physical_delete": False}

        def change(con):
            row = con.execute("""SELECT trashed,revision FROM data_library_lifecycle WHERE id=?
                              ORDER BY revision DESC LIMIT 1""", (ident,)).fetchone()
            revision_check(request["expected_revision"], row[1])
            name = checked_name(request["name"], 120) if action == "rename" else current["name"]
            trashed = action == "trash" or action == "rename" and bool(row[0])
            revision = row[1] + 1
            con.execute("INSERT INTO data_library_lifecycle VALUES(?,?,?)", (ident, revision, int(trashed)))
            if action == "rename":
                con.execute("INSERT INTO data_library_display_names VALUES(?,?,?)", (ident, revision, name))
            return {"source": source, "id": ident, "name": name, "trashed": trashed,
                    "revision": revision, "physical_delete": False}

        return self.library._write(owner, request["idempotency_key"], {
            "operation": "market-lifecycle", "source": source, "id": ident, "action": action,
            "expected_revision": request["expected_revision"], "name": request.get("name"),
        }, change, publication_guard=guard)

    def legacy_set_trash(self, principal, ident, trashed, key):
        owner = principal.user_id
        guard = approval(principal)
        if guard is not None:
            # The complete lineage and its scope are immutable. The current
            # grant is checked separately in the write and replay transaction.
            try:
                self.detail(principal, "library", ident, include_trash=True)
            except DataError as error:
                if error.code == "RESOURCE_NOT_FOUND":
                    raise PlatformError("NOT_FOUND", 404) from None
                raise
        return self.library.set_trash(owner, ident, trashed, key, publication_guard=guard)

    def _resolve(self, principal, ref):
        if ref["source"] == "library":
            record = self.library.get(principal.user_id, ref["id"], selectable=True)
            try:
                self._require_access(principal, record)
            except PlatformError as error:
                if error.code == "MCP_LOCAL_DATA_ONLY":
                    raise DataError("RESOURCE_NOT_FOUND") from None
                raise
            store = None
        else:
            store = history_store(self.library)
            record = store.summary(principal.user_id, ref["id"], ref["version"])
            try:
                history_metadata(record)
            except DataError:
                if isinstance(principal, AccountDelegation):
                    raise DataError("RESOURCE_NOT_FOUND") from None
                raise
        if _ref(ref["source"], record) != ref:
            raise DataError("DATASET_REFERENCE_MISMATCH")
        return store, record

    def _load(self, principal, ref, resolved):
        store, record = resolved
        if store is not None:
            return load_history(store, principal.user_id, ref, record)
        return load_library(self.library, principal.user_id, ref, record)

    @staticmethod
    def _merge_request(request, *, publish):
        required = {"inputs"} | ({"preview_hash", "name", "idempotency_key"} if publish else set())
        optional = {"verification_reference", "conflict_policy"} | ({"connection_id"} if publish else set())
        if (type(request) is not dict or not required <= request.keys() or not request.keys() <= required | optional
                or type(request["inputs"]) is not list or len(request["inputs"]) != 2):
            raise DataError("MARKET_MERGE_INPUT_INVALID")
        refs = [reference(ref) for ref in request["inputs"]]
        if refs[0] == refs[1]:
            raise DataError("MARKET_MERGE_INPUT_INVALID")
        verification = request.get("verification_reference")
        if verification is not None:
            verification = reference(verification)
        policy = request.get("conflict_policy", "reject")
        if type(policy) is not str or policy not in POLICIES:
            raise DataError("MARKET_MERGE_POLICY_INVALID")
        return refs, verification, policy

    def _plan(self, principal, refs, verification_ref, policy):
        references = [*refs, verification_ref] if verification_ref is not None else refs
        resolved = [self._resolve(principal, ref) for ref in references]
        frames = [self._load(principal, ref, record) for ref, record in zip(references, resolved, strict=True)]
        first, second = frames[:2]
        if first["scope"] != second["scope"]:
            raise DataError("MARKET_MERGE_INCOMPATIBLE")
        start, end = min(first["start"], second["start"]), max(first["end"], second["end"])
        verified = frames[2] if verification_ref is not None else None
        if verified and (verified["scope"] != first["scope"]
                or [verified[key] for key in _DATE_FIELDS] != [start, end] or not verified["native_references"]):
            raise DataError("MARKET_MERGE_INCOMPATIBLE_VERIFICATION")
        result, rows = merge_rows(first, second, policy, verification=verified)
        native = (verified or first)["native_references"]
        if native and result["can_merge"] and not verified:
            original = read_native_references(self.library, principal.user_id, native, start, end)
            if project(original, start, end) != rows:
                raise DataError("MARKET_MERGE_VERIFICATION_COVERAGE")
        result.update(inputs=refs, verification_reference=verification_ref,
                      conflict_policy=policy, requested_range={"start": start, "end": end})
        if result["requires_verification"]:
            profile = result["profile"]
            result["verification_request"] = {
                "provider": profile["provider"], "tickers": [item["ticker"] for item in profile["securities"]],
                "securities": profile["securities"], "kind": "dataset", "start": start, "end": end,
                "frequency": profile["frequency"], "currency": profile["currency"],
                "timezone": profile["timezone"], "adjustment": profile["adjustment"],
                "price_basis": profile["price_basis"],
            }
        result["preview_hash"] = fingerprint({"preview": result, "rows": None if rows is None else fingerprint(rows),
                                              "native_references": native})
        return result, rows, verified or first, native, second

    def preview(self, principal, request):
        if approval(principal):
            with closing(self.db.connect()) as con:
                approval(principal)(con)
        return self._plan(principal, *self._merge_request(request, publish=False))[0]

    def _replay(self, principal, key, binding):
        owner = principal.user_id
        identifier(key)
        with self.db.transaction() as con:
            guard = approval(principal)
            if guard:
                guard(con)
            old = con.execute("SELECT request_hash,response FROM data_library_keys WHERE owner=? AND key=?",
                              (owner, key)).fetchone()
            if old is not None:
                if old[0] != fingerprint(binding):
                    raise DataError("IDEMPOTENCY_CONFLICT")
                return json.loads(old[1])
        return None

    def _guard_sources(self, principal, refs, connection_id):
        owner = principal.user_id
        store = history_store(self.library) if any(ref["source"] == "history" for ref in refs) else None
        def check(con):
            guard = approval(principal)
            if guard:
                guard(con)
            if read_connection(con, owner, connection_id)["archived"]:
                raise DataError("MARKET_CONNECTION_ARCHIVED")
            for ref in refs:
                if ref["source"] == "library":
                    latest = self.library._get(con, owner, ref["id"], selectable=True)
                    actual = _ref("library", latest)
                else:
                    from .history_store import view_hash
                    value = store._version(con, owner, ref["id"], ref["version"])
                    actual = {**_ref("history", value), "content_hash": view_hash(
                        value["content_hash"], value["requested_range"], 0, value["actual_coverage"]["row_count"])}
                if actual != ref:
                    raise DataError("DATASET_REFERENCE_MISMATCH")
        return check

    def _save(self, principal, *, plan, rows, source_frame, native, request, key, default_connection):
        owner = principal.user_id
        profile = plan["profile"]
        connection_id = request.get("connection_id", default_connection)
        if type(connection_id) is not str:
            raise DataError("MARKET_CONNECTION_REQUIRED")
        connection = self.library.active_connection(owner, connection_id)
        if connection["provider"] != profile["provider"]:
            raise DataError("MARKET_MERGE_INCOMPATIBLE")
        start, end = (plan["requested_range"][key] for key in _DATE_FIELDS)
        source = {"provider": profile["provider"], "reference": "verified-market-management-v1",
                  "universe_as_of": None}
        body = {"connection_id": connection_id, "name": request["name"], "kind": "dataset",
                "benchmark": None, "universe": resolve_universe({"manual": " ".join(
                    item["ticker"] for item in profile["securities"])}),
                "start": start, "end": end, "metadata": _prices_profile(profile), "rows": rows,
                "scope": source_frame["scope"], "source": source, "received_at": parse_instant(
                    source_frame["received_at"]).isoformat()}
        provenance = {key: plan[key] for key in ("inputs", "verification_reference", "conflict_policy",
                      "preview_hash", "profile", "coverage", "requested_range")}
        provenance.update(operation=request["operation"], native_references=native,
                          rows_hash=fingerprint(rows))
        acq = {"provider": profile["provider"], "price_policy": profile["price_basis"],
               "quantity_basis": QUANTITY_BASIS, "raw_compatible": False} if native else None
        refs = plan["inputs"] + ([plan["verification_reference"]] if plan["verification_reference"] else [])
        return self.library.import_dataset(owner, body, key, security_metadata=source_frame["security_metadata"],
            managed_origin={"request": request, "provenance": provenance, "acquisition": acq},
            publication_guard=self._guard_sources(principal, refs, connection_id))

    def merge(self, principal, request):
        refs, verification, policy = self._merge_request(request, publish=True)
        name = checked_name(request["name"], 120)
        expected = request["preview_hash"]
        if type(expected) is not str or not re.fullmatch("[0-9a-f]{64}", expected):
            raise DataError("MARKET_MERGE_PREVIEW_CHANGED")
        binding = {"operation": "merge", "inputs": refs, "verification_reference": verification,
                   "preview_hash": expected, "conflict_policy": policy, "name": name}
        if "connection_id" in request:
            binding["connection_id"] = request["connection_id"]
        key = request["idempotency_key"]
        old = self._replay(principal, key, binding)
        if old is not None:
            self._require_access(principal, self.library.get(principal.user_id, old["dataset_id"]))
            return old
        result, rows, base, native, other = self._plan(principal, refs, verification, policy)
        if result["preview_hash"] != expected:
            raise DataError("MARKET_MERGE_PREVIEW_CHANGED")
        if not result["can_merge"]:
            raise DataError(result["reason_code"])
        return self._save(principal, plan=result, rows=rows, source_frame=base, native=native,
                          request=binding, key=key, default_connection=base["connection_id"] or other["connection_id"])

    def reuse(self, principal, request):
        required = {"reference", "idempotency_key"}
        if type(request) is not dict or not required <= request.keys() or set(request) - required - {"name", "connection_id"}:
            raise DataError("INVALID_INPUT")
        ref = reference(request["reference"])
        key = request["idempotency_key"]
        if ref["source"] == "library":
            identifier(key)
            if approval(principal):
                with closing(self.db.connect()) as con:
                    approval(principal)(con)
            if "name" in request or "connection_id" in request:
                raise DataError("MARKET_REUSE_LIBRARY_REFERENCE_ONLY")
            value = self.library.get(principal.user_id, ref["id"], selectable=True)
            self._require_access(principal, value)
            if _ref("library", value) != ref:
                raise DataError("DATASET_REFERENCE_MISMATCH")
            return {"dataset_id": ref["id"], "version": ref["version"], "content_hash": ref["content_hash"]}
        name = checked_name(request.get("name", "复用已保存日线"), 120)
        binding = {"operation": "reuse", "reference": ref, "name": name,
                   "connection_id": request.get("connection_id")}
        old = self._replay(principal, key, binding)
        if old is not None:
            self._require_access(principal, self.library.get(principal.user_id, old["dataset_id"]))
            return old
        frame = self._load(principal, ref, self._resolve(principal, ref))
        dates = [row["session_date"] for row in frame["rows"]]
        plan = {"profile": frame["profile"], "inputs": [ref], "verification_reference": None,
                "conflict_policy": "reject", "preview_hash": fingerprint(binding),
                "coverage": {"start": min(dates), "end": max(dates), "observed_only": True},
                "requested_range": {key: frame[key] for key in _DATE_FIELDS}}
        return self._save(principal, plan=plan, rows=frame["rows"], source_frame=frame,
                          request=binding, key=key, native=frame["native_references"], default_connection=None)

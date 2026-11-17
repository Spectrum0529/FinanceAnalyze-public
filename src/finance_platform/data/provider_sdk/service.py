"""SDK observation family, isolated from legacy raw records and snapshots."""
from contextlib import closing
import csv
import io
import json
from uuid import uuid4

from finance_platform.core.contracts import Instrument, canonical, fingerprint, identifier, parse_instant, payload
from finance_platform.core.security import clean
from finance_platform.data.contracts import DataError, validate_paging
from finance_platform.data.service import DataService
from predictor.research.append_guards import install as install_guards
from .contracts import FAMILY, PROVIDERS, SINA_INTERFACE, source_request_parameters, require_declaration, checked_json, now
from .history_contracts import HISTORY_PROFILE, SINA_QFQ_INTERFACE, output_limits, YAHOO_PRICE_POLICY, SINA_PRICE_POLICY, NATIVE_INTERFACE, NATIVE_PRICE_POLICY
from . import sina_quality

MIGRATIONS = ("""CREATE TABLE data_provider_observation_results(
 result_id TEXT PRIMARY KEY,dataset_id TEXT NOT NULL REFERENCES data_datasets(dataset_id),
 owner_id TEXT NOT NULL,collection_id TEXT NOT NULL,unit_ordinal INTEGER NOT NULL,
 source_version INTEGER NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(dataset_id,collection_id,unit_ordinal));""",)


class ProviderDataService(DataService):
    def __init__(self, database, policy):
        super().__init__(database, policy)
        database.apply_module_migrations("data_provider_observations", MIGRATIONS)
        from ..parquet_store import ParquetRows, MIGRATIONS as PARQUET_MIGRATIONS
        from finance_platform.core.database import immutable
        database.apply_module_migrations("data_parquet_history_1", PARQUET_MIGRATIONS)
        self.parquet = ParquetRows(policy)
        with database.transaction() as con:
            immutable(con, "data_provider_row_storage", ("result_id",))
        with database.transaction() as con:
            for action in ("UPDATE", "DELETE"):
                con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_provider_observations_{action} BEFORE {action} ON data_provider_observation_results BEGIN SELECT RAISE(ABORT,'Append-only provider observations'); END")
            install_guards(con, ("data_provider_observation_results",))

    @staticmethod
    def _source_key(owner_id, source_id):
        identifier(owner_id); identifier(source_id)
        return "sdk-"+fingerprint({"owner":owner_id,"source":source_id})

    def register_source(self, owner_id, source):
        if not isinstance(source, dict) or source.get("provider") not in PROVIDERS:
            if isinstance(source,dict) and isinstance(source.get("source_id"),str):
                with closing(self.db.connect()) as con:
                    sdk=con.execute("SELECT 1 FROM data_sources WHERE source_id=? AND owner_id=?",(self._source_key(owner_id,source["source_id"]),owner_id)).fetchone()
                if sdk:raise DataError("SOURCE_ID_PROVIDER_CONFLICT")
            return super().register_source(owner_id, source)
        fields={"source_id","name","provider","capabilities","assets","frequencies","fields","declared_history","verified_coverage","license","connection"}
        if set(source)!=fields or type(source["name"]) is not str or not 1<=len(source["name"])<=200:
            raise DataError("SOURCE_SCHEMA_INVALID")
        for field in ("capabilities","assets","frequencies","fields"):
            if type(source[field]) is not list or len(source[field])>100 or any(type(v)is not str or not 1<=len(v)<=128 for v in source[field]):
                raise DataError("SOURCE_SCHEMA_INVALID")
        if source["verified_coverage"]!=[]:raise DataError("PROVIDER_COVERAGE_REVIEW_REQUIRED")
        if set(source["license"])!={"license_id","evidence","use_allowed","rights_verified"}:
            raise DataError("LICENSE_SCHEMA_INVALID")
        identifier(source["license"]["license_id"])
        require_declaration(source)
        expected={"endpoint":PROVIDERS[source["provider"]]["origin"],"key_env":None,"secret_env":None,"user_agent_env":None,"feed":None}
        if source["connection"]!=expected:raise DataError("SDK_CONNECTION_INVALID")
        if source["frequencies"]!=["1Day"]:raise DataError("SDK_INTERVAL_UNSUPPORTED")
        key=self._source_key(owner_id,source["source_id"])
        with closing(self.db.connect()) as con:
            legacy=con.execute("SELECT 1 FROM data_sources WHERE source_id=? AND owner_id=?",(source["source_id"],owner_id)).fetchone()
        if legacy:raise DataError("SOURCE_ID_PROVIDER_CONFLICT")
        body=clean(source); digest=fingerprint(body)
        with self.db.transaction() as con:
            old=con.execute("SELECT version,content_hash FROM data_sources WHERE source_id=? AND owner_id=? ORDER BY version DESC LIMIT 1",(key,owner_id)).fetchone()
            if old and old["content_hash"]==digest:
                return {"source_id":source["source_id"],"version":old["version"],"content_hash":digest}
            version=1 if old is None else old["version"]+1
            con.execute("INSERT INTO data_sources VALUES(?,?,?,?,?,?)",(key,version,owner_id,canonical(body),digest,now()))
        return {"source_id":source["source_id"],"version":version,"content_hash":digest}

    def source(self, owner_id, source_id, version=None):
        key=self._source_key(owner_id,source_id)
        with closing(self.db.connect()) as con:
            suffix="AND version=?" if version is not None else "ORDER BY version DESC LIMIT 1"
            args=(key,owner_id,version) if version is not None else (key,owner_id)
            row=con.execute("SELECT * FROM data_sources WHERE source_id=? AND owner_id=? "+suffix,args).fetchone()
        if row is None:return super().source(owner_id,source_id,version)
        body=json.loads(row["body"])
        if fingerprint(body)!=row["content_hash"]:raise DataError("SOURCE_HASH_MISMATCH")
        return {**body,"version":row["version"],"content_hash":row["content_hash"]}

    def create_dataset(self, owner_id, source_id, *, scope, identities, title="Development dataset", issuer_ids=()):
        source=self.source(owner_id,source_id)
        if source["provider"] not in PROVIDERS:
            return super().create_dataset(owner_id,source_id,scope=scope,identities=identities,title=title,issuer_ids=issuer_ids)
        require_declaration(source)
        if scope!="DEVELOPMENT" or issuer_ids or not isinstance(identities,list) or not 1<=len(identities)<=100:
            raise DataError("SDK_DATASET_SCOPE_INVALID")
        mappings=[payload(Instrument.from_mapping(i)) for i in identities]
        for i,a in enumerate(mappings):
            for b in mappings[i+1:]:
                if (a["ticker"]==b["ticker"] or a["instrument_id"]==b["instrument_id"]) and (b["ticker_valid_to"] is None or parse_instant(a["ticker_valid_from"])<parse_instant(b["ticker_valid_to"])) and (a["ticker_valid_to"] is None or parse_instant(b["ticker_valid_from"])<parse_instant(a["ticker_valid_to"])):
                    raise DataError("IDENTITY_OVERLAP")
        if any(i["currency"]!="USD" or i["asset_class"] not in {"US_EQUITY","US_ETF"} for i in mappings):
            raise DataError("PROVIDER_IDENTITY_MISMATCH")
        body={"scope":scope,"identities":mappings,"issuer_ids":[],"title":clean(title),"source_id":source_id,
              "source_version":source["version"],"formal_research_approved":False,"data_family":FAMILY}
        dataset_id="dataset-"+uuid4().hex
        with self.db.transaction() as con:
            con.execute("INSERT INTO data_datasets VALUES(?,?,?,?,?,?,?)",(dataset_id,owner_id,self._source_key(owner_id,source_id),source["version"],canonical(body),fingerprint(body),now()))
        return {"dataset_id":dataset_id,**body,"content_hash":fingerprint(body)}

    def reject_legacy(self, owner_id, dataset_id):
        if self.dataset(owner_id,dataset_id).get("data_family")==FAMILY:
            raise DataError("PROVIDER_OBSERVATION_ONLY")

    def preview(self, owner_id, dataset_id, *args, **kwargs):
        self.reject_legacy(owner_id,dataset_id)
        return super().preview(owner_id,dataset_id,*args,**kwargs)

    def import_bytes(self, owner_id, dataset_id, *args, **kwargs):
        self.reject_legacy(owner_id,dataset_id)
        return super().import_bytes(owner_id,dataset_id,*args,**kwargs)

    def import_events_bytes(self, owner_id, dataset_id, *args, **kwargs):
        self.reject_legacy(owner_id,dataset_id)
        return super().import_events_bytes(owner_id,dataset_id,*args,**kwargs)

    def append_records(self, owner_id, dataset_id, *args, **kwargs):
        self.reject_legacy(owner_id,dataset_id)
        return super().append_records(owner_id,dataset_id,*args,**kwargs)

    def snapshot(self, owner_id, dataset_id, *args, **kwargs):
        self.reject_legacy(owner_id,dataset_id)
        return super().snapshot(owner_id,dataset_id,*args,**kwargs)

    def get_snapshot(self, owner_id, snapshot_id, **kwargs):
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT 1 FROM data_provider_observation_results WHERE owner_id=? AND result_id=?",(owner_id,snapshot_id)).fetchone()
        if row:raise DataError("PROVIDER_OBSERVATION_ONLY")
        return super().get_snapshot(owner_id,snapshot_id,**kwargs)

    def resolve_call(self, owner_id, dataset_id, parameters, *, probe=False):
        dataset=self.dataset(owner_id,dataset_id)
        source=self.source(owner_id,dataset["source_id"],dataset["source_version"])
        require_declaration(source)
        if dataset.get("history_profile") == HISTORY_PROFILE:
            from ..history_store import HistoryStore
            security = HistoryStore(self).security(owner_id, dataset["identity_evidence_ref"]["security_id"])
            if security["content_hash"] != dataset["identity_evidence_ref"]["content_hash"] or dataset["identities"] != [security["instrument"]]:
                raise DataError("SECURITY_DATASET_BINDING_MISMATCH")
        return source_request_parameters(source,dataset,parameters,probe=probe)

    def create_history_dataset(self, owner_id, source_id, security_id):
        from ..history_store import HistoryStore
        security = HistoryStore(self).security(owner_id, security_id)
        source = self.source(owner_id, source_id)
        require_declaration(source)
        identity = payload(Instrument.from_mapping(security["instrument"]))
        evidence = {k: security[k] for k in ("security_id", "content_hash")}
        body = {"scope": "DEVELOPMENT", "identities": [identity], "issuer_ids": [], "title": security["ticker"]+" saved daily history",
                "source_id": source_id, "source_version": source["version"], "formal_research_approved": False, "data_family": FAMILY,
                "history_profile": HISTORY_PROFILE, "identity_evidence_ref": evidence,
                "identity_binding": {"mic": identity["mic"], "first_trade_hint": security.get("provider_first_trade_hint"), "semantic_hash": security["semantic_hash"]}}
        if source["provider"]=="akshare" and sina_quality.POLICY_ID in source["capabilities"]:
            body["quality_policy"]={"id":sina_quality.POLICY_ID,"content_hash":sina_quality.POLICY_HASH}
        key = "dataset-history-" + fingerprint({"owner": owner_id, "body": body})[:48]
        digest = fingerprint(body)
        with self.db.transaction() as con:
            old = con.execute("SELECT owner_id,content_hash FROM data_datasets WHERE dataset_id=?", (key,)).fetchone()
            if old and (old["owner_id"] != owner_id or old["content_hash"] != digest): raise DataError("DATASET_HASH_MISMATCH")
            if old is None:
                con.execute("INSERT INTO data_datasets VALUES(?,?,?,?,?,?,?)", (key, owner_id, self._source_key(owner_id, source_id), source["version"], canonical(body), digest, now()))
        return {"dataset_id": key, **body, "content_hash": digest}

    @staticmethod
    def capabilities(source):
        require_declaration(source)
        allowed="export" in source["license"]["use_allowed"]
        return {"display":True,"legacy_raw_snapshot":False,"legacy_research":False,"matching_or_simulation":False,
                "export_allowed":allowed,"export_denied_reason":None if allowed else "DATA_USE_NOT_LICENSED"}

    def existing_unit(self, owner_id, dataset_id, collection_id, binding):
        with closing(self.db.connect()) as con:
            page=con.execute("SELECT owner_id,binding_hash,body FROM data_collection_pages WHERE collection_id=? AND ordinal=0",(collection_id,)).fetchone()
        if page is None:return None
        if page["owner_id"]!=owner_id:raise DataError("RESOURCE_NOT_FOUND")
        if page["binding_hash"]!=binding:raise DataError("COLLECTION_BINDING_CHANGED")
        body=json.loads(page["body"])
        return self._full_observation(owner_id,body["result_id"])

    def persist_capture(self, context, owner_id, dataset_id, collection_id, call, manifest, artifacts, execution):
        """Only private trusted runner/capture adapter calls this, never HTTP JSON."""
        identifier(collection_id)
        dataset=self.dataset(owner_id,dataset_id)
        source=self.source(owner_id,dataset["source_id"],dataset["source_version"])
        use=require_declaration(source)
        expected_interface = NATIVE_INTERFACE if NATIVE_INTERFACE in source.get("capabilities", []) else SINA_QFQ_INTERFACE if dataset.get("history_profile") == HISTORY_PROFILE else SINA_INTERFACE
        if source["provider"] == "akshare" and (source.get("connection", {}).get("endpoint") != PROVIDERS["akshare"]["origin"]
                or call.get("interface") != expected_interface or manifest.get("sdk_ref", {}).get("interface") != expected_interface):
            raise DataError("SDK_SOURCE_REDECLARATION_REQUIRED")
        if call.get("interface") == NATIVE_INTERFACE:
            expected_call = self.resolve_call(owner_id, dataset_id, {
                "symbols": [call.get("ticker")], "provider_symbols": {call.get("ticker"): call.get("provider_symbol")},
                "start": call.get("start"), "end": call.get("end"), "timeframe": "1Day"},
                probe=call.get("operation") == "probe")
            if expected_call != call:
                raise DataError("SDK_NATIVE_CALL_BINDING_MISMATCH")
        binding=fingerprint({"dataset_id":dataset_id,"source_hash":source["content_hash"],"call":call,"family":FAMILY})
        old=self.existing_unit(owner_id,dataset_id,collection_id,binding)
        if old is not None:return old
        limits = output_limits(call)
        rows=checked_json(artifacts["observations.json"], limit=limits["observations"], max_nodes=limits["nodes"])
        snapshot=checked_json(artifacts["sdk-frame.json"], limit=limits["snapshot"], max_nodes=limits["nodes"])
        if call.get("profile") == HISTORY_PROFILE and manifest["sdk_ref"].get("price_policy") != (YAHOO_PRICE_POLICY if source["provider"] == "yfinance" else NATIVE_PRICE_POLICY if expected_interface == NATIVE_INTERFACE else SINA_PRICE_POLICY):
            raise DataError("HISTORY_PRICE_POLICY_UNSUPPORTED")
        if call.get("interface") == NATIVE_INTERFACE:
            from .sina_native_rows import validate_native_capture
            validate_native_capture(call, manifest, artifacts, snapshot, rows)
        quality_mode=sina_quality.enabled(call)
        quality=manifest["sdk_ref"].get("data_quality")
        if quality_mode:
            if dataset.get("quality_policy")!={"id":sina_quality.POLICY_ID,"content_hash":sina_quality.POLICY_HASH}:raise DataError("SINA_QUALITY_POLICY_INVALID")
            sina_quality.validate_capture(quality,call,manifest,artifacts,rows)
        elif quality is not None:raise DataError("SINA_QUALITY_POLICY_INVALID")
        if type(rows)is not list or not 1<=len(rows)<=call["max_rows"] or len(snapshot["rows"])!=len(rows):
            raise DataError("SDK_OUTPUT_INVALID")
        if any(r.get("price_basis")!="PROVIDER_OBSERVED" or r.get("raw_compatible") is not False or r.get("historical_first_available_at")is not None for r in rows):
            raise DataError("SDK_OUTPUT_INVALID")
        received=max(r["observed_received_at"] for r in rows)
        parse_instant(received)
        storage_binding=self.market.bind(owner_id,context=context)
        refs=[]
        for entity in manifest["entities"]:
            context.require_active()
            raw=artifacts[entity["artifact"]]
            ref=self.archive(owner_id,source,raw,parameters={"call":call,"entity_role":entity["role"]},received_at=entity["observed_received_at"],scope="DEVELOPMENT",schema="sdk-original-financial-entity-1",binding=storage_binding)
            refs.append({**{k:v for k,v in entity.items() if k!="artifact"},"archive":ref})
        context.require_active()
        sdk_snapshot=self.archive(owner_id,source,artifacts["sdk-frame.json"],parameters={"call":call},received_at=received,scope="DEVELOPMENT",schema="sdk-frame-snapshot-1",binding=storage_binding)
        series=[{k:v for k,v in r.items() if k not in {"observed_received_at","raw_row_locator","sdk_row_locator"}} for r in rows]
        result_id="observation-"+uuid4().hex
        body={"schema_version":FAMILY,"data_family":FAMILY,"result_id":result_id,"dataset_id":dataset_id,
              "collection_id":collection_id,"unit_ordinal":0,"provider":source["provider"],"sdk_version":manifest["sdk_ref"]["version"],
              "source_ref":{"source_id":source["source_id"],"version":source["version"],"content_hash":source["content_hash"]},
              "use_basis_ref":{**use,"source_owner_id":owner_id,"source_version":source["version"]},"sdk_ref":manifest["sdk_ref"],
              "date_range":{"start":call["start"],"end":call["end"]},"request_parameters":call,
              "session_date_min":min(r["session_date"] for r in rows),"session_date_max":max(r["session_date"] for r in rows),
              "row_count":len(rows),"observed_received_at":received,"availability_basis":"OBSERVED_RECEIPT_ONLY","historical_first_available_at":None,
              "price_basis":"PROVIDER_OBSERVED","raw_compatible":False,"basis_verification":"PROVIDER_DOCUMENTED_OR_REQUESTED_NOT_PER_ROW_CERTIFIED","raw_http_entities":refs,"sdk_result_snapshot":sdk_snapshot,
              "rows":rows,"quality_flags":sorted({flag for r in rows for flag in r["sdk_transform_flags"]}),
              "warnings":sorted({flag for r in rows for flag in r["sdk_transform_flags"]}),"use_capabilities":self.capabilities(source),"series_content_hash":fingerprint(series),"execution":execution}
        if call.get("profile") == HISTORY_PROFILE:
            body.update(profile=HISTORY_PROFILE, price_policy=manifest["sdk_ref"]["price_policy"], quantity_basis="ADJUSTED_SIMULATION_UNITS")
        if quality_mode:body["data_quality"]=quality
        digest=fingerprint(body)
        # Serialize/publish complete blobs before taking the SQLite writer lock.
        # Cancellation can then commit while Arrow/IO is running; final
        # transaction fencing decides whether the observation is visible.
        stored_body, descriptor = body, None
        if call.get("profile") == HISTORY_PROFILE:
            descriptor = self.parquet.write(context, owner_id, body["rows"])
            stored_body = {k: v for k, v in body.items() if k != "rows"}
        stored_body=self.market.pack(owner_id,stored_body,storage_binding)
        stored_body['source_ref']=body['source_ref']
        page={"collection_id":collection_id,"ordinal":0,"binding_hash":binding,"result_id":result_id,"next_cursor":None,"records":len(rows),"data_family":FAMILY}
        context.require_active()
        with self.db.transaction() as con:
            context.assert_fenced(con)
            existing=con.execute("SELECT result_id FROM data_provider_observation_results WHERE dataset_id=? AND collection_id=? AND unit_ordinal=0",(dataset_id,collection_id)).fetchone()
            if existing is not None:raise DataError("COLLECTION_CONCURRENT_COMMIT")
            con.execute("INSERT INTO data_provider_observation_results VALUES(?,?,?,?,?,?,?,?,?)",(result_id,dataset_id,owner_id,collection_id,0,source["version"],canonical(stored_body),digest,now()))
            if descriptor is not None:
                con.execute("INSERT INTO data_provider_row_storage VALUES(?,?,?,?,?,?,?)",
                    (result_id, owner_id, descriptor["format"], descriptor["blob_sha256"],
                     descriptor["rows_content_hash"], descriptor["row_count"], descriptor["byte_count"]))
            con.execute("INSERT INTO data_collection_pages VALUES(?,?,?,?,?)",(collection_id,0,owner_id,binding,canonical(page)))
            con.execute("INSERT INTO data_collection_events(collection_id,owner_id,body,created_at) VALUES(?,?,?,?)",(collection_id,owner_id,canonical({"kind":"SDK_UNIT_COMMITTED","result_id":result_id}),now()))
        return self._full_observation(owner_id,result_id)

    def observation(self, owner_id, result_id, limit=50, offset=0):
        validate_paging(limit,offset)
        body=self._full_observation(owner_id,result_id)
        return {**body,"rows":body["rows"][offset:offset+limit],"total":body["row_count"],"limit":limit,"offset":offset}

    def _full_observation(self, owner_id, result_id):
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT body,content_hash FROM data_provider_observation_results WHERE result_id=? AND owner_id=?",(result_id,owner_id)).fetchone()
        if row is None:raise DataError("RESOURCE_NOT_FOUND")
        stored=json.loads(row["body"]);read_scope=self.market.scope(owner_id,stored)
        body=stored
        # Authorize source use before opening any newly externalized rows.
        source=self.source(owner_id,body["source_ref"]["source_id"],body["source_ref"]["version"])
        if source["content_hash"]!=body["source_ref"]["content_hash"]:raise DataError("SOURCE_HASH_MISMATCH")
        if read_scope is not None:
            full=self.market.unpack(owner_id,{k:v for k,v in body.items() if k!='source_ref'})
            if full['source_ref']!=body['source_ref']:raise DataError('SOURCE_HASH_MISMATCH')
            body=full
        if self.capabilities(source)!=body["use_capabilities"]:raise DataError("OBSERVATION_POLICY_CHANGED")
        # Dispatch only after the original owner-scoped observation lookup.
        with closing(self.db.connect()) as con:
            storage = con.execute("SELECT format,blob_sha256,rows_content_hash,row_count,byte_count "
                "FROM data_provider_row_storage WHERE result_id=? AND owner_id=?", (result_id, owner_id)).fetchone()
        if storage is not None:
            if "rows" in body:raise DataError("PARQUET_DESCRIPTOR_INVALID")
            body["rows"] = self.parquet.read(owner_id, dict(storage),read_scope=read_scope)
        if fingerprint(body)!=row["content_hash"]:raise DataError("OBSERVATION_HASH_MISMATCH")
        source=self.source(owner_id,body["source_ref"]["source_id"],body["source_ref"]["version"])
        if source["content_hash"]!=body["source_ref"]["content_hash"]:raise DataError("SOURCE_HASH_MISMATCH")
        if self.capabilities(source)!=body["use_capabilities"]:raise DataError("OBSERVATION_POLICY_CHANGED")
        return {**body,"content_hash":row["content_hash"]}

    def observations(self, owner_id, dataset_id, limit=50, offset=0):
        validate_paging(limit,offset)
        dataset=self.dataset(owner_id,dataset_id)
        if dataset.get("data_family")!=FAMILY:raise DataError("PROVIDER_OBSERVATION_ONLY")
        with closing(self.db.connect()) as con:
            ids=con.execute("SELECT result_id FROM data_provider_observation_results WHERE owner_id=? AND dataset_id=? ORDER BY created_at DESC,result_id LIMIT ? OFFSET ?",(owner_id,dataset_id,limit,offset)).fetchall()
            total=con.execute("SELECT count(*) FROM data_provider_observation_results WHERE owner_id=? AND dataset_id=?",(owner_id,dataset_id)).fetchone()[0]
        fields={"result_id","dataset_id","collection_id","provider","sdk_version","source_ref","date_range","session_date_min","session_date_max","row_count","observed_received_at","price_basis","raw_compatible","series_content_hash","content_hash"}
        items=[]
        for item in ids:
            full=self.observation(owner_id,item[0],1,0)
            items.append({**{k:v for k,v in full.items() if k in fields},**{k:full["use_capabilities"][k] for k in ("export_allowed","export_denied_reason")}})
        return {"items":items,"total":total,"limit":limit,"offset":offset}

    def export_observation(self, owner_id, result_id, format):
        full=self._full_observation(owner_id,result_id)
        source=self.source(owner_id,full["source_ref"]["source_id"],full["source_ref"]["version"])
        require_declaration(source,"export")
        if format=="json":return canonical(full).encode()
        if format!="csv":raise DataError("EXPORT_FORMAT_UNSUPPORTED")
        output=io.StringIO(newline="")
        metadata={"source_ref":canonical(full["source_ref"]),"use_basis_ref":canonical(full["use_basis_ref"]),"sdk_ref":canonical(full["sdk_ref"]),
                  "raw_http_entities":canonical(full["raw_http_entities"]),"sdk_result_snapshot":canonical(full["sdk_result_snapshot"]),
                  "observation_result_id":result_id,"observation_content_hash":full["content_hash"]}
        fields=["session_date","existing_instrument_id","provider_symbol","open","high","low","close","volume","currency","price_basis","raw_compatible","observed_received_at","availability_basis","historical_first_available_at",
                "price_unit","volume_unit","volume_share_basis","currency_and_unit_evidence","provider_identity_verification","provider_basis_claim","source_adjustment_request","basis_verification","basis_reference_time","completeness","sdk_transform_flags","raw_row_locator","sdk_row_locator",*metadata]
        writer=csv.DictWriter(output,fieldnames=fields,extrasaction="ignore");writer.writeheader()
        for row in full["rows"]:
            projected={key:(canonical(value) if isinstance(value,(dict,list)) else value) for key,value in row.items()}
            writer.writerow({**projected,**metadata})
        return output.getvalue().encode("utf-8-sig")

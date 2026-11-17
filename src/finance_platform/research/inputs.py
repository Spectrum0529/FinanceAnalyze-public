"""Server-owned source/rights/content authorization before research effects."""
from contextlib import closing
from dataclasses import dataclass
import json
from finance_platform.core.contracts import VersionRef,DataScope,payload,fingerprint,Instrument,parse_instant,parse_decimal
from finance_platform.data.service import DataService
from predictor.access import AccessPolicy
from .data import Bar,Tick,DevelopmentSample,ResearchError,sample_from_version
from .snapshot import snapshot_sample


def legacy_snapshot_sample(snapshot,dataset,source):
    """Decode the original v2 algorithm for comparison, never create new v2 inputs."""
    bars,ticks=[],[]
    for row in snapshot["records"]:
        if row["kind"]!="bar":continue
        v,m=row["values"],row["metadata"]
        start,end=parse_instant(m["effective_at"]),parse_instant(v["bar_end"])
        available=parse_instant(m["available_at"])
        frequency={"1Day":"1d","1Min":"1m"}[v["frequency"]]
        bars.append(Bar(row["instrument_id"],start,end,available,parse_decimal(v["close"]),parse_decimal(v["volume"]),frequency,v["trading_status"]=="TRADABLE"))
        ticks.append(Tick("close-"+m["record_id"][:40],row["instrument_id"],end,available,parse_decimal(v["close"]),parse_decimal(v["volume"]),v["trading_status"]=="TRADABLE"))
    return DevelopmentSample(tuple(Instrument.from_mapping(i) for i in dataset["identities"]),tuple(bars),DataScope(snapshot["scope"]),dataset["source_id"],source["license"]["license_id"],tuple(ticks))


@dataclass(frozen=True)
class InputGrant:
    data: object
    owner_id: str
    origin: dict | None
    action_metadata: dict

    def authorize_action(self,provenance):
        if self.origin is None:return False
        self.data.authorize_snapshot(self.owner_id,self.origin["snapshot_id"],purpose="development",expected_origin=self.origin)
        return self.action_metadata.get(provenance.record_id)==fingerprint(payload(provenance))


class InputAuthorizer:
    def __init__(self,runtime):
        self.runtime=runtime
        # Initialized outside core resolver transactions; validation is readonly.
        self.data=DataService(runtime.database,AccessPolicy(runtime.root))

    def validate_qlib(self,principal,export_reference,*,require_native=True):
        """Readonly native-export grant from the trusted data-owner adapter.

        FA014 owns the real manifest/receipt/AccessPolicy mapping. No private
        dataset/URI/YAML supplied by a client can substitute for this resolver.
        """
        from .qlib_workflow import reference,validate_input
        reference(export_reference)
        ref=VersionRef(**export_reference)
        self.runtime.versions.get(principal,ref)
        with closing(self.runtime.database.connect()) as con:
            row=con.execute("SELECT owner_id,kind FROM resource_versions WHERE resource_id=? AND version=?",
                            (ref.resource_id,ref.version)).fetchone()
        if row is None or row["owner_id"]!=principal.user_id or row["kind"]!="data":
            raise ResearchError("QLIB_EXPORT_OWNER_OR_KIND_INVALID")
        resolver=getattr(self.runtime,"qlib_export_authorizer",None)
        if resolver is None or not callable(getattr(resolver,"authorize",None)):
            raise ResearchError("QLIB_AUTHORIZED_EXPORT_RESOLVER_REQUIRED")
        grant=resolver.authorize(principal,export_reference,purpose="development_readonly")
        if (type(grant)is not dict or set(grant)!={"owner_id","export_reference","source_reference","data"}
                or grant["owner_id"]!=principal.user_id or grant["export_reference"]!=export_reference):
            raise ResearchError("QLIB_EXPORT_AUTHORIZATION_INVALID")
        source=reference(grant["source_reference"])
        self.runtime.versions.get(principal,VersionRef(**source))
        with closing(self.runtime.database.connect()) as con:
            source_row=con.execute("SELECT owner_id,kind FROM resource_versions WHERE resource_id=? AND version=?",
                                   (source["resource_id"],source["version"])).fetchone()
        if source_row is None or source_row["owner_id"]!=principal.user_id or source_row["kind"]!="data":
            raise ResearchError("QLIB_SOURCE_OWNER_OR_KIND_INVALID")
        validate_input(grant["data"])
        if require_native and grant["data"]["native_dump_succeeded"] is not True:raise ResearchError("QLIB_NATIVE_DUMP_NOT_READY")
        return grant

    def validate(self,principal,reference):
        reference=reference if isinstance(reference,VersionRef) else VersionRef(**reference)
        value=self.runtime.versions.get(principal,reference)
        sample=sample_from_version(value)
        with closing(self.runtime.database.connect()) as con:
            resource=con.execute("SELECT owner_id,kind FROM resource_versions WHERE resource_id=? AND version=?",(reference.resource_id,reference.version)).fetchone()
            if resource["kind"]!="data":raise ResearchError("RESEARCH_INPUT_RESOURCE_KIND_INVALID")
            owner=resource["owner_id"]
            registered=con.execute("SELECT policy_json FROM research_data_export_bindings WHERE resource_id=? AND version=?",(reference.resource_id,reference.version)).fetchone() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_data_export_bindings'").fetchone() else None
        origin=value.get("origin") if value.get("schema_version") in {"research-input-2","research-input-3"} else None
        if origin and origin.get("origin_kind")=="data_snapshot":
            self.data.authorize_snapshot(owner,origin["snapshot_id"],purpose="development",expected_origin=origin)
            snapshot=self.data.get_snapshot(owner,origin["snapshot_id"],require_quality=True)
            dataset=self.data.dataset(owner,snapshot["dataset_id"])
            source=self.data.source(owner,dataset["source_id"],dataset["source_version"])
            if value["schema_version"]=="research-input-3":
                expected,execution=snapshot_sample(snapshot,dataset,source)
                if fingerprint(value["source_snapshot"])!=fingerprint(snapshot) or fingerprint(value["execution"])!=fingerprint(execution) or fingerprint(value["sample"])!=fingerprint(payload(expected)):
                    raise ResearchError("RESEARCH_SNAPSHOT_CONTENT_MISMATCH")
            else:
                expected=legacy_snapshot_sample(snapshot,dataset,source)
                if fingerprint(value["sample"])!=fingerprint(payload(expected)):
                    raise ResearchError("RESEARCH_SNAPSHOT_CONTENT_MISMATCH")
            action_metadata={r["metadata"]["record_id"]:fingerprint(r["metadata"]) for r in snapshot["records"] if r["kind"]=="action"}
            return value,sample,InputGrant(self.data,owner,origin,action_metadata)
        if origin:
            if (registered is None or json.loads(registered["policy_json"])!={"kind":"PLATFORM_GENERATED_SYNTHETIC","allowed":True}
                    or origin.get("origin_kind")!="generated" or origin.get("generator")!="platform-synthetic-v1"
                    or origin.get("scope")!="SYNTHETIC" or origin.get("sample_hash")!=fingerprint(value["sample"])):
                raise ResearchError("RESEARCH_INPUT_ORIGIN_NOT_AUTHORIZED")
        elif sample.scope is not DataScope.SYNTHETIC:
            raise ResearchError("RESEARCH_DEVELOPMENT_ORIGIN_REQUIRED")
        if sample.scope is not DataScope.SYNTHETIC:
            raise ResearchError("RESEARCH_INPUT_ORIGIN_NOT_AUTHORIZED")
        return value,sample,InputGrant(self.data,owner,None,{})

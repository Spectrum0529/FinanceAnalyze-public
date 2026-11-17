"""Trusted project-local Qlib launcher construction; no default activation."""
from pathlib import Path
from .errors import PlatformError
from .qlib_native import QlibBinding, QlibProbeCompletion, QlibNativeLauncher
from finance_platform.research import qlib_workflow


def project_qlib_launcher(runtime, binding):
    """Called only for an explicit server-supplied exact typed binding.

    Native65/security/financial/source receipts remain required FilePins of the
    owner contract. This factory neither invents those receipts nor launches
    a process, configures data/model backends or marks a job successful.
    """
    if type(binding) is not QlibBinding or type(binding.probe_completion) is not QlibProbeCompletion:
        raise PlatformError("QLIB_PROJECT_TYPED_BINDING_REQUIRED")
    root = Path(runtime.root).resolve()
    worker = root / "workers/qlib"
    contract = Path(qlib_workflow.__file__).absolute()
    expected = (
        (binding.worker_python.path, worker / ".venv/Scripts/python.exe"),
        (binding.worker_manifest.path, worker / "pyproject.toml"),
        (binding.worker_lock.path, worker / "uv.lock"),
        (binding.probe_completion.worker_root, worker),
        (binding.entry.path, contract.with_name("qlib_child.py")),
        (binding.contract_source.path, contract),
        (binding.bootstrap.path, Path(__file__).absolute().with_name("qlib_bootstrap.py")),
    )
    if any(actual != wanted for actual, wanted in expected):
        raise PlatformError("QLIB_PROJECT_SOURCE_OR_RUNTIME_MISMATCH")
    if (binding.worker_binding.sha256 != qlib_workflow.WORKER_BINDING_SHA256
            or binding.worker_lock.sha256 != qlib_workflow.WORKER_LOCK_SHA256):
        raise PlatformError("QLIB_PROJECT_CONTRACT_PIN_MISMATCH")
    binding.verify()
    return QlibNativeLauncher(binding, root / ".local/platform/native_research", qlib_workflow)

from dataclasses import dataclass
from copy import deepcopy
from .auth import Principal
from .server_composition import QlibServerMaterial, MetadataPin, read_metadata
from .source_binding import source_digest
from .qlib_native import FilePin


def _file_pin(value):
    if type(value) is not dict or set(value) != {"path", "bytes", "sha256"}:
        raise PlatformError("QLIB_STATIC_PROOF_INVALID")
    return FilePin(Path(value["path"]), value["bytes"], value["sha256"])


@dataclass(frozen=True)
class ExportQlibLauncherFactory:
    runtime: object
    material: QlibServerMaterial

    def __post_init__(self):
        if (type(self.material) is not QlibServerMaterial
                or self.material.root != Path(self.runtime.root).resolve()
                or self.material.profile != qlib_workflow.FIXTURE_ID):
            raise PlatformError("QLIB_SERVER_MATERIAL_INVALID")

    @property
    def worker_binding_sha256(self):
        return self.material.worker_binding.sha256

    @property
    def source_manifest_sha256(self):
        return self.material.source_digest

    @property
    def worker_lock_bytes(self):
        record = read_metadata(self.material.root, self.material.worker_binding)
        expected = self.material.root / "workers/qlib/uv.lock"
        pin = _file_pin(record["lock"])
        if pin.path != expected or pin.sha256 != qlib_workflow.WORKER_LOCK_SHA256:
            raise PlatformError("QLIB_WORKER_LOCK_MISMATCH")
        return pin.read_verified()

    def _review(self, name, native_hash):
        pin = getattr(self.material, name + "_review")
        if pin is None:
            raise PlatformError("QLIB_PROFILE_REVIEW_REQUIRED")
        value = read_metadata(self.material.root, pin)
        fields = {"schema", "kind", "decision", "profile", "source_digest", "worker_binding_sha256",
                  "worker_lock_sha256", "coverage", "review_record"}
        if (type(value) is not dict or set(value) != fields
                or value["schema"] != "finance-qlib-profile-review-1" or value["kind"] != name
                or value["decision"] != "APPROVED_PROFILE_EXECUTION" or value["profile"] != self.material.profile
                or value["source_digest"] != self.material.source_digest
                or value["worker_binding_sha256"] != self.worker_binding_sha256
                or value["worker_lock_sha256"] != qlib_workflow.WORKER_LOCK_SHA256):
            raise PlatformError("QLIB_PROFILE_REVIEW_SCOPE_MISMATCH")
        coverage = value["coverage"]
        if coverage != {"kind": "CURRENT_AUTHORIZED_SYNTHETIC_PROFILE"}:
            if (type(coverage) is not dict or set(coverage) != {"kind", "native_manifest_hashes"}
                    or coverage["kind"] != "EXACT_NATIVE_MANIFESTS"
                    or type(coverage["native_manifest_hashes"]) is not list
                    or not 1 <= len(coverage["native_manifest_hashes"]) <= 128
                    or native_hash not in coverage["native_manifest_hashes"]):
                raise PlatformError("QLIB_PROFILE_REVIEW_SCOPE_MISMATCH")
            for sha in coverage["native_manifest_hashes"]:
                qlib_workflow.hex_digest(sha)
        # This is an applicability record of the existing reviewer. Preserve
        # and re-read its original evidence; never rewrite or elevate old proof.
        evidence = MetadataPin.parse(value["review_record"])
        if evidence == pin:
            raise PlatformError("QLIB_PROFILE_REVIEW_SCOPE_MISMATCH")
        read_metadata(self.material.root, evidence)
        return FilePin(self.material.root / pin.path, pin.bytes, pin.sha256)

    def for_export(self, principal, *, grant, purpose="development_readonly"):
        if type(principal) is not Principal or purpose != "development_readonly":
            raise PlatformError("QLIB_FACTORY_PURPOSE_OR_OWNER_INVALID")
        if (type(grant) is not dict or set(grant) != {"owner_id", "export_reference", "source_reference", "data"}
                or grant["owner_id"] != principal.user_id):
            raise PlatformError("QLIB_EXPORT_AUTHORIZATION_INVALID")
        self.runtime.require_owner(principal, principal.user_id)
        qlib_workflow.reference(grant["export_reference"])
        qlib_workflow.reference(grant["source_reference"])
        qlib_workflow.validate_input(grant["data"])
        if grant["data"]["native_dump_succeeded"] is not True:
            raise PlatformError("QLIB_NATIVE_DUMP_NOT_READY")
        from finance_platform.data.qlib_export_authorizer import QlibExportAuthorizer
        adapter = getattr(self.runtime, "qlib_export_authorizer", None)
        if type(adapter) is not QlibExportAuthorizer:
            raise PlatformError("QLIB_AUTHORIZED_EXPORT_RESOLVER_REQUIRED")
        current = adapter.authorize(principal, deepcopy(grant["export_reference"]), purpose=purpose)
        if qlib_workflow.digest(current) != qlib_workflow.digest(grant):
            raise PlatformError("QLIB_EXPORT_AUTHORIZATION_CHANGED")
        if source_digest(self.material.root) != self.material.source_digest:
            raise PlatformError("RESEARCH_SERVER_SOURCE_MISMATCH")
        native_hash = current["data"]["native_manifest_hash"]
        security = self._review("security", native_hash)
        financial = self._review("financial", native_hash)
        record = read_metadata(self.material.root, self.material.worker_binding)
        proof = read_metadata(self.material.root, self.material.probe_completion)
        arguments, mapping = proof["arguments"], proof["binding_mapping"]
        completion = QlibProbeCompletion(worker_root=Path(arguments["worker_root"]), **{
            name: _file_pin(arguments[name]) for name in ("result", "request", "stdout", "stderr", "worker_spec", "identity_probe")})
        if (Path(arguments["worker_root"]) != self.material.root / "workers/qlib"
                or mapping["worker_binding"] != {"path": str(self.material.root / self.material.worker_binding.path),
                    "bytes": self.material.worker_binding.bytes, "sha256": self.material.worker_binding.sha256}
                or mapping["worker_lock"] != record["lock"]):
            raise PlatformError("QLIB_STATIC_PROOF_INVALID")
        contract = Path(qlib_workflow.__file__).absolute()
        def source_pin(path):
            raw = path.read_bytes()
            import hashlib
            return FilePin(path, len(raw), hashlib.sha256(raw).hexdigest())
        binding = QlibBinding(**{name: _file_pin(mapping[name]) for name in (
            "worker_python", "worker_manifest", "worker_lock", "worker_binding", "runtime_identity")},
            entry=source_pin(contract.with_name("qlib_child.py")), contract_source=source_pin(contract),
            bootstrap=source_pin(Path(__file__).absolute().with_name("qlib_bootstrap.py")),
            runtime_files=tuple(_file_pin(row) for row in mapping["runtime_files"]),
            security_receipt=security, financial_receipt=financial,
            source_manifest_sha256=self.material.source_digest, native_manifest_hash=native_hash, probe_completion=completion)
        # Strict project/source/worker/probe checks remain. No stage is required
        # here: current run preparation and stage are checked later by execute.
        return project_qlib_launcher(self.runtime, binding)

def project_qlib_dump_deployment(runtime, material):
    """Build static typed official-dump identity only with installed review.

    No native hash/owner/export/run is a deployment field. The data component
    supplies those per actual attempt. Missing review means unconfigured.
    """
    if type(material) is not QlibServerMaterial or material.root != Path(runtime.root).resolve():
        raise PlatformError("QLIB_SERVER_MATERIAL_INVALID")
    if material.dump_review is None:
        return None
    from finance_platform.data.qlib_dump_deployment import QlibDumpDeployment
    from .source_binding import checked_regular_file
    proof=read_metadata(material.root,material.probe_completion)
    arguments,mapping=proof["arguments"],proof["binding_mapping"]
    completion=QlibProbeCompletion(worker_root=Path(arguments["worker_root"]),**{
        name:_file_pin(arguments[name]) for name in ("result","request","stdout","stderr","worker_spec","identity_probe")})
    def code_pin(relative):
        path=checked_regular_file(material.root,relative)
        import hashlib
        raw=path.read_bytes()
        return FilePin(path,len(raw),hashlib.sha256(raw).hexdigest())
    names=("qlib_dump.py","qlib_dump_child.py","qlib_dump_domain.py","qlib_dump_trace.py","qlib_dump_deadline.py", "qlib_dump_deployment.py",
        "qlib_preparation.py","qlib_export_authorizer.py","qlib_input_bridge.py","qlib_export.py","parquet_store.py",
        "qlib_dump_pool_gate.py","qlib_dump_optional_ipv6.py")
    review=read_metadata(material.root,material.dump_review)
    if review.get("schema")!="qlib-synthetic-dump-profile-execution-1":
        raise PlatformError("QLIB_DUMP_PROFILE_EXECUTION_REVIEW_REQUIRED")
    deployment=QlibDumpDeployment(root=material.root,
        **{name:_file_pin(mapping[name]) for name in ("worker_python","worker_manifest","worker_lock","worker_binding","runtime_identity")},
        runtime_files=tuple(_file_pin(row) for row in mapping["runtime_files"]),probe_completion=completion,
        official_dump=code_pin("vendor/qlib/scripts/dump_bin.py"),bootstrap=code_pin("src/finance_platform/core/qlib_bootstrap.py"),
        profile_source=code_pin("src/finance_platform/core/qlib_process.py"),
        source_pins=tuple(code_pin("src/finance_platform/data/"+name) for name in names),
        execution_review=FilePin(material.root/material.dump_review.path,material.dump_review.bytes,material.dump_review.sha256))
    deployment.verify()
    return deployment

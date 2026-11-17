"""Finite primitive native-output JSON, separate from Decimal input fingerprints."""
from .payload_storage import persist as store_payload, hydrate as load_payload, task_binding
import hashlib
import math
import json
from finance_platform.core.security import now_text
from finance_platform.core.security import safe_json
from .data import ResearchError

FAMILIES=frozenset({"qlib-workflow-result-1","tradingagents-native-report-1"})


def _primitive(value,depth=0):
    if depth>20:raise ResearchError("NATIVE_RESULT_DEPTH_INVALID")
    if type(value)is dict:
        if any(type(key)is not str for key in value):raise ResearchError("NATIVE_RESULT_KEY_INVALID")
        for item in value.values():_primitive(item,depth+1)
    elif type(value)is list:
        for item in value:_primitive(item,depth+1)
    elif type(value)is float:
        if not math.isfinite(value):raise ResearchError("NATIVE_RESULT_NONFINITE")
    elif value is not None and type(value)not in (str,bool,int):raise ResearchError("NATIVE_RESULT_NON_PRIMITIVE")


def encode(value):
    _primitive(value)
    # Reuse existing canonical JSON order, finite-number, size, secret-field
    # and scoped-secret checks. No core payload/fingerprint relaxation.
    return safe_json(value)


def digest(value):return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def finite_number(value,*,nullable=False):
    if nullable and value is None:return
    if type(value)not in (int,float) or not math.isfinite(value):raise ResearchError("NATIVE_RESULT_NUMBER_INVALID")


def qlib_numbers(value):
    from .qlib_workflow import validate_numeric_result
    validate_numeric_result(value)


def prepare_stored_result(research, context, value):
    return store_payload(research.runtime, task_binding(context), "reports", encode(value))


def insert_result(research,context,value,*,sample_hash,strategy_hash,connection,stored_encoded):
    """Existing table/transaction; caller already validates native source grant.

    This boundary adds no native success authority. Reads still join the actual
    core SUCCEEDED row. The same path is exercised with float shape fixtures.
    """
    if value.get("family") not in FAMILIES:raise ResearchError("NATIVE_RESULT_FAMILY_INVALID")
    if value.get("family")=="qlib-workflow-result-1":qlib_numbers(value)
    if value.get("run_id")!=context.run.run_id or value.get("job_id")!=context.job_id:
        raise ResearchError("NATIVE_RESULT_RUN_BINDING_INVALID")
    context.assert_fenced(connection)
    research.runtime.require_owner(research.principal(context.run.owner_id),context.run.owner_id,write=True)
    encoded=encode(value);result_hash=digest(json.loads(encoded))
    if load_payload(research.runtime, context.run.owner_id, "reports", stored_encoded) != json.loads(encoded):
        raise ResearchError("NATIVE_RESULT_STORAGE_MISMATCH")
    connection.execute("INSERT INTO research_results VALUES(?,?,?,?,?,?,?,?)",(context.run.run_id,context.run.owner_id,
        context.job_id,sample_hash,strategy_hash,stored_encoded,result_hash,now_text()))
    return result_hash

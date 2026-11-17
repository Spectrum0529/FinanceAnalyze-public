"""Trusted one-time final export/seal; no model port or guessed receipt digest."""
import json
from .errors import PlatformError


def final_scope(service,context,begun):
    with service.database.transaction()as con:
        attempt,session=service._attempt(con,context)
        spec=json.loads(session['spec_json']);resolved=json.loads(session['resolved_json'])
        if (attempt['phase']!='final_test' or attempt['status']!='IN_FLIGHT'
                or session['final_attempt']!=attempt['attempt_id']
                or begun['phase']!='final_test' or begun['attempt_id']!=attempt['attempt_id']
                or not session['candidate_hash'] or begun.get('candidate_hash')!=session['candidate_hash']
                or spec.get('final_test') is None or begun['spec'].get('final_test')!=spec['final_test']
                or begun['resolved'].get('final_windows')!=resolved['final_windows']):
            raise PlatformError('FINAL_SEAL_BINDING_MISMATCH',409)
        events=con.execute("SELECT metadata_json FROM autonomous_events WHERE session_id=? AND kind='FINAL_EXPOSED'",
                           (session['session_id'],)).fetchall()
        exposed=[json.loads(row[0])for row in events if json.loads(row[0]).get('attempt_id')==attempt['attempt_id']]
        if (len(exposed)!=1 or exposed[0].get('candidate_hash')!=session['candidate_hash']
                or exposed[0].get('input_hash')!=spec['final_test']['content_hash']):
            raise PlatformError('FINAL_SEAL_BINDING_MISMATCH',409)
    return spec,resolved


def receipt_contract():
    try:
        from finance_platform.data import autonomous_receipts
        return autonomous_receipts
    except ImportError:
        raise PlatformError('AUTONOMOUS_FINAL_UNAVAILABLE',503) from None


def prepare_final(runtime,service,inputs,context,begun):
    receipts=receipt_contract()
    final_scope(service,context,begun)
    principal=runtime.research_task_service.principal(context.run.owner_id)
    library=runtime.research_candidates
    # The descriptor proves immutable source, development checkpoint/options/
    # thresholds BEFORE exporting any final values. It is not a runnable seal.
    descriptor=library.final_descriptor(principal,begun['session_id'],begun['attempt_id'])
    packages=inputs.export(context,begun)
    from finance_platform.data.research_inputs import verify_research_input
    for package in packages:verify_research_input(package)
    # FA015 packages the same standard-library source on Windows and Linux.
    try:
        receipt_hash=receipts.digest(receipts.package_receipts(packages))
    except receipts.ReceiptRejected as error:
        raise PlatformError(str(error),422) from None
    seal=library.final_seal(principal,begun['session_id'],begun['attempt_id'],
                          final_input_receipts_sha256=receipt_hash,warmup_rows=descriptor['warmup_rows'])
    if (seal.get('final_input_receipts_sha256')!=receipt_hash
            or {k:v for k,v in seal.items()if k!='final_input_receipts_sha256'}!=descriptor):
        raise PlatformError('FINAL_SEAL_BINDING_MISMATCH',409)
    final_scope(service,context,begun)
    return seal,packages

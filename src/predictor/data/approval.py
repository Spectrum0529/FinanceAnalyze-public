"""Fail-closed formal-research restrictions for a new installation.

This public template carries requirements, not supplier receipts or research
history. Workbench downloads keep their separate source and owner permissions.
"""
import argparse
import hashlib
import json
from pathlib import Path
from predictor.access import AccessPolicy

USES = ('internal_research', 'raw_storage', 'post_expiry_retention', 'model_input', 'model_output', 'redistribution')
GROUPS = {'identity', 'auction', 'daily', 'actions', 'terminal', 'calendar', 'industry', 'lineage'}
INPUTS = {'requirements': 'resources/data_requirements.json', 'calendar': 'configs/calendar.lock.json'}
SYNTHETIC = 'samples/public/synthetic_market/events.json'
LOCK = 'configs/data_sources.lock.json'
REVISION = 'formal-research-unverified-1'


class ApprovalError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require_shape(requirements):
    if (type(requirements) is not dict or requirements.get('schema_version') != 1
            or requirements.get('contract_version') != 'core-market-fields-1'
            or requirements.get('data_certification') != 'UNVERIFIED'
            or requirements.get('formal_research_eligible') is not False):
        raise ApprovalError('Unverified public requirements template required')
    fields = requirements.get('fields')
    keys = {'id', 'group', 'name', 'unit', 'required'}
    if (type(fields) is not list or len(fields) != 100
            or any(type(f) is not dict or set(f) != keys for f in fields)
            or any(type(f[k]) is not str or not f[k] for f in fields for k in ('id', 'group', 'name', 'unit'))
            or len({f['id'] for f in fields}) != 100 or {f['group'] for f in fields} != GROUPS
            or any(f['required'] is not True for f in fields)):
        raise ApprovalError('The complete 100-field core contract is required')


def build_restrictions(inputs, bindings):
    requirements = inputs['requirements']
    require_shape(requirements)
    # No claim or statistic in this template can enable a market use.
    # Positive certification requires a separately reviewed implementation.
    return {
        'schema_version': 1, 'contract_revision': REVISION,
        'scope': 'FORMAL_RESEARCH_CERTIFICATION_ONLY',
        'lock_kind': 'REQUIREMENTS_WITHOUT_APPROVED_MARKET_EVIDENCE',
        'data_certification': 'UNVERIFIED', 'package_approved': False,
        'formal_research_generation_enabled': False, 'approved_market_sources': [], 'approved_history': None,
        'input_bindings': bindings,
        'required_field_contract': [{**f, 'certified_for_market_research': False,
            'disposition': 'CONTROL_ONLY' if f['group'] == 'calendar' else 'BLOCK_FORMAL_USE'}
            for f in requirements['fields']],
        'provider_use_matrix': [], 'existing_development_samples': [],
        'market_uses': {use: {'allowed': False, 'status': 'EVIDENCE_NOT_CONFIGURED'} for use in USES},
        'allowed_nonmarket_work': {
            'synthetic_engineering': {'allowed': True, 'source': bindings['synthetic_fixture'], 'market_substitution_allowed': False},
            'calendar_clock_control': {'allowed': True, 'calendar': bindings['calendar'], 'market_data_license_granted': False}},
        'availability_assumptions': {'historical_first_available_at': None,
            'historical_latency_seconds': None, 'publication_timestamp_inferred': False,
            'receipt_time_backdated': False, 'date_only_treated_as_exact': False},
        'quality_disposition': {'no_price_fill': True, 'no_unknown_recovery_zero': True,
            'no_daily_open_auction_substitution': True, 'no_cross_date_identity_or_industry_backfill': True},
        'approval_process': {'positive_approval_supported_in_this_revision': False,
            'new_evidence_requires_new_revision_and_run': True, 'user_continue_is_not_license_evidence': True,
            'holdout_access_enabled': False, 'procurement_or_contract_acceptance_authorized': False}}


def load_inputs(root):
    policy, values, bindings = AccessPolicy(root), {}, {}
    for name, path in INPUTS.items():
        raw = policy.authorize(path, operation='config_read').read_bytes()
        values[name], bindings[name] = json.loads(raw), {'path': path, 'sha256': sha(raw)}
    raw = policy.read_bytes(SYNTHETIC)
    fixture = json.loads(raw)
    if fixture.get('scope') != 'synthetic_only' or fixture.get('uses_market_data') is not False:
        raise ApprovalError('Only explicit nonmarket fixture data is allowed')
    bindings['synthetic_fixture'] = {'path': SYNTHETIC, 'sha256': sha(raw)}
    return values, bindings


def verified_contract(root, path=LOCK):
    policy = AccessPolicy(root)
    raw = policy.authorize(path, operation='config_read').read_bytes()
    value = json.loads(raw)
    inputs, bindings = load_inputs(root)
    if canonical(value) != canonical(build_restrictions(inputs, bindings)):
        raise ApprovalError('Restriction template/input mismatch; new reviewed revision required')
    return value


def require_use(contract, source, use):
    if (contract.get('contract_revision') != REVISION or contract.get('package_approved') is not False
            or contract.get('formal_research_generation_enabled') is not False
            or contract.get('approved_market_sources') != []):
        raise ApprovalError('Unsupported or altered formal-research restrictions')
    if source == 'project-synthetic' and use == 'synthetic_engineering':
        return {'allowed': True, 'scope': 'NONMARKET_ENGINEERING_ONLY'}
    if source == 'calendar' and use == 'calendar_clock_control':
        return {'allowed': True, 'scope': 'CLOCK_CONTROL_ONLY'}
    if use in USES or use == 'formal_historical_construction':
        raise ApprovalError('DATA_PACKAGE_NOT_APPROVED: formal research and certified source uses are blocked')
    raise ApprovalError('Unknown source/use or no locally reviewed evidence')


def main():
    parser = argparse.ArgumentParser(description='Read-only formal research permission check')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--lock', default=LOCK)
    parser.add_argument('--source', default='nyse')
    parser.add_argument('--use', default='formal_historical_construction')
    args = parser.parse_args()
    try:
        contract = verified_contract(args.root.resolve(), args.lock)
        print(json.dumps(require_use(contract, args.source, args.use), ensure_ascii=False))
        return 0
    except (ApprovalError, ValueError, OSError):
        print(json.dumps({'allowed': False, 'reason': 'DATA_PACKAGE_NOT_APPROVED'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

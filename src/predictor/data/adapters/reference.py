"""NYSE evaluation reference observations; no historical universe reconstruction."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

from predictor.contracts.records import RawRecord, TIME_FIELDS
from predictor.contracts.snapshot import content_hash
from predictor.data.acquisition import _immutable, json_bytes
from predictor.data.reference_acquisition import replay_reference
from predictor.logging import utc_timestamp


class ReferenceError(ValueError):
    pass


def source_date(value):
    if not value:
        return None
    try:
        result = datetime.strptime(value, '%m/%d/%Y').date()
    except ValueError:
        raise ReferenceError('Malformed source date') from None
    return result.isoformat()


def number(value, *, positive=False):
    if not value:
        return None
    try:
        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', value):
            raise InvalidOperation
        result = Decimal(value)
        if not result.is_finite() or (positive and result <= 0):
            raise InvalidOperation
    except InvalidOperation:
        raise ReferenceError('Malformed source numeric field') from None
    return str(result)


def identifier(value, pattern, field):
    if value and not re.fullmatch(pattern, value):
        raise ReferenceError('Invalid ' + field)
    return value or None


def parse_reference(raw, manifest, *, processed_at):
    kind = manifest['kind']
    day = date.fromisoformat(manifest['sample_date']).isoformat()
    if hashlib.sha256(raw).hexdigest() != manifest['raw_response_hash']:
        raise ReferenceError('Source hash mismatch')
    rows = []
    if kind == 'master':
        # Latin-1 is a reversible byte map, NOT a certified source charset.
        text = raw.decode('latin-1')
        for row_number, line in enumerate(text.split('\n'), 1):
            if not line.strip():
                continue
            fields = line.removesuffix('\r').split('|')
            if len(fields) != 157:
                raise ReferenceError('Master requires the observed 157-field full-file layout')
            stock = identifier(fields[0], r'[0-9]{1,5}', 'stock number')
            if not stock or not 1 <= int(stock) <= 80000 or not fields[1].strip():
                raise ReferenceError('Master security identity missing')
            selected = [*range(12), *range(106, 113)]
            source = {str(i + 1): fields[i] for i in selected}
            payload = {'kind': kind, 'source_schema': 'NYSE_EQUITY_4.0_157_fields',
                'source_security_id': 'nyse-stock-number:' + str(int(stock)), 'issuer_id': None,
                'issuer_name': fields[5], 'security_name': fields[6], 'symbol': fields[1],
                'sip_symbol': fields[2], 'cusip': identifier(fields[3], r'[A-Z0-9]{9}', 'CUSIP'),
                'isin': identifier(fields[4], r'[A-Z0-9]{12}', 'ISIN'), 'security_type': fields[8],
                'primary_market': fields[11] or None, 'test_symbol': fields[9],
                'snapshot_date': day, 'old_stock_symbol': fields[110] or None,
                'symbol_reuse_flag': fields[109] or None, 'split_flag': fields[107] or None,
                'source_encoding': 'LATIN1_REVERSIBLE_BYTE_MAP_UNVERIFIED_CHARSET',
                'event_type': 'identity_snapshot', 'event_date': day,
                'published_date': None, 'revision_date': None, 'source_fields': source,
                'source_row_number': row_number, 'source_row_hash': hashlib.sha256(line.removesuffix('\r').encode('latin-1')).hexdigest()}
            rows.append((payload['source_security_id'], payload))
    elif kind in {'ticker', 'distribution', 'info'}:
        try:
            text = raw.decode('utf-8-sig')
            if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I):
                raise ReferenceError('DTD/entities forbidden')
            xml = ET.fromstring(text)
        except (UnicodeError, ET.ParseError):
            raise ReferenceError('Invalid UTF-8 reference XML') from None
        shapes = {'ticker': ('NYSE_TICKER_NOTICES_INTRADAY', 'NYSE_TICKER_NOTICES_INTRADAY'),
            'distribution': ('CorporateActions', 'CorporateAction'), 'info': ('InfoNotices', 'InfoNotice')}
        root_tag, row_tag = shapes[kind]
        if xml.tag != root_tag or xml.attrib:
            raise ReferenceError('Unexpected reference XML schema')
        for child in xml:
            if child.tag != row_tag or child.attrib:
                raise ReferenceError('Unexpected reference row schema')
            source = {}
            for field in child:
                if field.tag in source or len(field) or field.attrib:
                    raise ReferenceError('Duplicate/nested source field')
                source[field.tag] = field.text or ''
            required = {'ticker': {'NoticeID', 'NoticeDate', 'ActionDate', 'ActionTypeCode', 'SecuritySymbol', 'CUSIP', 'IssuerName'},
                'distribution': {'DividendID', 'Dividend_Type', 'Ex_Date', 'Security_Symbol', 'CUSIP', 'Issuer_Name', 'Cancel_Flag'},
                'info': {'Notice_Date', 'Company', 'Notice_Type', 'Notice_Body'}}[kind]
            if not required <= source.keys():
                raise ReferenceError('Required reference field missing')
            payload = {'kind': kind, 'source_schema': 'NYSE_CA_v3_family_observed_' + kind,
                'source_security_id': None, 'issuer_id': None, 'source_fields': source,
                'revision_date': None, 'snapshot_date': day}
            if kind == 'info':
                clock = re.fullmatch(r'(\d{1,2}/\d{1,2}/\d{4}) (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)', source['Notice_Date'])
                if not clock:
                    raise ReferenceError('Malformed timezone-unspecified notice time') from None
                hour, minute, second = map(int, clock.group(2, 3, 4))
                if not (1 <= hour <= 23 and minute < 60 and second < 60) or (hour > 12 and clock[5] != 'PM'):
                    raise ReferenceError('Malformed timezone-unspecified notice time')
                clock_quality = 'NONSTANDARD_24H_WITH_PM_SOURCE_LITERAL' if hour > 12 else 'TIMEZONE_UNSPECIFIED_SOURCE_LITERAL'
                payload.update(issuer_name=source['Company'], symbol=None, cusip=None,
                    event_type=source['Notice_Type'], event_date=None, published_date=source_date(clock[1]),
                    notice_time_literal=source['Notice_Date'], notice_timezone=None,
                    notice_clock_quality=clock_quality, narrative_html=source['Notice_Body'], terminal_recovery=None)
                # Provider supplies no notice ID. This is a local grouping key only.
                logical = 'local-info:' + content_hash({k: source[k] for k in ('Notice_Date', 'Company', 'Notice_Type')})
                payload['native_event_id'] = None
            else:
                distribution = kind == 'distribution'
                event_id = source['DividendID' if distribution else 'NoticeID']
                if not event_id.strip():
                    raise ReferenceError('Source event ID missing')
                symbol = source['Security_Symbol' if distribution else 'SecuritySymbol']
                if not symbol.strip():
                    raise ReferenceError('Source symbol missing')
                payload.update(native_event_id=event_id, issuer_name=source['Issuer_Name' if distribution else 'IssuerName'],
                    symbol=symbol, cusip=identifier(source['CUSIP'], r'[A-Z0-9]{9}', 'CUSIP'),
                    event_type=source['Dividend_Type' if distribution else 'ActionTypeCode'],
                    event_date=source_date(source['Ex_Date' if distribution else 'ActionDate']),
                    published_date=source_date(source.get('Announcement_Date') if distribution else source['NoticeDate']))
                logical = 'nyse-' + kind + ':' + event_id
                if distribution:
                    flag = source['Cancel_Flag']
                    if flag not in {'Y', 'N', ''}:
                        raise ReferenceError('Unknown cancellation flag')
                    pre, post = (number(source.get(key), positive=True) for key in ('Pre_Split_Shares', 'Post_Split_Shares'))
                    if bool(pre) != bool(post):
                        raise ReferenceError('Incomplete split ratio')
                    payload.update(cash_amount=number(source.get('Total_Dividend_Amount')), cash_currency=source.get('Dividend_Currency') or None,
                        pre_split_shares=pre, post_split_shares=post, split_ratio_direction='post_over_pre',
                        cancelled=flag == 'Y', cancelled_date=source_date(source.get('Cancelled_Date')),
                        declared_date=source_date(source.get('Declared_Date')), record_date=source_date(source.get('Record_Date')),
                        pay_date=source_date(source.get('Pay_Date')), revision_date=source_date(source.get('Updated_Date')))
                else:
                    payload.update(notes=source.get('Notes'), terminal_recovery=None)
            rows.append((logical, payload))
    else:
        raise ReferenceError('Unknown reference kind')
    if not rows:
        raise ReferenceError('Empty reference source')
    records, seen, logical_master, duplicates = [], set(), {}, 0
    for logical, payload in rows:
        row_number = payload.pop('source_row_number', None)
        digest = content_hash(payload)
        if kind == 'master' and logical in logical_master and logical_master[logical] != digest:
            raise ReferenceError('Conflicting security ID within master snapshot')
        logical_master[logical] = digest
        key = (logical, digest)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        payload.update(formal_research_eligible=False, historical_identity_join_eligible=False,
            issuer_identity_status='UNRESOLVED_SOURCE_HAS_NO_ISSUER_ID')
        quality = {k: 'missing' for k in TIME_FIELDS}
        times = {k: None for k in TIME_FIELDS}
        for field, value in [('event_time', payload['event_date']), ('published_at', payload['published_date']),
                             ('revision_time', payload['revision_date'])]:
            times[field] = value
            quality[field] = 'date_only' if value else 'missing'
        times.update(ingested_at=manifest['response_received_at'], processed_at=processed_at)
        quality.update(ingested_at='exact', processed_at='exact')
        identity = payload['source_security_id'] or 'unresolved-observation:' + content_hash({'logical': logical, 'date': day, 'digest': digest})
        meta = {'record_id': logical + ':' + digest, 'security_id': identity, 'source_id': 'nyse-reference-' + kind,
            **times, 'version': 1, 'content_hash': content_hash(payload), 'license_id': 'public-evaluation-broader-rights-unverified',
            'timestamp_quality': quality, 'availability_basis': 'live_observed', 'availability_evidence': {
                'publication_reference': None, 'vendor_reference': None, 'latency_assumption': None, 'official_time_references': {}}}
        RawRecord.from_mapping(meta)
        records.append({'logical_event_id': logical, 'observation_hash': digest, 'source_row_number': row_number,
            'metadata': meta, 'payload': payload})
    return {'kind': kind, 'row_count': len(records), 'duplicates_ignored': duplicates, 'records': records,
        'data_certification': 'UNVERIFIED', 'formal_research_eligible': False, 'model_evaluation': 'NOT_RUN'}


def resolve_snapshot_symbol(records, symbol, snapshot_date, *, cusip=None):
    """Only exact dated master evidence; never apply today's mapping to another day."""
    day = date.fromisoformat(snapshot_date).isoformat()
    matches = [r['payload'] for r in records if r['payload']['kind'] == 'master'
        and r['payload']['snapshot_date'] == day and r['payload']['symbol'] == symbol
        and (cusip is None or r['payload']['cusip'] == cusip)]
    identities = {p['source_security_id'] for p in matches}
    if len(identities) != 1:
        raise ReferenceError('No unique dated security mapping')
    return next(iter(identities))


def replay_references(policy, directory):
    raw, manifest = replay_reference(policy, directory)
    directory = policy.authorize(directory)
    binding = {'raw_response_hash': manifest['raw_response_hash'],
        'parser_hash': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    stamp_path = directory / ('processing-' + binding['parser_hash'] + '.json')
    if stamp_path.exists():
        stamp = json.loads(policy.read_bytes(stamp_path))
        if any(stamp.get(k) != value for k, value in binding.items()):
            raise ReferenceError('Processing binding changed')
    else:
        if not directory.is_relative_to(policy.partitions['development']):
            raise ReferenceError('Processing journal must be in development')
        stamp = {**binding, 'processed_at': utc_timestamp()}
    result = parse_reference(raw, manifest, processed_at=stamp['processed_at'])
    _immutable(policy, stamp_path, json_bytes(stamp))
    return result

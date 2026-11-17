"""Authorized Qlib input preparation; official native dump is a separate step.

No Qlib import/process/network or binary format encoder in the platform runtime.
Current executable preparation supports only the installed synthetic fixture;
real adjusted-provider basis/unknown volume is explicitly rejected.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import random
from uuid import uuid4

from finance_platform.core.contracts import canonical, fingerprint, identifier
from finance_platform.core.database import immutable
from finance_platform.data.contracts import DataError
from .parquet_store import ParquetRows
from .provider_sdk.artifacts import regular_owned

FIXTURE_ID = 'FA014-QLIB-US-OFFLINE-V2'
SEED = 14027
SYMBOLS = tuple('SYNTH'+c for c in 'ABCDEFGH')
BENCHMARK = 'SYNTHBM'
ORIGIN = 'SYNTHETIC_ONLY_NOT_PROVIDER_DATA'
POLICY = 'SYNTHETIC_QLIB_NORMALIZED_OHLCV_V1'
COLUMNS = ('symbol','date','open','high','low','close','vwap','volume','factor')
MIGRATIONS = ("""CREATE TABLE data_qlib_input_exports(
 export_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,run_id TEXT NOT NULL,
 body TEXT NOT NULL,content_hash TEXT NOT NULL);""",)


@dataclass(frozen=True)
class HistorySelection:
    history_id: str
    version: int
    start: str
    end: str


def synthetic_fixture(fixture_id=FIXTURE_ID):
    if fixture_id != FIXTURE_ID:
        raise DataError('QLIB_FIXTURE_NOT_ALLOWED')
    calendar=[]; day=date(2023,1,3)
    while len(calendar)<420:
        if day.weekday()<5:calendar.append(day.isoformat())
        day+=timedelta(days=1)
    instruments={}
    with localcontext() as ctx:
        ctx.prec=50
        for number,symbol in enumerate((*SYMBOLS,BENCHMARK)):
            rng=random.Random(SEED+1009*number)
            rows=[]
            for index,day in enumerate(calendar):
                noise=Decimal(rng.randint(-25,25))/100 if index else Decimal(0)
                base=Decimal(100)+Decimal(index)*Decimal('0.03')+Decimal(number)*Decimal(index)*Decimal('0.001')+noise
                split=(symbol=='SYNTHA')
                action=Decimal('0.5') if split and index<210 else Decimal(1)
                raw_scale=Decimal(2) if split and index<210 else Decimal(1)
                raw={key:base*raw_scale+spread*raw_scale for key,spread in
                     [('open',Decimal('-0.10')),('high',Decimal('0.40')),('low',Decimal('-0.40')),('close',Decimal(0))]}
                factor=action/Decimal(100)
                raw_volume=Decimal(1000+3*index+17*number)*(2 if split and index>=210 else 1)
                # Four explicit synthetic prints. Unequal quantity weights
                # define VWAP; no real close/typical-price fallback.
                prints=[{'price':format(raw[key],'f'),'quantity':format(raw_volume*Decimal(weight)/10,'f')}
                        for key,weight in [('open',2),('high',1),('low',1),('close',6)]]
                raw['vwap']=sum(Decimal(p['price'])*Decimal(p['quantity']) for p in prints)/raw_volume
                row={'session_date':day,'symbol':symbol, 'synthetic_trades':prints,
                    **{k:format(v*factor,'f') for k,v in raw.items()},
                    'volume':format(raw_volume/factor,'f'),'factor':format(factor,'f'),
                    'raw_ohlcv':{**{k:format(v,'f') for k,v in raw.items()},'volume':format(raw_volume,'f')},
                    'origin':ORIGIN,'fixture_id':FIXTURE_ID,'price_policy':POLICY,
                    'price_unit':'SYNTHETIC_USD_PER_ADJUSTED_UNIT','volume_unit':'SYNTHETIC_NORMALIZED_QUANTITY',
                    'quantity_basis':'SYNTHETIC_ADJUSTED_UNITS', 'vwap_evidence':'WEIGHTED_FOUR_SYNTHETIC_PRINTS_NOT_PROVIDER_VWAP',
                    'raw_volume_unit':'SYNTHETIC_SHARES','currency':'USD','raw_compatible':False,
                    'source_timestamp':day+'T21:00:00Z','available_at':day+'T21:00:01Z',
                    'availability_basis':'SYNTHETIC_CLOCK_NOT_MARKET_PIT','calendar_id':'SYNTHETIC_WEEKDAY_UTC_2100',
                    'label_mature_at':calendar[index+2]+'T21:00:01Z' if index+2<len(calendar) else None}
                # Intentionally absent observation; no exporter forward/back fill.
                if not (symbol=='SYNTHC' and index==180):rows.append(row)
            instruments[symbol]=rows
    return {'fixture_id':FIXTURE_ID,'seed':SEED,'origin':ORIGIN,'calendar':calendar,
        'instruments':instruments,'universe':list(SYMBOLS),'benchmark':BENCHMARK,
        'semantics':{'price_policy':POLICY,'normalization':'raw_price*A/100; factor=A/100; volume=raw_shares/factor',
            'split':{'symbol':'SYNTHA','index':210,'ratio':'2'},
            'raw_claim':'SYNTHETIC_DECLARATION_ONLY_NO_REAL_PROVIDER_CERTIFICATION',
            'native_dump_dtype':'official little-endian float32',
            'native_comparison_tolerance':{'rtol':'0.0000001','atol':'0.0000001'}},
        'split_indices':{'index_convention':'0-based half-open','warmup':[0,60],'train':[60,280],
            'valid':[280,340],'test_predictions':[340,400],'maturity_execution_buffer':[400,420],
            'effective_train_calendar_indices':[60,278],'effective_valid_calendar_indices':[280,338],
            'mature_label_calendar_day_ceiling':{'train':218,'valid':58,'test_predictions':60},
            'label_expression':'Ref($close,-2)/Ref($close,-1)-1','label_horizon_sessions':2,
            'purge':'Train t+2<=D279 and valid t+2<=D339: exclude last2 nominal days once. Test t340..399 may mature throughD401 using buffer only. Missing observations/feature/label Dropna can reduce actual samples; never claim220/60 effective label days. No buffer/warmup fit or tuning.'}}


def validate_fixture(fixture):
    # Trusted installed fixture path only; a client synthetic flag is never a
    # way to certify real-provider prices, volume or factor.
    if (fixture.get('fixture_id')!=FIXTURE_ID or fixture.get('origin')!=ORIGIN
            or fixture.get('seed')!=SEED or fixture.get('universe')!=list(SYMBOLS)
            or fixture.get('benchmark')!=BENCHMARK or set(fixture.get('instruments',{}))!=set((*SYMBOLS,BENCHMARK))):
        raise DataError('QLIB_SYNTHETIC_BINDING_INVALID')
    calendar=fixture['calendar']
    if len(calendar)!=420 or calendar!=sorted(set(calendar)):
        raise DataError('QLIB_CALENDAR_INVALID')
    for day in calendar:
        if date.fromisoformat(day).isoformat()!=day:raise DataError('QLIB_CALENDAR_INVALID')
    for symbol,rows in fixture['instruments'].items():
        if not rows:raise DataError('QLIB_WINDOW_EMPTY')
        dates=[r['session_date'] for r in rows]
        if dates!=sorted(set(dates)) or not set(dates)<=set(calendar):raise DataError('QLIB_DUPLICATE_OR_DATE_INVALID')
        for row in rows:
            if (row.get('origin')!=ORIGIN or row.get('fixture_id')!=FIXTURE_ID or row.get('symbol')!=symbol
                    or row.get('price_policy')!=POLICY or row.get('volume_unit')!='SYNTHETIC_NORMALIZED_QUANTITY'
                    or row.get('raw_volume_unit')!='SYNTHETIC_SHARES'):
                raise DataError('QLIB_BASIS_UNSUPPORTED')
            try:
                factor=Decimal(row['factor'])
                values={k:Decimal(row[k]) for k in ('open','high','low','close','vwap','volume')}
                raw={k:Decimal(row['raw_ohlcv'][k]) for k in values}
                prints=row['synthetic_trades']
                if (len(prints)!=4 or sum(Decimal(p['quantity']) for p in prints)!=raw['volume']
                        or sum(Decimal(p['price'])*Decimal(p['quantity']) for p in prints)/raw['volume']!=raw['vwap']):
                    raise ValueError
                if (not factor.is_finite() or factor<=0 or any(not x.is_finite() for x in (*values.values(),*raw.values()))
                        or any(values[k]<=0 for k in ('open','high','low','close','vwap')) or values['volume']<0
                        or values['high']<max(values[k] for k in ('open','low','close','vwap'))
                        or values['low']>min(values[k] for k in ('open','high','close','vwap'))
                        or any(values[k]!=raw[k]*factor for k in ('open','high','low','close','vwap'))
                        or values['volume']*factor!=raw['volume']):
                    raise ValueError
            except (ValueError,KeyError,ArithmeticError):raise DataError('QLIB_FACTOR_OR_UNIT_INVALID') from None
    fingerprint(fixture)  # Original canonical/type/credential validation.


class QlibInputExporter:
    def __init__(self,database,policy,history_store=None):
        self.db,self.policy,self.history_store=database,policy,history_store
        self.parquet=ParquetRows(policy)
        self.db.apply_module_migrations('data_qlib_inputs_1',MIGRATIONS)
        with self.db.transaction() as con:immutable(con,'data_qlib_input_exports',('export_id',))

    def _directory(self,owner,run_id,export_id):
        for key in [owner,run_id,export_id]:identifier(key)
        path=self.policy.partitions['development']/'platform'/owner/'qlib-exports'/run_id/export_id
        for item in (path,*path.parents):
            if item.exists() or item.is_symlink():
                info=item.lstat()
                if item.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:raise DataError('QLIB_EXPORT_PATH_INVALID')
            if item==self.policy.root:break
        if self.policy.authorize(path)!=path:raise DataError('QLIB_EXPORT_PATH_INVALID')
        return path

    def _write(self,directory,name,raw):
        path=self.policy.authorize(directory/name)
        with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        regular_owned(directory,name)
        checked=self.policy.read_bytes(path)
        if checked!=raw:raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        return {'name':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}

    def prepare_fixture(self,context,*,fixture_id=FIXTURE_ID):
        context.require_active()
        fixture=synthetic_fixture(fixture_id)
        return self._prepare(context,fixture)

    def _prepare(self,context,fixture):
        """Private trusted seam used for negative synthetic validation tests."""
        context.require_active();validate_fixture(fixture)
        owner,run_id=context.run.owner_id,context.run.run_id
        export_id='qlib-input-'+uuid4().hex
        directory=self._directory(owner,run_id,export_id)
        directory.mkdir(parents=True,exist_ok=False)
        csv_dir=self.policy.authorize(directory/'csv');csv_dir.mkdir()
        items=[]
        for symbol,rows in sorted(fixture['instruments'].items()):
            context.require_active()
            descriptor=self.parquet.write(context,owner,rows)
            restored=self.parquet.read(owner,descriptor)
            if restored!=rows:raise DataError('QLIB_INPUT_PARQUET_MISMATCH')
            output=io.StringIO(newline='')
            writer=csv.DictWriter(output,fieldnames=COLUMNS,lineterminator='\n')
            writer.writeheader()
            for row in restored:writer.writerow({**{k:row[k] for k in COLUMNS if k not in {'date'}},'date':row['session_date']})
            file=self._write(csv_dir,symbol+'.csv',output.getvalue().encode())
            items.append({'symbol':symbol,'row_count':len(rows),'rows_logical_hash':fingerprint(rows),
                'parquet':descriptor,'CSV':file,'source':'NEW_SYNTHETIC_FIXTURE_NOT_MARKET_HISTORY'})
        body={'schema':'qlib-authorized-input-1','export_id':export_id,'owner_id':owner,'run_id':run_id,
            'fixture_id':FIXTURE_ID,'origin':ORIGIN,'seed':SEED,'calendar':fixture['calendar'],
            'universe':fixture['universe'],'benchmark':BENCHMARK,'semantics':fixture['semantics'],
            'split_indices':fixture['split_indices'],'instruments':items,'logical_data_hash':fingerprint(fixture),
            'input_columns':list(COLUMNS),'state':'INPUT_PREPARED_NATIVE_DUMP_NOT_EXECUTED',
            'native_output':None,'provider_uri':None,'native_dump_commit':'54355232463878d2eebb91fe0ee5fa7fa1f5976c'}
        manifest=self._write(directory,'input_manifest.json',canonical(body).encode())
        context.require_active()
        with self.db.transaction() as con:
            context.assert_fenced(con)
            con.execute('INSERT INTO data_qlib_input_exports VALUES(?,?,?,?,?)',
                (export_id,owner,run_id,canonical(body),fingerprint(body)))
        return {'export_id':export_id,'owner_id':owner,'run_id':run_id,'content_hash':fingerprint(body),
            'logical_data_hash':body['logical_data_hash'],'manifest_file':manifest,
            'manifest_ref':str((directory/'input_manifest.json').relative_to(self.policy.root)),
            'state':body['state'],'provider_uri':None}

    def get(self,owner,export_id):
        identifier(owner);identifier(export_id)
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT body,content_hash FROM data_qlib_input_exports WHERE export_id=? AND owner_id=?',
                (export_id,owner)).fetchone()
        if row is None:raise DataError('RESOURCE_NOT_FOUND')
        body=json.loads(row['body'])
        if fingerprint(body)!=row['content_hash']:raise DataError('QLIB_EXPORT_LOGICAL_MISMATCH')
        directory=self._directory(owner,body['run_id'],export_id)
        path=regular_owned(directory,'input_manifest.json')
        if self.policy.read_bytes(path)!=canonical(body).encode():raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        for instrument in body['instruments']:
            entry=instrument['CSV'];path=regular_owned(directory/'csv',entry['name'])
            raw=self.policy.read_bytes(path)
            if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
                raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        return {**body,'content_hash':row['content_hash']}

    def prepare_histories(self,context,selections):
        # No new authentication flow: the existing installed handler supplies
        # its authenticated fenced context. Never trust an HTTP owner/path flag.
        context.require_active()
        if self.history_store is None or not 1<=len(selections)<=9:raise DataError('QLIB_HISTORY_SELECTION_INVALID')
        for selection in selections:
            if not isinstance(selection,HistorySelection):raise DataError('QLIB_HISTORY_SELECTION_INVALID')
            value=self.history_store.view(context.run.owner_id,selection.history_id,selection.version,selection.start,selection.end)
            if value.get('provider') in {'akshare','yfinance'} or not value.get('raw_compatible'):
                raise DataError('QLIB_REAL_ADJUSTED_BASIS_NOT_REVIEWED')
        # No unreviewed raw/factor transformation or fake synthetic flag fallback.
        raise DataError('QLIB_HISTORY_BASIS_ADAPTER_NOT_IMPLEMENTED')

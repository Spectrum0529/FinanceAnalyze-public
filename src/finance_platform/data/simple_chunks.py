"""Server-only bounded Parquet chunks using the existing guarded RawStore.

Completed chunks may survive cancellation as unreferenced evidence. Only the
fenced library publication plus Core SUCCEEDED makes a dataset selectable.
There are no user-supplied paths or generic file operations in this interface.
"""
from datetime import datetime,timezone
from hashlib import sha256
from uuid import uuid4
from finance_platform.core.contracts import fingerprint,identifier
from predictor.data.raw_store import RawStore
from .parquet_store import _arrow
from .simple_inputs import validate_rows
from .contracts import DataError

FORMAT='library-multisecurity-parquet-1'
NATIVE_FORMAT='library-native-unit-parquet-1'
OBSERVED_FORMAT='library-observed-unit-parquet-1'
BATCH_ROWS=2000
MAX_ROWS=2000000
MAX_CHUNKS=2000
FIELDS=('ticker','session_date','open','high','low','close','volume')
PARSE_SHA256=sha256(b'dataset-parquet-chunk-to-canonical-rows-v1').hexdigest()
WRDS_MEDIA_TYPE='application/vnd.financeanalyze.wrds-bars+parquet'

class DatasetChunks:
    def __init__(self,policy,owner,*,binding=None,read_scope=None):
        identifier(owner)
        directory=policy.partitions['development']/'dataset-library'/sha256(owner.encode()).hexdigest()
        self.market=getattr(policy,'market_storage',None);self.owner=owner;self.directory=directory
        self.read_scope=read_scope
        self.store=self.market.raw(owner,directory,binding=binding,scope=read_scope) if self.market else RawStore(policy,directory)

    def put(self,rows,*,start,end,scope,context,provider='massive'):
        context.require_active()
        if self.market:
            binding=self.market.bind(self.owner,context=context)
            self.store=self.market.raw(self.owner,self.directory,binding=binding)
        origins={'massive':('massive.daily.normalized','https://api.massive.com/v2/aggs'),
                 'yfinance':('yfinance.history.normalized','https://query1.finance.yahoo.com'),
                 'akshare':('akshare.history.normalized','https://finance.sina.com.cn'),
                 'openbb_cboe':('openbb.cboe.daily.observed','https://cdn.cboe.com/api/global/delayed_quotes/charts/historical')}
        if (provider=='wrds')!=(scope=='licensed_local'):raise DataError('WRDS_CAPTURE_REQUIRED')
        if provider not in origins and provider!='wrds':raise DataError('PROVIDER_UNSUPPORTED')
        if not rows or len(rows)>BATCH_ROWS:raise DataError('IMPORT_BATCH_LIMIT')
        native=provider in {'akshare','yfinance','openbb_cboe'};storage_format=OBSERVED_FORMAT if provider=='openbb_cboe' else NATIVE_FORMAT if native else FORMAT
        rows=validate_rows(rows,start,end,sorted({r['ticker'] for r in rows}),allow_missing_volume=native)
        pa,pq,_=_arrow();schema=pa.schema([pa.field(k,pa.string(),nullable=native and k=='volume') for k in FIELDS],metadata={b'format':storage_format.encode()})
        sink=pa.BufferOutputStream();pq.write_table(pa.Table.from_pylist(rows,schema=schema),sink,compression='zstd',row_group_size=BATCH_ROWS)
        raw=sink.getvalue().to_pybytes();digest=sha256(raw).hexdigest();batch='library-'+uuid4().hex
        if provider=='wrds':
            from .wrds_library import HISTORY_JOB
            if not self.market or context.run.handler!=HISTORY_JOB:raise DataError('WRDS_CAPTURE_REQUIRED')
            ref=self.market.storage.put(binding,'market_data',raw,media_type=WRDS_MEDIA_TYPE)
            return {'registered_payload':ref,'sha256':digest,'rows_hash':fingerprint(rows),
                    'row_count':len(rows),'byte_count':len(raw),'first_key':[rows[0]['ticker'],rows[0]['session_date']],
                    'last_key':[rows[-1]['ticker'],rows[-1]['session_date']]}
        self.store.begin_batch(batch,{'request':{'source_id':origins[provider][0],'source_url':origins[provider][1],
          'parameters':{'rows_hash':fingerprint(rows),'start':start,'end':end},'schema_version':storage_format,
          'data_scope':'synthetic_only' if scope=='synthetic' else 'public_provider_sample'},
          'license':{'id':'PROVIDER_TERMS_OR_SYNTHETIC_FIXTURE','status':'NOT_A_REDISTRIBUTION_GRANT','usage':'LOCAL_PERSONAL'},
          'expected_pages':1,'expected_response_hash':digest,'expected_records':len(rows),'source_version':None})
        context.require_active()
        self.store.put_page(batch,0,raw,{'received_at':datetime.now(timezone.utc).isoformat(),'http_status':200,'response_headers':{},'request_headers':None})
        done=self.store.finish_batch(batch,record_count=len(rows));context.require_active()
        return {'request_key':done['request_key'],'version':done['version'],'sha256':digest,'rows_hash':fingerprint(rows),**({'format':storage_format} if native else {}),
                'row_count':len(rows),'byte_count':len(raw),'first_key':[rows[0]['ticker'],rows[0]['session_date']],
                'last_key':[rows[-1]['ticker'],rows[-1]['session_date']]}

    def read(self,descriptor,*,start,end,parse_cache=None,parse_identity=None):
        if 'registered_payload' in descriptor:
            ref=descriptor['registered_payload']
            scope=self.read_scope or self.store.scope if self.market else None
            if (not scope or type(ref)is not dict or ref.get('media_type')!=WRDS_MEDIA_TYPE
                    or ref.get('location_id')!=scope['location_id']):
                raise DataError('WRDS_CAPTURE_REQUIRED')
            raw=self.market.storage.read(self.owner,ref,category='market_data',maximum_bytes=8*1024**2)
            parse_cache=None
        else:
            raw=self.store.read_version(descriptor['request_key'],descriptor['version'])
        if len(raw)!=descriptor['byte_count'] or sha256(raw).hexdigest()!=descriptor['sha256']:raise DataError('DATASET_HASH_MISMATCH')
        storage_format=descriptor.get('format',FORMAT)
        if (storage_format not in {FORMAT,NATIVE_FORMAT,OBSERVED_FORMAT}
                or type(descriptor['row_count']) is not int or not 0<descriptor['row_count']<=BATCH_ROWS):
            raise DataError('DATASET_CHUNK_INVALID')
        native=storage_format in {NATIVE_FORMAT,OBSERVED_FORMAT}
        pa,pq,_=_arrow()

        def compute():
            schema=pa.schema([pa.field(k,pa.string(),nullable=native and k=='volume') for k in FIELDS],
                             metadata={b'format':storage_format.encode()})
            reader=pq.ParquetFile(pa.BufferReader(raw))
            if not reader.schema_arrow.equals(schema,check_metadata=True) or reader.metadata.num_rows!=descriptor['row_count']:
                raise DataError('DATASET_CHUNK_INVALID')
            rows=reader.read().to_pylist()
            return validate_rows(rows,start,end,sorted({r['ticker'] for r in rows}),allow_missing_volume=native)

        def validate(rows):
            if (type(rows) is not list or len(rows)!=descriptor['row_count'] or fingerprint(rows)!=descriptor['rows_hash']
                    or [rows[0]['ticker'],rows[0]['session_date']]!=descriptor['first_key']
                    or [rows[-1]['ticker'],rows[-1]['session_date']]!=descriptor['last_key']):
                raise DataError('DATASET_HASH_MISMATCH')

        if parse_cache is None or parse_identity is None or self.read_scope is None:
            rows=compute()
            validate(rows)
            return rows
        # The source bytes above come from the original registered object even
        # on a hit. Only completed verified decoding is reusable.
        inputs={**parse_identity,'location':self.read_scope['location_id'],'descriptor':descriptor,
                'source_start':start,'source_end':end,'format':storage_format,'nullable_volume':native}
        identity={'input_sha256':fingerprint(inputs),'computation_sha256':PARSE_SHA256,
                  'engine_sha256':parse_identity['engine_sha256']}
        result=parse_cache(identity=identity,compute=compute,validate=validate)
        return [dict(row) for row in result.value]

    def stream(self,manifest,*,start,end,parse_cache=None,parse_identity=None):
        if manifest.get('format')not in {FORMAT,NATIVE_FORMAT,OBSERVED_FORMAT} or not 0<len(manifest.get('chunks',[]))<=MAX_CHUNKS:raise DataError('DATASET_CHUNK_INVALID')
        count=0;previous=None
        for item in manifest['chunks']:
            for row in self.read(item,start=start,end=end,parse_cache=parse_cache,parse_identity=parse_identity):
                key=(row['ticker'],row['session_date'])
                if previous is not None and key<=previous:raise DataError('IMPORT_DUPLICATE_OR_UNSORTED_BAR')
                previous=key;count+=1
                if count>MAX_ROWS:raise DataError('IMPORT_SERVER_ROWS_LIMIT')
                yield row
        if count!=manifest['row_count']:raise DataError('DATASET_HASH_MISMATCH')

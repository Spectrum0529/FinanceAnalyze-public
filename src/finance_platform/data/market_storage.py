"""Typed market payload envelope; legacy logical hashes remain unchanged.

Native SDK working roots stay managed. Only durable publications use these refs.
RawStore retains its ingestion/validation engine and managed SQLite index; its
object IO is adapted under a location-scoped managed index namespace, so identical
logical descriptors can coexist at A/B without reinterpreting old dataset refs.
"""
from contextlib import closing,contextmanager
from contextvars import ContextVar
from hashlib import sha256
from uuid import uuid4
import json
from finance_platform.core.contracts import canonical
from finance_platform.core.storage_locations import StorageService
from finance_platform.core.storage_guard import fail
from predictor.data.raw_store import RawStore

ACTIVE=ContextVar('market_storage_binding',default=None)

@contextmanager
def using(binding):
    token=ACTIVE.set(binding)
    try:yield
    finally:ACTIVE.reset(token)

class MarketStorage:
    def __init__(self,database,policy):
        self.storage=StorageService(database,policy);self.policy=policy
        policy.market_storage=self
    def bind(self,owner,key=None,context=None):
        if context is not None:
            key='job:'+context.job_id
            return self.storage.lookup_binding(owner,key)
        current=ACTIVE.get()
        if current is not None:
            if current['owner_id']!=owner:fail('STORAGE_BINDING_INVALID')
            return current
        return self.storage.bind(owner,key or 'market:'+uuid4().hex,categories=('market_data',))
    def pack(self,owner,value,binding,*,connection=None):
        raw=canonical(value).encode()
        ref=self.storage.put(binding,'market_data',raw,media_type='application/json',connection=connection)
        return {'storage_envelope':'market-body-1','payload':ref}
    def unpack(self,owner,value):
        if type(value)is str:value=json.loads(value)
        if type(value)is dict and value.get('storage_envelope')=='market-body-1':
            if not {'storage_envelope','payload'}<=set(value) or set(value)-{'storage_envelope','payload','source_ref','quality'}:fail('STORAGE_REFERENCE_INVALID')
            body=self.storage.decode_payload(owner,value['payload'],category='market_data',maximum_bytes=64*1024**2)
            if 'source_ref' in value and body.get('source_ref')!=value['source_ref']:fail('STORAGE_HASH_MISMATCH')
            if 'quality' in value:
                q=value['quality']
                if type(q)is not dict or set(q)!={'allowed'} or type(q['allowed'])is not bool or body.get('quality',{}).get('allowed') is not q['allowed']:fail('STORAGE_HASH_MISMATCH')
            return body
        return value
    def scope(self,owner,value):
        if type(value)is str:value=json.loads(value)
        if type(value)is dict and value.get('storage_envelope')=='market-body-1':
            ref=value['payload']
            if ref['owner_id']!=owner or ref['category']!='market_data':fail('STORAGE_REFERENCE_INVALID')
            return {'owner_id':owner,'location_id':ref['location_id']}
        return None
    def raw(self,owner,directory,*,binding=None,scope=None):
        return MarketRawStore(self,owner,directory,binding=binding,scope=scope)

class MarketRawStore(RawStore):
    def __init__(self,market,owner,directory,*,binding=None,scope=None):
        self.market,self.owner,self.binding=market,owner,binding
        if binding is not None:
            if binding['owner_id']!=owner:fail('STORAGE_BINDING_INVALID')
            scope={'owner_id':owner,'location_id':market.storage._binding(binding,'market_data')}
        self.scope=scope
        if scope is not None:
            if scope['owner_id']!=owner:fail('STORAGE_REFERENCE_INVALID')
            directory=directory/'registered-locations'/scope['location_id']
        super().__init__(market.policy,directory)
    def _publish(self,raw):
        if self.scope is None:return super()._publish(raw)
        if self.binding is None:fail('STORAGE_BINDING_MISSING')
        return self.market.storage.put(self.binding,'market_data',raw,media_type='application/octet-stream')['sha256']
    def _blob(self,body_hash):
        if self.scope is None:return super()._blob(body_hash)
        if type(body_hash)is not str or len(body_hash)!=64:fail('STORAGE_REFERENCE_INVALID')
        return self.market.storage.read_digest(self.owner,self.scope['location_id'],body_hash,
                    category='market_data',maximum_bytes=64*1024**2)

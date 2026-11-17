"""Explicit resumed publication: verify old pinned unit, publish new run copy.

No search/fallback across roots and no moving/deleting or rewriting old content.
Only the selected unit is read through its recorded owner/location scope.
"""
from .simple_chunks import DatasetChunks,BATCH_ROWS
from .contracts import DataError

def promote_unit(library,context,manifest,source_scope,*,start,end,provider):
    owner=context.run.owner_id
    if type(source_scope)is not dict or set(source_scope)!={'owner_id','location_id'} or source_scope['owner_id']!=owner:
        raise DataError('DOWNLOAD_RESUME_STORAGE_UNBOUND')
    binding=library.market.storage.lookup_binding(owner,'job:'+context.job_id)
    same=source_scope['location_id']==binding['locations']['market_data']['location_id']
    reader=DatasetChunks(library.policy,owner,read_scope=source_scope)
    writer=DatasetChunks(library.policy,owner)
    if manifest.get('row_count')==0 and manifest.get('chunks')==[]:return dict(manifest)
    descriptors=[];buffer=[];total=0
    for row in reader.stream(manifest,start=start,end=end):
        if total%128==0:context.require_active()
        total+=1
        if total>10000:raise DataError('IMPORT_ROWS_LIMIT')
        if not same:
            buffer.append(row)
            if len(buffer)==BATCH_ROWS:
                descriptors.append(writer.put(buffer,start=start,end=end,scope='public',context=context,provider=provider));buffer=[]
    if total!=manifest['row_count']:raise DataError('DATASET_HASH_MISMATCH')
    if same:return dict(manifest)
    if buffer:descriptors.append(writer.put(buffer,start=start,end=end,scope='public',context=context,provider=provider))
    return {**manifest,'chunks':descriptors}

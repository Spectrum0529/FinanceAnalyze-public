"""Bounded, explicit report projections and pages over the same saved identity."""
import hashlib
import json
from collections import OrderedDict
from sys import getsizeof
from threading import Lock
from .errors import PlatformError

def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(encode(value)).hexdigest()
def pointer(path,key):return path+'/'+str(key).replace('~','~0').replace('/','~1')


def filename(result_id):
    return "finance-" + result_id.replace(":", "-") + ".json"


VIEW_POLICY = "finance-result-pages-1"
CACHE_BYTES = 48 * 1024**2


def _memory_weight(value):
    total = 0
    pending = [value]
    while pending and total <= CACHE_BYTES:
        item = pending.pop()
        total += getsizeof(item)
        if type(item) is list:
            pending.extend(item)
        elif type(item) is dict:
            pending.extend(item.keys())
            pending.extend(item.values())
    return total


class VerifiedViews:
    def __init__(self):
        self.rows = OrderedDict()
        self.weight = 0
        self.lock = Lock()

    def get(self, key):
        with self.lock:
            entry = self.rows.get(key)
            if entry is not None:
                self.rows.move_to_end(key)
        return None if entry is None else entry[0]

    def put(self, key, value):
        weight = _memory_weight(value[0])
        if weight > CACHE_BYTES:
            return
        with self.lock:
            if key in self.rows:
                return
            while self.rows and (len(self.rows) >= 8 or self.weight + weight > CACHE_BYTES):
                _, (_, evicted_weight) = self.rows.popitem(last=False)
                self.weight -= evicted_weight
            self.rows[key] = (value, weight)
            self.weight += weight


class Sections(list):
    complete=True
    def record(self,row):
        if len(self)<1024:self.append(row)
        else:self.complete=False

class ResultViews:
    def __init__(self, index):
        self.index = index
        self.verified = VerifiedViews()

    def _version(self, principal, result_id, expected_sha256=None, *, inspect_deleted=False):
        identity = getattr(self.index, "view_identity", None)
        identity = identity(principal, result_id, inspect_deleted=inspect_deleted) if identity is not None else None
        key = (VIEW_POLICY, identity) if identity is not None else None
        cached = self.verified.get(key) if key is not None else None
        if cached is None:
            reader = self.index._read_for_metadata if inspect_deleted else self.index.read
            value = reader(principal, result_id)
            sha = digest(value)
            if key is not None:
                self.verified.put(key, (value, sha))
        else:
            value, sha = cached
            self.index.reauthorize_view(principal, result_id, value)
        if expected_sha256 is not None and sha != expected_sha256:
            raise PlatformError("RESULT_VIEW_CHANGED", 409)
        return value, sha

    def _project(self,value,path,sections,*,depth=0):
        if depth>32:raise PlatformError('RESULT_VIEW_TOO_DEEP',422)
        if type(value)is list or type(value)is str and len(value)>16000:
            row={'section':path,'kind':'list'if type(value)is list else'text','total':len(value),
                 'unit':'items'if type(value)is list else'unicode_characters','paged':True}
            sections.record(row)
            return {'_paged':True,**row}
        if type(value)is dict:return {key:self._project(item,pointer(path,key),sections,depth=depth+1)for key,item in value.items()}
        return value

    def summary(self,principal,result_id):
        original,sha=self._version(principal,result_id);sections=Sections()
        projected=self._project(original,'',sections)
        source,_=self.index._source(result_id)
        if source.series is not None:
            sections.extend({'section':'@'+key,'kind':'list','total':row['total'],'unit':'items','paged':True,
                             'native_saved_series':True,'max_page_size':500}
                            for key,row in original.get('series_manifest',{}).items())
        if len(encode(projected))>1024**2:
            projected={'_paged':True,'section':'','kind':'object','total':len(original),'unit':'keys'}
        return {'result_id':result_id,'source_view_sha256':sha,'view':'summary',
                'summary':projected,'sections':sections,'sections_complete':sections.complete,'root_section':'',
                'section_discovery':'分区清单最多1024项；sections_complete为false时从空root_section逐页遍历，所有_paged标记均给出后续分区。',
                'paging_notice':'数组及长文本由明确分页标记替代；原始结果未删减。通过分区接口读取全部条目。',
                'section_url':'/api/v1/results/'+result_id+'/series'}

    def section(self,principal,result_id,section='',limit=500,offset=0,expected_sha256=None):
        if (type(section)is not str or len(section)>1024 or type(limit)is not int or not 1<=limit<=8192
                or type(offset)is not int or not 0<=offset<=2000000):raise PlatformError('INVALID_INPUT',422)
        original,sha=self._version(principal,result_id,expected_sha256)
        value=original
        if section.startswith('@'):
            source,run=self.index._source(result_id);name=section[1:]
            if source.series is None or name not in original.get('series_manifest',{})or limit>500:
                raise PlatformError('RESULT_SECTION_UNAVAILABLE',404)
            page=source.series(principal,run,name,limit,offset)
            if page['total']!=original['series_manifest'][name]['total']:raise PlatformError('RESULT_VIEW_CHANGED',409)
            result={'result_id':result_id,'source_view_sha256':sha,'section':section,'kind':'list',
                    'total':page['total'],'limit':limit,'offset':offset,'items':page['items'],
                    'next_offset':offset+len(page['items'])if offset+len(page['items'])<page['total']else None,
                    'nested_sections':[],'native_saved_series':True}
            if len(encode(result))>2*1024**2:raise PlatformError('RESULT_PAGE_TOO_LARGE',413)
            return result
        if section:
            if not section.startswith('/'):raise PlatformError('RESULT_SECTION_UNAVAILABLE',404)
            try:
                for token in section[1:].split('/'):
                    token=token.replace('~1','/').replace('~0','~')
                    if type(value)is dict:value=value[token]
                    elif type(value)is list and token.isdecimal():value=value[int(token)]
                    else:raise KeyError()
            except (KeyError,IndexError):raise PlatformError('RESULT_SECTION_UNAVAILABLE',404)from None
        if type(value)not in (list,dict,str):raise PlatformError('RESULT_SECTION_UNAVAILABLE',404)
        if type(value)is not str and limit>1000:raise PlatformError('INVALID_INPUT',422)
        total=len(value);end=min(offset+limit,total);nested=Sections()
        if type(value)is str:payload={'text':value[offset:end]};kind='text';count=len(payload['text'])
        elif type(value)is list:
            payload={'items':[self._project(item,pointer(section,index),nested)for index,item in enumerate(value[offset:end],offset)]};kind='list';count=len(payload['items'])
        else:
            keys=sorted(value)[offset:end]
            payload={'items':[{'key':key,'value':self._project(value[key],pointer(section,key),nested)}for key in keys]};kind='object';count=len(keys)
        result={'result_id':result_id,'source_view_sha256':sha,'section':section,'kind':kind,
                'total':total,'limit':limit,'offset':offset,'next_offset':offset+count if offset+count<total else None,
                'nested_sections':nested,'nested_sections_complete':nested.complete,**payload}
        if len(encode(result))>2*1024**2:raise PlatformError('RESULT_PAGE_TOO_LARGE',413)
        return result

    def export_manifest(self,principal,result_id):
        view=self.summary(principal,result_id)
        return {'format':VIEW_POLICY,'result_id':result_id,'source_view_sha256':view['source_view_sha256'],
                'filename': filename(result_id),
                'summary':view['summary'],'sections':view['sections'],'sections_complete':view['sections_complete'],'root_section':'',
                'read_url':view['section_url'],'instructions':'按 section 分页读取并核对 source_view_sha256；next_offset 为 null 表示该分区已读完。嵌套分区同样继续读取。未用截断内容冒充完整导出。'}

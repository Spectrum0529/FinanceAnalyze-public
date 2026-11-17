"""Lossless bounded chunks using FA013's existing evaluation JSON artifacts."""
import hashlib
import json
from .protocol import encoded, require, strict_json, MAX_RESPONSE


def single(kind, media_type, content, logical_id):
    content = content if type(content) is str else encoded(content).decode()
    raw = content.encode()
    require(len(raw) <= 65536, 'ARTIFACT_SINGLE_TOO_LARGE')
    return {'kind': kind, 'media_type': media_type, 'content': content, 'logical_id': logical_id,
            'part_index': 0, 'part_count': 1, 'full_sha256': hashlib.sha256(raw).hexdigest()}


def chunks(kind, media_type, content, logical_id):
    text=content if type(content) is str else encoded(content).decode()
    parts=[text[i:i+15000] for i in range(0,len(text),15000)] or ['']
    require(len(parts)<=256,'ARTIFACT_CHUNK_CAPACITY_EXCEEDED')
    sha=hashlib.sha256(text.encode()).hexdigest()
    return [{'kind':kind,'media_type':media_type,'content':p,'logical_id':logical_id,
             'part_index':i,'part_count':len(parts),'full_sha256':sha} for i,p in enumerate(parts)]


def pack(value, maximum_parts=250):
    # ASCII JSON keeps chunks unambiguous even with multibyte source/feedback.
    text = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    pieces = [text[i:i+60000] for i in range(0, len(text), 60000)]
    require(len(pieces) <= maximum_parts, 'ARTIFACT_CHUNK_CAPACITY_EXCEEDED')
    require(len(text.encode()) <= 16*1024*1024, 'ARTIFACT_TOTAL_CAPACITY_EXCEEDED')
    sha = hashlib.sha256(text.encode()).hexdigest()
    return [{'kind': 'evaluation', 'media_type': 'application/json', 'content': piece,
             'logical_id': 'full-experiment', 'part_index': i, 'part_count': len(pieces), 'full_sha256': sha}
            for i, piece in enumerate(pieces)]


def unpack(artifacts):
    parts = artifacts
    require(0 < len(parts) <= 250, 'ARTIFACT_PARTS_INVALID')
    expected = parts[0]['full_sha256']
    require(all(p['logical_id'] == 'full-experiment' and p['part_index'] == i and p['part_count'] == len(parts)
                and p['full_sha256'] == expected and len(p['content'].encode()) <= 65536 for i, p in enumerate(parts)), 'ARTIFACT_PARTS_INVALID')
    raw = ''.join(p['content'] for p in parts).encode()
    require(hashlib.sha256(raw).hexdigest() == expected, 'ARTIFACT_HASH_MISMATCH')
    value = strict_json(raw, 16*1024*1024)
    return value

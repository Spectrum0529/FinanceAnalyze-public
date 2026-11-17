"""One approved loopback product origin, normal session/CSRF, no raw proxy."""
import asyncio
import base64
from contextlib import nullcontext
import hashlib
import json
import re
from time import time
from urllib.parse import urlsplit,quote
import httpx
from pydantic import SecretStr
from .errors import PlatformError
from .mcp_grants import SCOPE_LABELS
from .mcp_proof import BASE, EXECUTE, GRANT_ID, PROOF_HEADER, SESSION, make_claim, request_proof
from finance_platform.operations.provider_credentials import WRDS_SLOT

MAX_BYTES=8*1024**2

def checked_origin(value):
    try:
        parsed=urlsplit(value)
        if (parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost','::1'}
                or parsed.username is not None or parsed.password is not None or parsed.path not in {'','/'}
                or parsed.query or parsed.fragment or parsed.port is not None and not 1<=parsed.port<=65535):raise ValueError()
    except (TypeError,ValueError):raise PlatformError('MCP_LOCAL_ORIGIN_REQUIRED',422) from None
    return parsed.scheme+'://'+parsed.netloc


class ProductGateway:
    def __init__(self,origin,*,transport=None,timeout=30,key_store=None,client_id=None,installation=None):
        self.origin=checked_origin(origin)
        self.key_store, self.client_id = key_store, client_id
        self.installation = installation
        self.account_mode = key_store is not None
        if self.account_mode and (urlsplit(self.origin).hostname != "127.0.0.1"
                                  or type(installation) is not str or not re.fullmatch(r"[0-9a-f]{64}", installation)):
            raise PlatformError("MCP_LOCAL_ORIGIN_REQUIRED", 422)
        self.client=httpx.AsyncClient(base_url=self.origin,transport=transport,timeout=timeout,follow_redirects=False,trust_env=False,
            headers={'Origin':self.origin,'Accept':'application/json'})
        self.csrf=None;self.session_lock=asyncio.Lock()

    async def close(self):await self.client.aclose()

    async def _json(self,method,path,*,signed=False,credential=None,**kwargs):
        request = self.client.build_request(method, path, **kwargs)
        if signed:
            grant, key = credential or self.key_store.read()
            request.headers[PROOF_HEADER] = request_proof(
                key, self.installation, self.origin, method, request.url.raw_path, request.content, grant, int(time()))
        try:
            response = await self.client.send(request, stream=True)
            try:
                if response.is_redirect:
                    raise PlatformError('MCP_REDIRECT_REFUSED',502)
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>MAX_BYTES:raise PlatformError('MCP_RESPONSE_TOO_LARGE',413)
                try:value=json.loads(raw,parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
                except (ValueError,UnicodeError):raise PlatformError('MCP_PRODUCT_RESPONSE_INVALID',502) from None
                if response.status_code>=400:
                    code=value.get('code') if type(value)is dict else None
                    if type(code)is not str or not re.fullmatch('[A-Z][A-Z0-9_]{0,63}',code):code='MCP_PRODUCT_ACTION_FAILED'
                    raise PlatformError(code,response.status_code)
                return value
            finally:
                await response.aclose()
        except httpx.HTTPError:raise PlatformError('MCP_PRODUCT_UNAVAILABLE',503) from None

    async def account_scope(self, *, credential=None, observe_tools=False):
        query = {"observe": "tools"} if observe_tools else None
        response = await self._json("GET", SESSION, signed=True, credential=credential, params=query)
        scope = response.get("scope") if type(response) is dict else None
        if type(scope) is not str or scope not in SCOPE_LABELS:
            raise PlatformError("MCP_PRODUCT_RESPONSE_INVALID", 502)
        return scope

    def authorization_revision(self):
        return self.key_store.revision()

    async def discovery_state(self):
        credential = self.key_store.read()
        return credential[0], await self.account_scope(credential=credential)

    async def pair(self, code):
        with getattr(self.key_store, "locked", nullcontext)():
            await self._pair(code)

    async def _pair(self, code):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        try:
            claim = make_claim(code.strip(), self.installation, self.origin, self.client_id, key)
        except ValueError:
            raise PlatformError("MCP_PAIRING_REJECTED", 401) from None
        response = await self._json("POST", BASE + "/pairings/claim", json=claim)
        grant = response.get("grant_id") if type(response) is dict else None
        if (type(grant) is not str or not GRANT_ID.fullmatch(grant)
                or response.get("client_id") != self.client_id):
            raise PlatformError("MCP_PRODUCT_RESPONSE_INVALID", 502)
        if await self.account_scope(credential=(grant, key)) != response.get("scope"):
            raise PlatformError("MCP_PRODUCT_RESPONSE_INVALID", 502)
        self.key_store.write(grant, key)

    async def session(self):
        if self.csrf is not None:return
        async with self.session_lock:
            if self.csrf is not None:return
            value=await self._json('POST','/api/v1/auth/local-session')
            if (type(value)is not dict or value.get('user',{}).get('user_id')!='local-personal'
                    or value.get('user',{}).get('role')!='researcher' or type(value.get('csrf_token'))is not str):
                raise PlatformError('MCP_PERSONAL_MODE_REQUIRED',403)
            self.csrf=value['csrf_token']

    async def _csv(self,path):
        try:
            async with self.client.stream('GET',path)as response:
                if response.is_redirect:raise PlatformError('MCP_REDIRECT_REFUSED',502)
                if response.status_code>=400:raise PlatformError('MCP_PRODUCT_ACTION_FAILED',response.status_code)
                if 'text/csv'not in response.headers.get('content-type',''):raise PlatformError('MCP_PRODUCT_RESPONSE_INVALID',502)
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>1024**2:raise PlatformError('MCP_RESPONSE_TOO_LARGE',413)
                return {'format':'original-csv-base64','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                        'content_base64':base64.b64encode(raw).decode('ascii'),'complete':True}
        except httpx.HTTPError:raise PlatformError('MCP_PRODUCT_UNAVAILABLE',503)from None

    async def call(self,operation,parameters):
        if (operation.name in {"provider_credential_set", "provider_credential_delete"}
                and parameters.slot == WRDS_SLOT):
            raise PlatformError("WRDS_CREDENTIAL_LOCAL_ONLY", 403)
        if not self.account_mode:
            await self.session()
        values=parameters.model_dump(exclude_none=operation.name not in {'storage_location_check','storage_location_save','data_research_export'})
        path=operation.path
        for key in operation.path_fields:path=path.replace('{'+key+'}',quote(str(values.pop(key)),safe=''))
        query={key:values.pop(key) for key in operation.query_fields if key in values}
        body=values.pop(operation.body_field) if operation.body_field else values if operation.body_rest else None
        if not operation.body_rest and values:raise PlatformError('MCP_INPUT_BINDING_INVALID',422)
        headers={'X-CSRF-Token':self.csrf} if operation.method!='GET' and not self.account_mode else {}
        if operation.name=='data_connection_create'and body is not None:
            body={**body,'connection':{**body['connection'],'credential_ref':parameters.connection.credential_ref}}
        if operation.name == "data_provider_download" and self.account_mode:
            body = {**body, "request": parameters.request.model_dump()}
        if operation.name == "data_connection_lifecycle" and "credential_ref" in parameters.model_fields_set:
            body = {**body, "credential_ref": parameters.credential_ref}
        if operation.name=='provider_credential_set' and body is not None:
            if not isinstance(body.get('value'),SecretStr):raise PlatformError('INVALID_INPUT',422)
            body={**body,'endpoint_id':parameters.endpoint_id,'value':body['value'].get_secret_value()}
        if body is not None and len(json.dumps(body,ensure_ascii=False,allow_nan=False).encode())>2*1024**2:
            raise PlatformError('MCP_REQUEST_TOO_LARGE',413)
        if self.account_mode:
            return await self._json(operation.method, EXECUTE + path, signed=True, params=query, json=body)
        if operation.response_format=='csv_base64':return await self._csv('/api/v1'+path)
        return await self._json(operation.method,'/api/v1'+path,params=query,json=body,headers=headers)

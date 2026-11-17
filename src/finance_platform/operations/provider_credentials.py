"""Owner-bound, write-only provider credential management using Windows DPAPI.

No environment lookup, credential enumeration, plaintext metadata or public get.
Only the trusted provider path may call resolve_for_provider. Status is storage
presence, not proof that a key is valid or that a provider was contacted.
"""
import base64
import ctypes as C
from dataclasses import dataclass, field
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import re
import uuid

from finance_platform.core.errors import PlatformError
from finance_platform.operations.paths import contained, protect_private


class _Blob(C.Structure):
    _fields_ = [('size', W.DWORD), ('data', C.POINTER(C.c_ubyte))]


def _dpapi(data, entropy, *, decrypt=False):
    if os.name != 'nt':
        raise PlatformError('CREDENTIAL_STORE_UNAVAILABLE', 503)
    library = C.WinDLL('crypt32.dll', use_last_error=True, winmode=0x00000800)
    kernel = C.WinDLL('kernel32.dll', use_last_error=True, winmode=0x00000800)
    kernel.LocalFree.argtypes = [C.c_void_p]
    kernel.LocalFree.restype = C.c_void_p
    source_buffer, entropy_buffer = C.create_string_buffer(data), C.create_string_buffer(entropy)
    source = _Blob(len(data), C.cast(source_buffer, C.POINTER(C.c_ubyte)))
    extra = _Blob(len(entropy), C.cast(entropy_buffer, C.POINTER(C.c_ubyte)))
    output = _Blob()
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [C.POINTER(_Blob), C.c_void_p, C.POINTER(_Blob), C.c_void_p,
                         C.c_void_p, W.DWORD, C.POINTER(_Blob)]
    function.restype = W.BOOL
    try:
        # UI_FORBIDDEN only: never CRYPTPROTECT_LOCAL_MACHINE or UI prompts.
        if not function(C.byref(source), None, C.byref(extra), None, None, 1, C.byref(output)):
            raise PlatformError('CREDENTIAL_STORE_UNAVAILABLE', 503)
        if not 0 < output.size <= 16384:
            raise PlatformError('CREDENTIAL_STORE_UNAVAILABLE', 503)
        return C.string_at(output.data, output.size)
    finally:
        C.memset(source_buffer, 0, len(source_buffer))
        C.memset(entropy_buffer, 0, len(entropy_buffer))
        if output.data:
            C.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)
        # Python string/bytes copies cannot be guaranteed zeroized.


from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit
from ipaddress import ip_address
import json


WRDS_SLOT = "wrds"
WRDS_TARGET = "postgresql://wrds-pgdata.wharton.upenn.edu:9737/wrds"


@dataclass(frozen=True)
class WrdsCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)

    @classmethod
    def parse(cls, value):
        if type(value) is not dict or set(value) != {"username", "password"}:
            raise PlatformError("INVALID_INPUT", 422)
        username, password = value["username"], value["password"]
        if (type(username) is not str or re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", username) is None
                or type(password) is not str or not 1 <= len(password) <= 1024 or "\0" in password):
            raise PlatformError("INVALID_INPUT", 422)
        try:
            if len(password.encode("utf-8")) > 4096:
                raise PlatformError("INVALID_INPUT", 422)
        except UnicodeError:
            raise PlatformError("INVALID_INPUT", 422) from None
        return cls(username, password)


class FrozenTarget(NamedTuple):
    provider: str
    endpoint_id: str | None
    base_url: str
    target_sha256: str
    owner_scope: str


def canonical_target(provider, endpoint_id, base_url):
    """Nonsecret target fingerprint, shared with trusted env binding policy.

    Caller obtains URL from the trusted registry, never public request bodies.
    SDK construction must consume this returned exact canonical base_url.
    """
    if not isinstance(provider,str) or provider not in ProviderCredentialStore.PROVIDERS:
        raise PlatformError('INVALID_INPUT',422)
    if endpoint_id is not None and (not isinstance(endpoint_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',endpoint_id)):
        raise PlatformError('INVALID_INPUT',422)
    if provider == "wrds":
        if endpoint_id is not None or base_url != WRDS_TARGET:
            raise PlatformError("CREDENTIAL_TARGET_UNAVAILABLE", 409)
        return dict(_WRDS_TARGET_RECORD)
    try:
        if not isinstance(base_url,str) or not 1<=len(base_url)<=2048 or any(c.isspace() for c in base_url):raise ValueError()
        url=urlsplit(base_url)
        if url.scheme not in {'http','https'} or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:raise ValueError()
        host=url.hostname.encode('idna').decode('ascii').lower()
        if url.scheme=='http':
            try:loopback=ip_address(host).is_loopback
            except ValueError:loopback=host=='localhost'
            if not loopback:raise ValueError()
        port=url.port
        if port is not None and not 1<=port<=65535:raise ValueError()
        authority=('['+host+']') if ':' in host else host
        if port is not None and port!=({'http':80,'https':443}[url.scheme]):authority+=':'+str(port)
        target={'provider':provider,'endpoint_id':endpoint_id,'base_url':urlunsplit((url.scheme,authority,url.path,'',''))}
        target['target_sha256']=hashlib.sha256(json.dumps(target,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return target
    except (ValueError,UnicodeError):raise PlatformError('CREDENTIAL_TARGET_UNAVAILABLE',409) from None


_WRDS_TARGET_RECORD = {"provider": "wrds", "endpoint_id": None, "base_url": WRDS_TARGET}
_WRDS_TARGET_RECORD["target_sha256"] = hashlib.sha256(
    json.dumps(_WRDS_TARGET_RECORD, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


class ProviderCredentialStore:
    PROVIDERS={'openai','deepseek','anthropic','gemini','qwen','kimi','glm','openai_compatible','ollama','massive','tushare','wrds'}

    def __init__(self, root, target_resolver=None):
        self.root=Path(root).resolve()
        self.base=contained(self.root,self.root/'.local/secrets/provider_credentials')
        self.target_resolver=target_resolver

    def _key(self, owner, slot):
        if not isinstance(owner,str) or not 1<=len(owner)<=128 or not isinstance(slot,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',slot):
            raise PlatformError('INVALID_INPUT',422)
        return hashlib.sha256((str(self.root)+'\0'+owner).encode()).hexdigest()[:32]+hashlib.sha256(slot.encode()).hexdigest()[:32]

    def _file(self, key, suffix):
        path=contained(self.root,self.base/(key+suffix))
        if path.exists() and (not path.is_file() or path.stat().st_nlink!=1):raise PlatformError('INVALID_INPUT',422)
        return path

    def freeze_target(self, owner, provider, endpoint_id):
        scope=self._key(owner,'scope-check')[:32]
        if not isinstance(provider,str) or provider not in self.PROVIDERS or (endpoint_id is not None and (not isinstance(endpoint_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',endpoint_id))):
            raise PlatformError('INVALID_INPUT',422)
        if provider == "wrds":
            return FrozenTarget(owner_scope=scope, **canonical_target(provider, endpoint_id, WRDS_TARGET))
        if not callable(self.target_resolver):raise PlatformError('CREDENTIAL_TARGET_UNAVAILABLE',409)
        raw=self.target_resolver(owner,provider,endpoint_id)  # One registry lookup.
        if not isinstance(raw,dict) or set(raw)!={'provider','endpoint_id','base_url'} or raw['provider']!=provider or raw['endpoint_id']!=endpoint_id:
            raise PlatformError('CREDENTIAL_TARGET_UNAVAILABLE',409)
        normalized=canonical_target(**raw)
        return FrozenTarget(owner_scope=scope,**normalized)

    def _target_record(self, owner, target):
        if type(target) is not FrozenTarget or target.owner_scope!=self._key(owner,'scope-check')[:32]:
            raise PlatformError('CREDENTIAL_TARGET_MISMATCH',409)
        normalized=canonical_target(target.provider,target.endpoint_id,target.base_url)
        if normalized['target_sha256']!=target.target_sha256:raise PlatformError('CREDENTIAL_TARGET_MISMATCH',409)
        return normalized

    def _metadata(self,key):
        path=self._file(key,'.json')
        if not path.exists():return None
        if path.stat().st_size>8192:raise PlatformError('CREDENTIAL_STORE_UNAVAILABLE',503)
        try:
            value=json.loads(path.read_bytes())
            if isinstance(value,dict) and set(value)=={'provider','endpoint_id'}:return None  # unbound r03 metadata
            if not isinstance(value,dict) or set(value)!={'provider','endpoint_id','base_url','target_sha256'}:raise ValueError()
            normalized=canonical_target(value['provider'],value['endpoint_id'],value['base_url'])
            if value!=normalized:raise ValueError()
            return value
        except (ValueError,TypeError,KeyError):raise PlatformError('CREDENTIAL_STORE_UNAVAILABLE',503) from None

    def _ref_key(self,owner,ref):
        if not isinstance(ref,str) or not re.fullmatch(r'credential:FA_[0-9a-f]{64}',ref):raise PlatformError('CREDENTIAL_NOT_CONFIGURED',409)
        key=ref[len('credential:FA_'):]
        if key[:32]!=self._key(owner,'scope-check')[:32]:raise PlatformError('CREDENTIAL_NOT_CONFIGURED',409)
        return key

    def binding_for_reference(self,owner,secret_ref):
        key=self._ref_key(owner,secret_ref)
        if key == self._key(owner, WRDS_SLOT):
            record = self._wrds_record(key)
            if record is not None or not self._file(key, ".bin").exists():
                return {
                    "configured": record is not None, "secret_ref": secret_ref,
                    **_WRDS_TARGET_RECORD, "binding_verified": record is not None,
                    "credential_generation": record[0] if record else None,
                    "provider_validation": "NOT_RUN", "protection": "windows-user-dpapi",
                }
        target=self._metadata(key)
        return {'configured':bool(target and self._file(key,'.bin').exists()),'secret_ref':secret_ref,
                'provider':target['provider'] if target else None,'endpoint_id':target['endpoint_id'] if target else None,
                'base_url':target['base_url'] if target else None,'target_sha256':target['target_sha256'] if target else None,
                'binding_verified':bool(target),'provider_validation':'NOT_RUN','protection':'windows-user-dpapi'}

    def reference(self,owner,slot):
        return 'credential:FA_'+self._key(owner,slot)

    def status(self,owner,slot):
        return {'slot':slot,**self.binding_for_reference(owner,'credential:FA_'+self._key(owner,slot))}

    def _entropy(self,key,target):
        return (key+'\0'+json.dumps(target,sort_keys=True,separators=(',',':'))).encode()

    def _write(self,key,suffix,data):
        temporary=self._file(key+'.'+uuid.uuid4().hex,'.tmp')
        with temporary.open('xb'):pass
        protect_private(temporary)
        with temporary.open('wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        temporary.replace(self._file(key,suffix))

    def _prepare_directory(self):
        for directory in (self.root/'.local', self.root/'.local/secrets', self.base):
            contained(self.root, directory)
            directory.mkdir(exist_ok=True)
            protect_private(directory)

    def _wrds_record(self, key):
        try:
            path = self._file(key, ".wrds")
            with path.open("rb") as stream:
                encoded = stream.read(32769)
            if len(encoded) > 32768:
                raise ValueError()
            record = json.loads(encoded)
            if type(record) is not dict or set(record) != {"generation", "ciphertext"}:
                raise ValueError()
            generation, ciphertext = record["generation"], record["ciphertext"]
            if (type(generation) is not str or re.fullmatch(r"[0-9a-f]{32}", generation) is None
                    or type(ciphertext) is not str):
                raise ValueError()
            return generation, ciphertext
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise PlatformError("CREDENTIAL_STORE_UNAVAILABLE", 503) from None

    def _wrds_reference(self, owner, reference):
        key = self._ref_key(owner, reference)
        if key != self._key(owner, WRDS_SLOT):
            raise PlatformError("CREDENTIAL_TARGET_MISMATCH", 409)
        record = self._wrds_record(key)
        if record is None:
            raise PlatformError("CREDENTIAL_NOT_CONFIGURED", 409)
        return key, record

    def wrds_credential_generation(self, owner, reference):
        _, (generation, _) = self._wrds_reference(owner, reference)
        return generation

    def resolve_wrds(self, owner, reference, *, expected_generation):
        key, (generation, ciphertext) = self._wrds_reference(owner, reference)
        if generation != expected_generation:
            raise PlatformError("CREDENTIAL_GENERATION_CHANGED", 409)
        try:
            encrypted = base64.b64decode(ciphertext, validate=True)
            if not 0 < len(encrypted) <= 16384:
                raise ValueError()
            data = _dpapi(encrypted, self._entropy(key + generation, _WRDS_TARGET_RECORD), decrypt=True)
            return WrdsCredentials.parse(json.loads(data))
        except ValueError:
            raise PlatformError("CREDENTIAL_STORE_UNAVAILABLE", 503) from None

    def set(self,owner,slot,value,*,provider,endpoint_id):
        key=self._key(owner,slot)
        if provider == "wrds":
            if slot != WRDS_SLOT or endpoint_id is not None or self._file(key, ".bin").exists():
                raise PlatformError("CREDENTIAL_TARGET_MISMATCH", 409)
            pair = WrdsCredentials.parse(value)
            generation = uuid.uuid4().hex
            data = json.dumps({"username": pair.username, "password": pair.password},
                              ensure_ascii=False).encode("utf-8")
            encrypted = _dpapi(data, self._entropy(key + generation, _WRDS_TARGET_RECORD))
            self._prepare_directory()
            record = {"generation": generation, "ciphertext": base64.b64encode(encrypted).decode("ascii")}
            try:
                self._write(key, ".wrds", json.dumps(record, separators=(",", ":")).encode())
            except OSError:
                raise PlatformError("CREDENTIAL_STORE_UNAVAILABLE", 503) from None
            return self.status(owner, slot)
        if slot == WRDS_SLOT and (self._wrds_record(key) is not None or not self._file(key, ".bin").exists()):
            raise PlatformError("CREDENTIAL_TARGET_MISMATCH", 409)
        target=self._target_record(owner,self.freeze_target(owner,provider,endpoint_id))
        if not isinstance(value,str) or not value or len(value.encode())>4096 or '\0' in value:raise PlatformError('INVALID_INPUT',422)
        encrypted=_dpapi(value.encode(),self._entropy(key,target))
        self._prepare_directory()
        self._write(key,'.bin',encrypted)
        self._write(key,'.json',json.dumps(target,sort_keys=True).encode())
        return self.status(owner,slot)

    def delete(self,owner,slot):
        key=self._key(owner,slot)
        wrds_path = self._file(key, ".wrds") if slot == WRDS_SLOT else None
        if wrds_path is not None and wrds_path.exists():
            wrds_path.unlink(missing_ok=True)
        else:
            for suffix in (".bin", ".json"):
                self._file(key, suffix).unlink(missing_ok=True)
        return self.status(owner,slot)

    def validate_reference_target(self,owner,secret_ref,*,target):
        key=self._ref_key(owner,secret_ref);expected=self._target_record(owner,target)
        if expected["provider"] == "wrds":
            raise PlatformError("CREDENTIAL_TARGET_MISMATCH", 409)
        if self._metadata(key)!=expected:raise PlatformError('CREDENTIAL_TARGET_MISMATCH',409)
        if not self._file(key,'.bin').exists():raise PlatformError('CREDENTIAL_NOT_CONFIGURED',409)
        return key,expected

    def _provider_envelope(self, owner, secret_ref, target):
        key,expected=self.validate_reference_target(owner,secret_ref,target=target)
        try:
            with self._file(key, '.bin').open('rb') as stream:
                ciphertext = stream.read(16385)
        except FileNotFoundError:
            raise PlatformError('CREDENTIAL_NOT_CONFIGURED', 409) from None
        except OSError:
            raise PlatformError('CREDENTIAL_STORE_UNAVAILABLE', 503) from None
        if not 0 < len(ciphertext) <= 16384:
            raise PlatformError('CREDENTIAL_STORE_UNAVAILABLE', 503)
        return key, expected, ciphertext

    def generation_for_provider(self, owner, secret_ref, *, target):
        """Identify the DPAPI envelope generation without decrypting the credential."""
        _, _, ciphertext = self._provider_envelope(owner, secret_ref, target)
        return hashlib.sha256(ciphertext).hexdigest()

    def resolve_for_provider(self, owner, secret_ref, *, target, expected_generation=None):
        """Use the frozen SDK target. Validate any supplied generation against the bytes being decrypted."""
        key, expected, ciphertext = self._provider_envelope(owner, secret_ref, target)
        if expected_generation is not None and hashlib.sha256(ciphertext).hexdigest() != expected_generation:
            raise PlatformError('MODEL_TEST_CREDENTIAL_CHANGED', 409)
        return _dpapi(ciphertext, self._entropy(key, expected), decrypt=True).decode('utf-8')

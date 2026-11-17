"""Conservative model parameters; adapters/credentials are separate facts."""
from copy import deepcopy
from urllib.parse import urlsplit,urlunsplit
from .errors import PlatformError

PROVIDERS = {
    'offline': ('离线验证', 'offline'), 'openai': ('OpenAI', 'openai_responses'),
    'deepseek': ('DeepSeek', 'openai_chat'), 'anthropic': ('Anthropic', 'anthropic_messages'),
    'gemini': ('Google Gemini', 'google_genai'), 'qwen': ('通义千问', 'openai_chat'),
    'kimi': ('Kimi', 'openai_chat'), 'glm': ('智谱 GLM', 'openai_chat'),
    'openai_compatible': ('OpenAI 兼容服务', 'openai_chat'), 'ollama': ('本地 Ollama', 'ollama_chat'),
}
DEFAULT_MODELS = (
    {'provider':'offline','model':'deterministic-v1','efforts':[],'verified':True},
    {'provider':'openai','model':'gpt-6-astra','efforts':['low','medium','high'],'verified':True},
    {'provider':'deepseek','model':'deepseek-flash','efforts':['low','high','max'],'verified':True},
    {'provider':'deepseek','model':'deepseek-v4-pro','efforts':['low','high','max'],'verified':True},
    {'provider':'anthropic','model':'claude-opus-5-5','efforts':['low','medium','high','xhigh','max'],'verified':True,
     'source':'https://platform.claude.com/docs/en/build-with-claude/effort','checked_at':'2026-10-30'},
    {'provider':'anthropic','model':'claude-sonnet-5-5','efforts':['low','medium','high','xhigh','max'],'verified':True,
     'source':'https://platform.claude.com/docs/en/build-with-claude/effort','checked_at':'2026-10-30'},
    {'provider':'gemini','model':'gemini-3.6-flash','efforts':[],'verified':True,
     'source':'https://ai.google.dev/gemini-api/docs/thinking/','checked_at':'2026-10-30',
     'notice':'模型ID有官方记录；当前适配器使用generateContent，未把Interactions专有推理参数冒充已验证。'},
    {'provider':'qwen','model':'qwen3.7-plus','efforts':[],'verified':True,
     'native_parameters':{'enable_thinking':{'type':'boolean'}},
     'source':'https://www.alibabacloud.com/help/en/model-studio/deep-thinking','checked_at':'2026-10-30'},
    {'provider':'kimi','model':'kimi-k3.1-pro','efforts':[],'verified':True,
     'source':'https://platform.kimi.ai/docs/get-your-api-key','checked_at':'2026-10-30',
     'notice':'当前适配器未验证此型号reasoning_effort，不开放该选项。'},
    {'provider':'glm','model':'glm-5','efforts':[],'verified':True,
     'source':'https://docs.z.ai/guides/llm/glm-5','checked_at':'2026-10-30'},
)
DEFAULT_BASE_URLS={
    'openai':'https://api.openai.com/v1','deepseek':'https://api.deepseek.com',
    'anthropic':'https://api.anthropic.com','gemini':'https://generativelanguage.googleapis.com',
    'qwen':'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
    'kimi':'https://api.moonshot.ai/v1','glm':'https://api.z.ai/api/paas/v4',
    'ollama':'http://127.0.0.1:11434','massive':'https://api.massive.com','tushare':'https://api.tushare.pro/',
}

def canonical_base_url(value):
    try:
        parsed=urlsplit(value)
        if (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or not parsed.hostname or parsed.scheme not in {'http','https'}
                or parsed.scheme=='http' and parsed.hostname not in {'127.0.0.1','::1'}):raise ValueError()
        host=parsed.hostname.lower()
        if ':'in host:host='['+host+']'
        port=parsed.port
        if port is not None and port not in ({443}if parsed.scheme=='https'else{80}):host+=':'+str(port)
        return urlunsplit((parsed.scheme,host,parsed.path.rstrip('/'),'',''))
    except (TypeError,ValueError):raise PlatformError('MODEL_ENDPOINT_INVALID',422)from None


class ModelCapabilities:
    def __init__(self, models=None, *, endpoints=None, adapter_status=None,env_bindings=None,default_targets=None):
        self.models = {(r['provider'],r['model']):deepcopy(r) for r in (DEFAULT_MODELS if models is None else models)}
        self.endpoints = deepcopy(endpoints or {})
        self.adapters = deepcopy(adapter_status or {})
        self.default_targets=deepcopy(DEFAULT_BASE_URLS if default_targets is None else default_targets)
        # Static default destinations, not mutable endpoint aliases. Explicit
        # nonstandard env bindings must likewise supply a destination or hash.
        self.env_bindings=deepcopy({('env:FA_'+p.upper()+'_API_KEY'):{'provider':p,'endpoint_id':None,'base_url':url}
            for p,url in DEFAULT_BASE_URLS.items() if p!='ollama'} if env_bindings is None else env_bindings)

    def resolve_target(self,owner_id,provider,endpoint_id):
        if type(owner_id)is not str or not owner_id:raise PlatformError('FORBIDDEN',403)
        if endpoint_id is None:url=self.default_targets.get(provider)
        else:
            row=self.endpoints.get(endpoint_id,{})
            if row.get('provider')!=provider:raise PlatformError('ENDPOINT_PROFILE_REQUIRED',422)
            url=row.get('base_url',row.get('url'))
        if url is None:raise PlatformError('ENDPOINT_PROFILE_REQUIRED',422)
        from finance_platform.operations.provider_credentials import canonical_target
        target=canonical_target(provider,endpoint_id,url)
        return {key:target[key]for key in ('provider','endpoint_id','base_url')}

    def validate_target(self,choice,*,owner_id,metadata_reader=None,target=None,reference_validator=None):
        reference=choice.get('secret_ref')
        if reference is None:return
        if (target is None or target.provider!=choice['provider'] or target.endpoint_id!=choice.get('endpoint_profile')):
            raise PlatformError('CREDENTIAL_TARGET_UNVERIFIED',409)
        if reference.startswith('credential:'):
            if reference_validator is not None:
                reference_validator(owner_id,reference,target=target)
                return
            if metadata_reader is None:raise PlatformError('CREDENTIAL_BINDING_SERVICE_UNAVAILABLE',409)
            binding=metadata_reader(owner_id,reference)
            if binding.get('configured')is not True or binding.get('binding_verified')is not True:
                raise PlatformError('CREDENTIAL_NOT_CONFIGURED',409)
            if binding.get('target_sha256')!=target.target_sha256:raise PlatformError('CREDENTIAL_TARGET_MISMATCH',409)
        else:
            binding=self.env_bindings.get(reference)
            if binding is None:raise PlatformError('CREDENTIAL_TARGET_UNVERIFIED',409)
            if binding.get('target_sha256')is not None:
                if binding['target_sha256']!=target.target_sha256:raise PlatformError('CREDENTIAL_TARGET_MISMATCH',409)
            elif binding.get('base_url')is not None:
                from finance_platform.operations.provider_credentials import canonical_target
                expected=canonical_target(binding.get('provider'),binding.get('endpoint_id'),binding['base_url'])
                if expected['target_sha256']!=target.target_sha256:raise PlatformError('CREDENTIAL_TARGET_MISMATCH',409)
            else:raise PlatformError('CREDENTIAL_TARGET_UNVERIFIED',409)
        if binding.get('provider')!=choice['provider'] or binding.get('endpoint_id')!=choice.get('endpoint_profile'):
            raise PlatformError('CREDENTIAL_TARGET_MISMATCH',409)

    def validate(self, choice):
        provider = choice['provider']
        if provider not in PROVIDERS:raise PlatformError('UNSUPPORTED_CAPABILITY',422)
        row = self.models.get((provider,choice['model']))
        effort = choice.get('effort')
        if effort is not None and (not row or row.get('verified')is not True or effort not in row.get('efforts',())):
            raise PlatformError('UNSUPPORTED_CAPABILITY',422)
        if provider=='offline' and choice['model']!='deterministic-v1':
            raise PlatformError('UNSUPPORTED_CAPABILITY',422)
        params = choice.get('native_parameters',{})
        rules = {} if not row or row.get('verified')is not True else row.get('native_parameters',{})
        if set(params)-set(rules):raise PlatformError('UNSUPPORTED_CAPABILITY',422)
        for key,value in params.items():
            rule=rules[key]
            if rule.get('type')=='integer':
                if type(value)is not int or not rule['minimum']<=value<=rule['maximum']:
                    raise PlatformError('UNSUPPORTED_CAPABILITY',422)
            elif rule.get('type')=='boolean':
                if type(value)is not bool:raise PlatformError('UNSUPPORTED_CAPABILITY',422)
            elif rule.get('type')=='enum':
                if type(value)is not str or value not in rule['values']:raise PlatformError('UNSUPPORTED_CAPABILITY',422)
            else:raise PlatformError('UNSUPPORTED_CAPABILITY',422)
        endpoint=choice.get('endpoint_profile')
        if endpoint is not None and self.endpoints.get(endpoint,{}).get('provider')!=provider:
            raise PlatformError('UNSUPPORTED_CAPABILITY',422)
        if provider=='openai_compatible' and endpoint is None:
            raise PlatformError('ENDPOINT_PROFILE_REQUIRED',422)
        return deepcopy(row)

    def validate_budgets(self,choice,budgets):
        row=self.validate(choice)
        if row and row.get('verified')is True:
            for key,cap in (('input_tokens','max_input_tokens'),('output_tokens','max_output_tokens')):
                if type(row.get(cap))is int and budgets[key]>row[cap]:raise PlatformError('UNSUPPORTED_CAPABILITY',422)

    def describe(self):
        return {'providers':[{'id':p,'label':label,'protocol':protocol,'custom_model_id':p!='offline',
                    'configuration_supported':True,
                    'adapter_status':self.adapters.get(p,'AVAILABLE' if p=='offline' else 'CONFIGURABLE_RUNTIME_CHECKED_AT_EXECUTION'),
                    'credentials_required':p not in {'offline','ollama'}} for p,(label,protocol) in PROVIDERS.items()],
                'models':deepcopy(list(self.models.values())),
                'endpoint_profiles':[{'id':key,'provider':row['provider']} for key,row in self.endpoints.items()],
                'unknown_model_policy':'允许自定义模型ID；未知能力不开放推理档位或额外参数。',
                'env_reference_targets':deepcopy(self.env_bindings),
                'provider_calls_in_acceptance':'NOT_RUN'}

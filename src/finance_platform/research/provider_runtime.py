"""Native official SDK adapters, fixed origins and explicit parameter routing.

No credentials are discovered here. The trusted owner-scoped resolver injects
one secret immediately before execution. Endpoint IDs resolve in server config.
"""
import os
from threading import Lock
import httpx
# Match the existing native-worker policy: scoped keys only, no dotenv or payload tracing.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["OTEL_SDK_DISABLED"] = "true"

from .portfolio import fail
from finance_platform.core.errors import PlatformError

ENDPOINTS={"qwen":"https://dashscope-intl.aliyuncs.com/compatible-mode/v1","kimi":"https://api.moonshot.ai/v1","glm":"https://api.z.ai/api/paas/v4"}
DEFAULT_TARGETS={**ENDPOINTS,"openai":"https://api.openai.com/v1","deepseek":"https://api.deepseek.com","anthropic":"https://api.anthropic.com",
    "gemini":"https://generativelanguage.googleapis.com","ollama":"http://127.0.0.1:11434"}
NATIVE_PARAMETERS={"anthropic":{"thinking_budget","thinking_mode"},"gemini":{"thinking_budget","thinking_level","include_thoughts"},
    "qwen":{"enable_thinking","thinking_budget"},"kimi":{"thinking"},"glm":{"thinking"},"ollama":{"reasoning","num_ctx"},"openai_compatible":set()}


class FencedTransport(httpx.BaseTransport):
    """Allow one synchronous SDK handoff after the caller's current effect check."""

    def __init__(self, active, injected=None):
        self.active = active
        self.injected = injected
        self.inner = httpx.HTTPTransport(trust_env=False, retries=0) if injected is None else None
        self.admitted = False
        self.closed = False
        self.lock = Lock()

    def handle_request(self, request):
        with self.lock:
            if self.closed or self.admitted:
                raise PlatformError('CONFLICT', 409)
            self.active()
            self.admitted = True
        if self.injected is not None:
            return self.injected.send(request, stream=True, follow_redirects=False)
        return self.inner.handle_request(request)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        if self.inner is not None:
            self.inner.close()


def create_client(choice,*,output_tokens,timeout_seconds,retry,secret=None,transport=None,endpoint_resolver=None,target=None):
    if type(choice)is not dict or set(choice)-{"provider","model","effort","secret_ref","endpoint_profile","native_parameters"}:fail("MODEL_CHOICE_INVALID")
    provider=choice.get("provider");model=choice.get("model");effort=choice.get("effort")
    frozen_url=None
    if target is not None:
        from finance_platform.operations.provider_credentials import FrozenTarget
        if type(target)is not FrozenTarget or target.provider!=provider or target.endpoint_id!=choice.get("endpoint_profile"):fail("MODEL_FROZEN_TARGET_MISMATCH")
        frozen_url=target.base_url
    if type(model)is not str or not 1<=len(model)<=200:fail("MODEL_ID_INVALID")
    if type(output_tokens)is not int or not 1<=output_tokens<=131072 or type(timeout_seconds)is not int or not 1<=timeout_seconds<=600 or type(retry)is not int or not 0<=retry<=2:fail("MODEL_BUDGET_INVALID")
    params=choice.get("native_parameters",{})
    if type(params)is not dict or set(params)-NATIVE_PARAMETERS.get(provider,set()):fail("MODEL_PARAMETER_UNSUPPORTED")
    if provider!="ollama"and(type(secret)is not str or not secret):fail("MODEL_SECRET_REQUIRED")
    # Core capability validation is authoritative for a concrete model. This
    # adapter additionally refuses undocumented provider effort translations.
    if effort is not None:
        allowed={"openai":{"none","minimal","low","medium","high","xhigh"},"deepseek":{"low","high","max"},
            "anthropic":{"low","medium","high","xhigh","max"},"gemini":{"minimal","low","medium","high"}}
        if effort not in allowed.get(provider,set()):fail("MODEL_EFFORT_UNSUPPORTED")
    if provider in {"openai","deepseek"}:
        if params or choice.get("endpoint_profile")is not None and target is None:fail("MODEL_PARAMETER_UNSUPPORTED")
        if target is not None:
            from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI,DeepSeekChatOpenAI
            args={"model":model,"api_key":secret,"base_url":frozen_url,"timeout":timeout_seconds,"max_retries":retry,"cache":False}
            if transport is not None:args["http_client"]=transport
            if effort is not None:args["reasoning_effort"]=effort
            if provider=="openai":return NormalizedChatOpenAI(**args,max_tokens=output_tokens,use_responses_api=True)
            args["extra_body"]={"max_tokens":output_tokens}
            if effort is not None:args["extra_body"]["thinking"]={"type":"enabled"}
            return DeepSeekChatOpenAI(**args)
        from .team_models import official_client
        return official_client({k:choice.get(k)for k in("provider","model","effort","secret_ref")},output_tokens=output_tokens,timeout_seconds=timeout_seconds,retry=retry,secret=secret,transport=transport)
    if provider=="anthropic":
        if choice.get("endpoint_profile")is not None and target is None:fail("MODEL_PARAMETER_UNSUPPORTED")
        from langchain_anthropic import ChatAnthropic
        args={"model":model,"api_key":secret,"base_url":frozen_url or DEFAULT_TARGETS[provider],"max_tokens":output_tokens,"timeout":timeout_seconds,"max_retries":retry,"cache":False}
        if params:
            mode=params.get("thinking_mode","enabled"if "thinking_budget"in params else"adaptive")
            if mode not in {"enabled","adaptive","disabled"}:fail("MODEL_PARAMETER_UNSUPPORTED")
            args["thinking"]={"type":mode}
            if "thinking_budget"in params:
                count=params["thinking_budget"]
                if type(count)is not int or not 1024<=count<output_tokens or mode!="enabled":fail("MODEL_THINKING_BUDGET_INVALID")
                args["thinking"]["budget_tokens"]=count
        if effort is not None:
            config=dict(args.get("output_config",{}))
            if "effort"in config and config["effort"]!=effort:fail("MODEL_EFFORT_CONFLICT")
            config["effort"]=effort;args["output_config"]=config
        model_client=ChatAnthropic(**args)
        if transport is not None:
            # This pinned integration has no public http_client model field;
            # passing it becomes an invalid Messages API parameter. Inject one
            # instance-local cached SDK client using Anthropic's public client
            # transport API; model/tool/message serialization remains upstream.
            import anthropic
            model_client.__dict__["_client"]=anthropic.Anthropic(api_key=secret,base_url=frozen_url or DEFAULT_TARGETS[provider],timeout=timeout_seconds,max_retries=retry,http_client=transport)
        return model_client
    if provider=="gemini":
        if choice.get("endpoint_profile")is not None and target is None:fail("MODEL_PARAMETER_UNSUPPORTED")
        from langchain_google_genai import ChatGoogleGenerativeAI
        if "thinking_budget"in params and(effort is not None or "thinking_level"in params):fail("MODEL_THINKING_PARAMETER_CONFLICT")
        args={"model":model,"api_key":secret,"base_url":frozen_url or DEFAULT_TARGETS[provider],"vertexai":False,"max_output_tokens":output_tokens,"timeout":timeout_seconds,"max_retries":retry,"cache":False,**params}
        if effort is not None:
            if "thinking_level"in params and params["thinking_level"]!=effort:fail("MODEL_EFFORT_CONFLICT")
            args["reasoning_effort"]=effort
        if transport is not None:
            # Official google-genai HttpOptions exposes httpx_client. Do not
            # pass unknown kwargs that a model could silently ignore.
            args["client_args"]={"transport":transport,"trust_env":False,"follow_redirects":False}
        return ChatGoogleGenerativeAI(**args)
    if provider in {*ENDPOINTS,"openai_compatible"}:
        from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI
        endpoint=frozen_url or ENDPOINTS.get(provider)
        if target is None and choice.get("endpoint_profile")is not None:
            if endpoint_resolver is None:fail("MODEL_TRUSTED_ENDPOINT_REQUIRED")
            endpoint=endpoint_resolver(choice["endpoint_profile"],provider)
        if endpoint is None:fail("MODEL_TRUSTED_ENDPOINT_REQUIRED")
        args={"model":model,"api_key":secret,"base_url":endpoint,"timeout":timeout_seconds,"max_retries":retry,"cache":False,
            "extra_body":{"max_tokens":output_tokens,**params}}
        if transport is not None:args["http_client"]=transport
        return NormalizedChatOpenAI(**args)
    if provider=="ollama":
        if retry:fail("MODEL_OLLAMA_RETRY_UNSUPPORTED")
        from langchain_ollama import ChatOllama
        endpoint=frozen_url or "http://127.0.0.1:11434"
        if target is None and choice.get("endpoint_profile")is not None:
            if endpoint_resolver is None:fail("MODEL_TRUSTED_ENDPOINT_REQUIRED")
            endpoint=endpoint_resolver(choice["endpoint_profile"],provider)
        import httpx
        class ScopedAuth(httpx.Auth):
            def auth_flow(self,request):
                request.headers.pop("authorization",None)
                if secret:request.headers["authorization"]="Bearer "+secret
                yield request
        # SDK0.6.3 consults OLLAMA_API_KEY internally. Its fallback must never
        # be forwarded instead of the explicitly scoped reference. Auth is a
        # public httpx extension, not an environment or SDK monkeypatch.
        client_kwargs={"timeout":timeout_seconds,"headers":{"authorization":"Bearer SCOPED_LOCAL"},"auth":ScopedAuth()}
        if transport is not None:client_kwargs.update(transport=transport,trust_env=False,follow_redirects=False)
        return ChatOllama(model=model,base_url=endpoint,num_predict=output_tokens,client_kwargs=client_kwargs,validate_model_on_init=False,**params)
    fail("MODEL_PROVIDER_UNSUPPORTED")



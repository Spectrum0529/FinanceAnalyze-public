"""First-party OpenAI Responses under the existing TA supervisor.

The child submits only typed model inputs on its private channel. The parent
owns target selection, credentials, effective request and cumulative budget.
"""
from threading import Lock
from time import monotonic

from finance_platform.research import tradingagents_workflow as native
from .ta_protocol import checked, digest, encoded

OPENAI_URL = "https://api.openai.com/v1"
COUNT_FIELDS = frozenset({"model", "input", "instructions", "parallel_tool_calls", "reasoning",
                          "text", "tool_choice", "tools", "truncation"})
CREATE_FIELDS = COUNT_FIELDS | {"max_output_tokens", "store", "stream", "include"}
TEXT_BLOCKS = {"input_text", "output_text", "refusal"}


def text_content(value):
    return type(value) is str or type(value) is list and all(
        type(block) is dict and block.get("type") in TEXT_BLOCKS
        and not block.get("annotations")
        and type(block.get("refusal" if block["type"]=="refusal" else "text")) is str
        for block in value)


def allowed_input(value):
    if type(value) is str:
        return True
    if type(value) is not list or not value:
        return False
    for item in value:
        if type(item) is not dict:
            return False
        kind = item.get("type", "message")
        if kind == "message" and item.get("role") in {"user","assistant","developer","system"}:
            if "id" not in item and text_content(item.get("content")):
                continue
        if kind == "function_call" and all(type(item.get(key)) is str for key in ("call_id","name","arguments")):
            continue
        if kind == "function_call_output" and type(item.get("call_id")) is str and text_content(item.get("output")):
            continue
        if kind == "reasoning" and type(item.get("encrypted_content")) is str:
            continue
        return False
    return True


def fail(code):
    native.fail(code)


def provider_target(item):
    from finance_platform.operations.provider_credentials import FrozenTarget
    choice = item["model"]
    target = item["execution_target"]
    if (choice["provider"] != "openai" or choice.get("endpoint_profile") is not None
            or choice.get("native_parameters") or not (choice.get("secret_ref") or "").startswith("credential:")
            or type(target) is not dict or target.get("base_url") != OPENAI_URL
            or target.get("provider") != "openai" or target.get("endpoint_id") is not None):
        fail("TA_PROVIDER_EXACT_METER_UNAVAILABLE")
    return FrozenTarget(**target)


class ProviderMeter:
    def __init__(self, options, *, initial, active, clock=monotonic):
        self.options = native.options(options)
        if not native.PROVIDER_TOKEN_LIMITS.keys() <= self.options.keys():
            fail("TA_PROVIDER_BUDGET_STATE_INVALID")
        previous = (dict.fromkeys(native.budget_limits(self.options), 0) if initial is None
                    else native.consumed(initial, self.options))
        self.spent = {key: value for key, value in previous.items() if key.startswith("provider_")}
        self.active = active
        self.clock = clock
        self.started = clock()
        self.previous_wall = previous["wall_millis"]
        self.deadline = self.started + self.options["wall_seconds"] - self.previous_wall / 1000
        self.lock = Lock()
        self.blocked = None

    def wall_millis(self):
        return min(self.options["wall_seconds"]*1000,
                   self.previous_wall+int(max(0,self.clock()-self.started)*1000))

    def remaining(self):
        self.active()
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            fail("TA_WALL_BUDGET_EXCEEDED")
        return remaining

    def invoke(self, payload, *, model, role_limits, count, create):
        with self.lock:
            if self.blocked:
                fail(self.blocked)
            try:
                return self._invoke(payload, model, role_limits, count, create)
            except native.DebateBoundaryError as error:
                self.blocked = error.code
                raise

    def _invoke(self, payload, model, role_limits, count, create):
        self.remaining()
        if (type(payload) is not dict or not {"model", "input", "max_output_tokens"} <= payload.keys()
                or payload.keys() - CREATE_FIELDS or payload["model"] != model
                or payload.get("stream") not in (None, False)
                or payload.get("truncation") not in (None, "disabled")
                or payload.get("include") not in (None, [], ["reasoning.encrypted_content"])
                or type(payload["max_output_tokens"]) is not int
                or not allowed_input(payload["input"])
                or len(encoded(payload)) > 262144):
            fail("TA_PROVIDER_REQUEST_INVALID")
        tools=payload.get("tools",[])
        if type(tools) is not list or any(type(tool) is not dict or tool.get("type") != "function" for tool in tools):
            fail("TA_PROVIDER_REQUEST_INVALID")
        if self.spent["provider_calls"] >= self.options["model_calls"]:
            fail("TA_PROVIDER_CALL_BUDGET_EXCEEDED")
        max_in = min(role_limits["input_tokens"],
                     self.options["provider_input_tokens"] - self.spent["provider_input_tokens"])
        max_out = min(role_limits["output_tokens"], payload["max_output_tokens"],
                      self.options["provider_output_tokens"] - self.spent["provider_output_tokens"])
        # OpenAI Responses requires a generation allowance of at least 16.
        if max_in < 1 or max_out < native.PROVIDER_MIN_OUTPUT_TOKENS:
            fail("TA_PROVIDER_TOKEN_BUDGET_EXHAUSTED")
        counting = {key: value for key, value in payload.items() if key in COUNT_FIELDS}
        counting["truncation"] = "disabled"
        try:
            tokens = count(counting)
        except Exception:
            fail("TA_PROVIDER_INPUT_COUNT_UNAVAILABLE")
        if type(tokens) is not int or tokens < 1:
            fail("TA_PROVIDER_INPUT_COUNT_UNAVAILABLE")
        if tokens > max_in:
            fail("TA_PROVIDER_INPUT_BUDGET_EXCEEDED")
        self.remaining()
        self.spent["provider_calls"] += 1
        self.spent["provider_input_tokens"] += tokens
        self.spent["provider_output_tokens"] += max_out
        try:
            result = create({**counting, "max_output_tokens": max_out, "store": False, "stream": False,
                             **({"include": payload["include"]} if payload.get("include") else {})})
        except Exception:
            fail("TA_PROVIDER_USAGE_UNKNOWN")
        usage = result.get("usage") if type(result) is dict else None
        if (type(usage) is not dict or type(usage.get("input_tokens")) is not int
                or type(usage.get("output_tokens")) is not int or usage["input_tokens"] != tokens
                or not 0 <= usage["output_tokens"] <= max_out):
            fail("TA_PROVIDER_USAGE_UNKNOWN")
        self.spent["provider_output_tokens"] -= max_out - usage["output_tokens"]
        self.remaining()
        return result


class ProviderSession:
    """One already admitted native invocation with current queue fencing."""
    def __init__(self, context, request, initial, credential_store, *, transport=None):
        self.bundle = request["input_bundle"]
        self.snapshot = self.bundle["team_snapshot"]
        self.context = context
        self.meter = ProviderMeter(request["binding"]["options"], initial=initial,
                                   active=context.require_active)
        self.credential_store = credential_store
        self.transport = transport

    def verify_execution(self, value, binding):
        approval=self.bundle["provider_approval"]
        if (type(value) is not dict or set(value)!={"execution_sha256","request"}
                or value["execution_sha256"]!=approval["execution_sha256"]
                or binding["tools_manifest_hash"]!=digest(self.bundle)
                or binding["data_reference"]!=self.bundle["data_reference"]
                or binding["options"]!=approval["options"]
                or binding["model_profile"]["profile_sha256"]!=self.snapshot["sha256"]):
            fail("TA_EXTERNAL_EXECUTION_BINDING_CHANGED")

    def invoke(self, value):
        try:
            msg = self._invoke(value)
        except Exception as error:
            reason = getattr(error, "code", "TA_PROVIDER_USAGE_UNKNOWN")
            self.meter.blocked = self.meter.blocked or reason
            return {"message": None, "spent": dict(self.meter.spent), "error_code": reason}
        return {"message": msg, "spent": dict(self.meter.spent), "error_code": None}

    def _invoke(self, value):
        import httpx2
        from openai import OpenAI
        from langchain_core.messages import messages_from_dict
        from finance_platform.research.team_models import official_client

        if (type(value) is not dict or set(value) != {"role", "messages", "tools", "tool_choice"}
                or value["role"] not in self.snapshot["resolved_roles"]
                or type(value["messages"]) is not list or not value["messages"]
                or type(value["tools"]) is not list or len(value["tools"]) > 32
                or any(type(row) is not dict or row.get("type") not in {"human", "system", "ai", "tool", "developer"}
                       for row in value["messages"]) or len(encoded(value))>262144):
            fail("TA_PROVIDER_REQUEST_INVALID")
        role = self.snapshot["resolved_roles"][value["role"]]
        choice = role["model"]
        limits = role.get("budgets") or self.snapshot["configuration"]["budgets"]
        target = provider_target(role)
        owner = self.context.run.owner_id
        self.meter.remaining()
        if self.credential_store.freeze_target(owner, "openai", None) != target:
            fail("TA_PROVIDER_TARGET_CHANGED")
        secret = self.credential_store.resolve_for_provider(owner, choice["secret_ref"], target=target)
        messages = messages_from_dict([{"type": row["type"], "data": row} for row in value["messages"]])
        wire_calls = 0
        with httpx2.Client(transport=self.transport, trust_env=False, follow_redirects=False) as upstream:
            sdk = OpenAI(api_key=secret, base_url=target.base_url, http_client=upstream, max_retries=0)

            def wire(outbound):
                nonlocal wire_calls
                wire_calls += 1
                if wire_calls != 1 or outbound.method != "POST" or str(outbound.url) != OPENAI_URL+"/responses":
                    fail("TA_PROVIDER_REQUEST_INVALID")
                body = checked(outbound.read())
                try:
                    result = self.meter.invoke(body, model=choice["model"], role_limits=limits,
                        count=lambda params: sdk.responses.input_tokens.count(
                            **params, timeout=self.meter.remaining()).input_tokens,
                        create=lambda params: sdk.responses.create(
                            **params, timeout=self.meter.remaining()).model_dump(mode="json"))
                except native.DebateBoundaryError:
                    # The SDK wraps transport exceptions. Keep the meter's
                    # reason and spent state without passing provider details.
                    return httpx2.Response(422, json={"error": {"message": "TA provider request stopped"}})
                return httpx2.Response(200, json=result)

            with httpx2.Client(transport=httpx2.MockTransport(wire), trust_env=False) as fenced:
                client = official_client(choice, output_tokens=limits["output_tokens"], timeout_seconds=120,
                                         retry=0, secret=secret, transport=fenced, target=target)
                if value["tools"]:
                    client = client.bind_tools(value["tools"], tool_choice=value["tool_choice"])
                try:
                    result = client.invoke(messages, stream=False, store=False)
                except Exception:
                    fail(self.meter.blocked or "TA_PROVIDER_RESPONSE_INVALID")
        if wire_calls != 1 or getattr(result, "type", None) != "ai":
            fail("TA_PROVIDER_RESPONSE_INVALID")
        return result.model_dump(mode="json", exclude={"response_metadata"})

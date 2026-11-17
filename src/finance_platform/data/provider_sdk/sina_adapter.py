"""Pinned AKShare stock_us_daily prefix, explicit unadjusted early return.

Only the fixed local decoder/wrapper execute in V8; remote text is never code.
The installed SDK and its module globals are never modified.
"""
import ast
from copy import deepcopy
from decimal import Decimal
import hashlib
from importlib import metadata
import inspect
import json
from pathlib import Path
import re
import time
from types import FunctionType, SimpleNamespace

from finance_platform.data.contracts import DataError
from .contracts import SINA_INTERFACE, valid_sina_symbol, LIMITS, snapshot_frame, observed_row
from .history_contracts import HISTORY_PROFILE, SINA_FULL_ROWS, SINA_JSON_BYTES, SINA_QFQ_INTERFACE, SINA_PRICE_POLICY, QUANTITY_BASIS
from .sina_transport import SinaHTTP

ADAPTATION = "SINA_UNADJUSTED_EARLY_RETURN_V1"
HEAP_BYTES = 128 * 1024**2
FULL_ROWS = 20000
JSON_BYTES = 2 * 1024**2


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def verify_runtime(pins, budget):
    """No mini-racer/AKShare import before every Python/DLL/ICU pin is checked."""
    budget.check()
    pin = pins["sina"]["runtime"]
    try:
        dist = metadata.distribution("mini-racer")
    except metadata.PackageNotFoundError:
        budget.abort("SDK_DEPENDENCY_MISSING")
    if dist.version != pin["version"] or pin["name"] != "mini-racer":
        budget.abort("SDK_VERSION_UNREVIEWED")
    # Reject extra executable/module files as well as missing or changed files.
    installed = {str(f).replace("\\", "/") for f in dist.files or ()
                 if str(f).replace("\\", "/").startswith("py_mini_racer/")
                 and not str(f).endswith(".pyc") and "__pycache__" not in str(f)}
    if installed != set(pin["modules"]):
        budget.abort("SDK_INTERFACE_CHANGED")
    for relative, digest in pin["modules"].items():
        budget.check()
        path = Path(dist.locate_file(relative))
        raw = path.read_bytes()
        if sha(raw.replace(b"\r\n", b"\n") if path.suffix == ".py" else raw) != digest:
            budget.abort("SDK_INTERFACE_CHANGED")
    budget.check()
    return {**{k: pin[k] for k in ("name", "version", "artifact_sha256")},
            "modules_manifest_sha256": sha(json.dumps(pin["modules"], sort_keys=True, separators=(",", ":")).encode()),
            "native_dll_sha256": pin["modules"]["py_mini_racer/mini_racer.dll"],
            "icu_sha256": pin["modules"]["py_mini_racer/icudtl.dat"]}


def extracted_payload(raw, symbol):
    return parsed_assignment(raw, symbol)[0]


def parsed_assignment(raw, symbol):
    if type(raw) is not bytes or not 0 < len(raw) <= LIMITS["response_bytes"] or not valid_sina_symbol(symbol):
        raise DataError("SDK_RESPONSE_LIMIT_EXCEEDED")
    try:
        text = raw.decode("ascii")
    except UnicodeError:
        raise DataError("SINA_ASSIGNMENT_INVALID") from None
    # Deliberately narrow. No eval, JSONP, comments, escapes or trailing scripts.
    # Exact original label or K2 label observed through reviewed public metadata.
    # No other prefix/identifier, suffix-only or case-insensitive acceptance.
    match = re.fullmatch(r'\s*(?:var\s+)?KLC_(KL|K2)_' + re.escape(symbol) + r'\s*=\s*"([A-Za-z0-9+/]+)"\s*;\s*', text)
    if not match:
        raise DataError("SINA_ASSIGNMENT_INVALID")
    return match[2], "KLC_K2_TICKER" if match[1] == "K2" else "KLC_KL_TICKER"


def assignment_shape(raw, symbol):
    """Classify bounded text into fixed enums only; never execute or accept it."""
    if type(raw) is not bytes or len(raw) > LIMITS["response_bytes"] or not valid_sina_symbol(symbol):
        raise DataError("SDK_RESPONSE_LIMIT_EXCEEDED")
    result = {"schema_version": "sina-assignment-shape-1", "bytes": len(raw),
              "encoding": "ASCII", "content_class": "OTHER", "declaration": "UNPARSED",
              "label_binding": "UNPARSED", "quote": "UNPARSED", "terminator": "UNPARSED",
              "payload_class": "UNPARSED", "single_assignment": False, "literal_has_escapes": False}
    if not raw:
        return {**result, "encoding": "EMPTY", "content_class": "EMPTY"}
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        result["encoding"] = "UTF8_BOM_ASCII"
    try:
        text = raw.decode("ascii").strip(" \t\r\n")
    except UnicodeError:
        return {**result, "encoding": "NON_ASCII"}
    if text.startswith("<"):
        return {**result, "content_class": "HTML_OR_MARKUP"}
    lhs = re.match(r"(?:(var)[ \t\r\n]+)?([A-Za-z_$][A-Za-z0-9_$]{0,95})[ \t\r\n]*=[ \t\r\n]*", text)
    if not lhs:
        return result
    result.update(content_class="ASSIGNMENT_PREFIX", declaration="VAR" if lhs[1] else "NONE")
    label = lhs[2]
    plain, k2 = "KLC_KL_" + symbol, "KLC_K2_" + symbol
    if label == plain:
        result["label_binding"] = "EXACT_KLC_KL"
    elif label == k2:
        result["label_binding"] = "EXACT_KLC_K2"
    elif label.upper() in {plain, k2}:
        result["label_binding"] = "CASE_VARIANT_KNOWN"
    elif label.endswith(symbol):
        result["label_binding"] = "SUFFIX_ONLY_UNTRUSTED"
    else:
        result["label_binding"] = "MISMATCH"
    start = lhs.end()
    if start >= len(text) or text[start] not in {'"', "'"}:
        return {**result, "quote": "NONE"}
    quote = text[start]
    result["quote"] = "DOUBLE" if quote == '"' else "SINGLE"
    end = start + 1
    while end < len(text):
        if text[end] == "\\":
            result["literal_has_escapes"] = True
            end += 2
            continue
        if text[end] == quote:
            break
        end += 1
    if end >= len(text):
        return result
    payload = text[start + 1:end]
    tail = text[end + 1:].strip(" \t\r\n")
    result["terminator"] = "SEMICOLON" if tail == ";" else "NONE" if not tail else "TRAILING_OTHER"
    result["single_assignment"] = tail in {"", ";"}
    if not payload:
        result["payload_class"] = "EMPTY"
    elif re.fullmatch(r"[A-Za-z0-9+/]+", payload):
        result["payload_class"] = "BASE64"
    elif re.fullmatch(r"[A-Za-z0-9+/]+={1,2}", payload):
        result["payload_class"] = "BASE64_PADDED"
    else:
        result["payload_class"] = "OTHER"
    return result


def capture_assignment(call, budget, response):
    """Keep failed entity in original private child artifact path, before grammar."""
    shape = assignment_shape(response.body, call["provider_symbol"])
    if budget.attempts:
        budget.attempts[-1]["sina_assignment_shape"] = shape
    entity = {"role": "sina_us_full_history", "origin": "finance.sina.com.cn",
              "path": "/staticdata/us/" + call["provider_symbol"], "parameters": {},
              "observed_received_at": response.received_at, "body": response.body,
              "content_decoding": "IDENTITY_ENTITY_NOT_TLS_WIRE", "content_encoding": "identity",
              "decoded_entity_bytes": len(response.body), **response.decoding,
              "media_type": "SINA_UNVALIDATED_ENTITY_NOT_FINANCIAL_JSON",
              "entity_sha256": sha(response.body), "entity_bytes": len(response.body),
              "assignment_validated": False}
    budget.entities.append(entity)
    encoded, label_template = parsed_assignment(response.body, call["provider_symbol"])
    entity.update(media_type="SINA_SINGLE_COMPRESSED_STRING_ASSIGNMENT_NOT_JSON", assignment_validated=True,
                  payload_sha256=sha(encoded.encode("ascii")), payload_bytes=len(encoded),
                  symbol_binding="EXACT_REQUEST_PATH_AND_ASSIGNMENT_LABEL", assignment_label_template=label_template)
    return entity, encoded


def adapted_tree(function):
    """Deterministic surgery on an exact hash-pinned function, not a reimplementation."""
    node = ast.parse(inspect.getsource(function).replace("\r\n", "\n")).body[0]
    cuts = [i for i, statement in enumerate(node.body)
            if ast.unparse(statement) == "data_df = data_df.astype('float')"]
    if len(cuts) != 1:
        raise DataError("SDK_INTERFACE_CHANGED")
    original_branch = [s for s in node.body if isinstance(s, ast.If) and ast.unparse(s.test) == "adjust == ''"]
    if len(original_branch) != 1 or [ast.unparse(s) for s in original_branch[0].body] != ["data_df.reset_index(inplace=True)", "return data_df"]:
        raise DataError("SDK_INTERFACE_CHANGED")
    node.body = node.body[:cuts[0] + 1]
    calls = [s for s in node.body if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "dict_list" for t in s.targets)]
    if len(calls) != 1 or ast.unparse(calls[0].value.func) != "js_code.call":
        raise DataError("SDK_INTERFACE_CHANGED")
    # Strict extracted payload replaces the original unsafe/brittle text split.
    calls[0].value.args[1] = ast.Attribute(value=ast.Name(id="res", ctx=ast.Load()), attr="encoded", ctx=ast.Load())
    node.body.extend(ast.parse("data_df = _select(data_df)").body)
    node.body.extend(deepcopy(original_branch[0].body))
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    tree = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    # The exact resulting AST identity is pinned in addition to original source.
    return tree


def adaptation_hash(function):
    return sha(ast.dump(adapted_tree(function), include_attributes=False).encode())


class GuardedJS:
    def __init__(self, decoder, wrapper, budget, *, json_bytes=JSON_BYTES, full_rows=FULL_ROWS):
        from py_mini_racer import MiniRacer
        budget.check()
        self.budget, self.decoder, self.wrapper = budget, decoder, wrapper
        self.json_bytes, self.full_rows = json_bytes, full_rows
        self.deadline = min(budget.deadline, time.monotonic() + 5)
        self.ctx = None
        self.loaded = self.called = False
        self.decoded_json = None
        try:
            self.ctx = MiniRacer()  # Only pinned runtime's local timer bootstrap.
            self.ctx.set_hard_memory_limit(HEAP_BYTES)
            self.remaining()
        except BaseException:
            self.close()
            raise

    def remaining(self):
        self.budget.check()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.budget.abort("SDK_TIMEOUT")
        return remaining

    def eval(self, code):
        if self.loaded or code != self.decoder:
            self.budget.abort("SDK_INTERFACE_CHANGED")
        try:
            self.ctx.eval(self.decoder, timeout_sec=self.remaining(), max_memory=HEAP_BYTES)
            self.ctx.eval(self.wrapper, timeout_sec=self.remaining(), max_memory=HEAP_BYTES)
            self.loaded = True
        except DataError:
            raise
        except Exception:
            self.budget.abort("SINA_DECODER_FAILED")

    def call(self, name, encoded):
        if not self.loaded or self.called or name != "d" or type(encoded) is not str or not re.fullmatch(r"[A-Za-z0-9+/]+", encoded):
            self.budget.abort("SDK_INTERFACE_CHANGED")
        self.called = True
        try:
            value = self.ctx.call("__fa_sina_decode", encoded, timeout_sec=self.remaining(), max_memory=HEAP_BYTES)
            self.remaining()
            if type(value) is not str or not 0 < len(value) <= self.json_bytes or not value.isascii():
                self.budget.abort("SINA_DECODED_JSON_LIMIT")
            self.decoded_json = value.encode("ascii")
            # Wrapper checked schema, full count, all numbers and size inside V8.
            rows = json.loads(value, parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(DataError("SINA_DECODED_INVALID")))
            if type(rows) is not list or not 0 < len(rows) <= self.full_rows:
                self.budget.abort("SINA_FULL_HISTORY_LIMIT")
            self.remaining()
            return rows
        except DataError:
            raise
        except Exception:
            self.budget.abort("SINA_DECODER_FAILED")

    def close(self):
        if self.ctx is not None:
            ctx, self.ctx = self.ctx, None
            ctx.close()


def sina(call, budget, pins, *, fixture_exchange=None):
    if call.get("interface") not in {SINA_INTERFACE, SINA_QFQ_INTERFACE} or not valid_sina_symbol(call.get("provider_symbol")):
        budget.abort("SDK_INTERFACE_CHANGED")
    if call.get("interface") == SINA_QFQ_INTERFACE and call["provider_symbol"] in {"CIEN", "AI"}:
        budget.abort("SINA_KNOWN_FACTOR_QUALITY_UNSUPPORTED")
    from .sdk_calls import verify_distribution
    sdk_ref = verify_distribution("akshare", pins)
    runtime = verify_runtime(pins, budget)
    # All SDK and runtime files were checked before these imports.
    import akshare as ak
    from akshare.stock.cons import zh_js_decode
    function = ak.stock_us_daily
    pin = pins["sina"]
    if (not isinstance(function, FunctionType) or function.__name__ != "stock_us_daily"
            or function.__module__ != "akshare.stock.stock_us_sina" or function.__closure__
            or sha(inspect.getsource(function).replace("\r\n", "\n").encode()) != pin["function_sha256"]
            or sha(zh_js_decode.encode()) != pin["decoder_sha256"] or adaptation_hash(function) != pin["adapted_ast_sha256"]):
        budget.abort("SDK_INTERFACE_CHANGED")
    long_history = call.get("profile") == HISTORY_PROFILE
    wrapper_key = "history_wrapper_sha256" if long_history else "wrapper_sha256"
    wrapper = Path(__file__).with_name("sina_decoder_history.js" if long_history else "sina_decoder.js").read_text(encoding="utf-8")
    if sha(wrapper.replace("\r\n", "\n").encode()) != pin[wrapper_key] or pin["adaptation"] != ADAPTATION:
        budget.abort("SDK_INTERFACE_CHANGED")
    port = SinaHTTP(call, budget, fixture_exchange=fixture_exchange)
    js = None
    selected = []
    full_rows = None
    entity = None

    def make_js():
        nonlocal js
        if js is not None:
            budget.abort("SDK_INTERFACE_CHANGED")
        js = GuardedJS(zh_js_decode, wrapper, budget, json_bytes=SINA_JSON_BYTES, full_rows=SINA_FULL_ROWS) if long_history else GuardedJS(zh_js_decode, wrapper, budget)
        return js

    class Requests:
        @staticmethod
        def get(url, **kwargs):
            nonlocal entity
            response = port.get(url, **kwargs)
            entity, encoded = capture_assignment(call, budget, response)
            return SimpleNamespace(encoded=encoded)

    def select(frame):
        nonlocal full_rows
        budget.check()
        full_rows = json.loads(js.decoded_json, parse_float=Decimal)
        if len(frame) != len(full_rows) or len(frame) > (SINA_FULL_ROWS if long_history else FULL_ROWS):
            budget.abort("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
        selected.extend(i for i, row in enumerate(full_rows) if call["start"] <= row["date"] < call["end"])
        if not 0 < len(selected) <= call["max_rows"]:
            budget.abort("PROVIDER_EMPTY_OR_FIELDS_MISSING" if not selected else "SDK_ROW_LIMIT_EXCEEDED")
        return frame.iloc[selected].copy()

    try:
        # No original globals with requests/eval/factor access are retained.
        namespace = {"__builtins__": {}, "pd": function.__globals__["pd"], "requests": Requests,
                     "zh_js_decode": zh_js_decode, "py_mini_racer": SimpleNamespace(MiniRacer=make_js), "_select": select}
        exec(compile(adapted_tree(function), "<pinned-sina-unadjusted-prefix>", "exec"), namespace)
        frame = namespace["stock_us_daily"](symbol=call["provider_symbol"], adjust="")
        budget.check()
        snapshot = snapshot_frame(frame, call["max_rows"])
        proof = {"decoded_json_sha256": sha(js.decoded_json), "decoded_json_bytes": len(js.decoded_json),
                 "full_history_rows": len(full_rows), "full_history_start": full_rows[0]["date"], "full_history_end": full_rows[-1]["date"],
                 "selected_full_history_indices": selected, "acquisition_scope": "BOUNDED_FULL_HISTORY_THEN_LOCAL_WINDOW",
                 "selected_window_rows": len(selected), "requested_window": {k: call[k] for k in ("start", "end")},
                 "numeric_representation": "JS_BINARY64_JSON_SHORTEST_ROUNDTRIP_NOT_ORIGINAL_DECIMAL_LEXEME"}
        entity.update({k: v for k, v in proof.items() if k != "selected_full_history_indices"})
        rows = normalize_sina(call, snapshot, full_rows, selected, entity, proof)
        adjustment = {}
        if long_history:
            if call.get("interface") != SINA_QFQ_INTERFACE: budget.abort("SDK_INTERFACE_CHANGED")
            from .sina_factors import parse_factors, adjust_rows, TRANSFORM
            factor_url = "https://finance.sina.com.cn/us_stock/company/reinstatement/" + call["provider_symbol"] + "_qfq.js"
            response = SinaHTTP(call, budget, fixture_exchange=fixture_exchange, kind="qfq").get(factor_url)
            factor_entity = {"role": "sina_qfq_factors", "origin": "finance.sina.com.cn", "path": "/us_stock/company/reinstatement/"+call["provider_symbol"]+"_qfq.js",
                "parameters": {}, "factor_for_symbol": call["provider_symbol"], "body": response.body, "observed_received_at": response.received_at,
                "content_decoding": "IDENTITY_ENTITY_NOT_TLS_WIRE", "content_encoding": "identity", "decoded_entity_bytes": len(response.body), **response.decoding,
                "entity_sha256": sha(response.body), "entity_bytes": len(response.body), "parser": TRANSFORM}
            budget.entities.append(factor_entity)
            factors = parse_factors(response.body, call["provider_symbol"])
            if factors[-1]["date"] > proof["full_history_end"]: budget.abort("SINA_FACTOR_PRICE_ANCHOR_MISMATCH")
            rows, adjustment = adjust_rows(rows, factors, factor_entity=factor_entity, price_entity=entity)
            budget.check()
        return {**sdk_ref, "interface": call["interface"], "adaptation": ADAPTATION,
                "assignment_label_template": entity["assignment_label_template"],
                "adapter_source_sha256": sha(Path(__file__).read_bytes().replace(b"\r\n", b"\n")),
                **{k: pin[k] for k in ("function_sha256", "adapted_ast_sha256", "decoder_sha256")}, "wrapper_sha256": pin[wrapper_key],
                "corporate_action_basis": SINA_PRICE_POLICY if long_history else "SINA_UNADJUSTED_ACTIONS_UNAVAILABLE_NOT_TOTAL_RETURN",
                **({"price_policy": SINA_PRICE_POLICY, "quantity_basis": QUANTITY_BASIS, "price_transform": adjustment} if long_history else {}),
                "function_module_sha256": pins["akshare"]["modules"]["akshare/stock/stock_us_sina.py"],
                "decoder_module_sha256": pins["akshare"]["modules"]["akshare/stock/cons.py"],
                "runtime": runtime, **proof}, snapshot, rows
    finally:
        if js is not None:
            js.close()


def normalize_sina(call, snapshot, full_rows, selected, entity, proof):
    if len(snapshot["rows"]) != len(selected):
        raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
    result = []
    for sdk_index, source_index in enumerate(selected):
        original, item = full_rows[source_index], snapshot["rows"][sdk_index]
        label = original["date"]
        if item["values"]["date"]["value"][:10] != label:
            raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
        row = observed_row(call, label, {k: original[k] for k in ("open", "high", "low", "close")},
                           original["volume"], {k: item["values"][k] for k in ("open", "high", "low", "close", "volume")},
                           received_at=entity["observed_received_at"],
                           unit_evidence="CURRENT_PROVIDER_USD_IDENTITY_NOT_HISTORICAL_CURRENCY_CERTIFICATION_SINA_VOLUME_UNIT_UNKNOWN" if call.get("profile") == HISTORY_PROFILE else "US_EQUITY_CATALOG_USD_DECLARATION_NOT_EXTERNALLY_CERTIFIED_SINA_VOLUME_UNIT_UNKNOWN",
                           raw_locator={"entity": 0, "full_history_row": source_index,
                                        "decoded_json_sha256": proof["decoded_json_sha256"]}, sdk_locator={"row": sdk_index})
        row.update(source_adjustment_request={"interface": "stock_us_daily", "adjust": ""},
                   provider_basis_claim="SINA_UNADJUSTED_INTERFACE_NOT_PER_ROW_CERTIFIED",
                   basis_verification="SINA_UNADJUSTED_REQUEST_AND_PINNED_PREFIX_NOT_RAW_OR_PIT_CERTIFIED",
                   provider_identity_verification="EXACT_SINA_URL_AND_ASSIGNMENT_TICKER_PLUS_LOCAL_USD_CATALOG_NOT_EXTERNALLY_CERTIFIED",
                   source_reported_volume=row["volume"], volume=None, volume_unit=None,
                   volume_share_basis="UNKNOWN_SINA_DOCUMENTATION_DOES_NOT_SPECIFY_VOLUME_UNIT")
        row["sdk_transform_flags"].extend(["JS_DECODED_BINARY64_NOT_ORIGINAL_DECIMAL_LEXEME",
                                           "SINA_UNADJUSTED_EARLY_RETURN_ADAPTATION", "FULL_HISTORY_LOCALLY_WINDOWED",
                                           "SINA_VOLUME_UNIT_UNKNOWN_NORMALIZED_VOLUME_NULL"])
        result.append(row)
    return result

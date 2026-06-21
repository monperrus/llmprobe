"""
Reverse-engineer a model's preferred tool names and parameters for:
  - read_file
  - write_file
  - update_file
  - execute_bash
  - ask_user_question

Two distinct dimensions are probed and stored separately in the output JSON:

  behaviour            -- What the *model* can do regardless of provider.
                          Specifically: does it produce schema-conforming JSON
                          when tool schemas are provided?  Tool calling and
                          structured output share the same underlying capability
                          (the model must emit JSON that matches a schema), so
                          the tool-call rounds give a direct signal for both.
                          Stored under the "behaviour" key.

  provider_api_support -- What the *provider/endpoint* exposes at the API level,
                          independently of the model.  The same model served by
                          two different providers can differ here.  Currently
                          probes whether the endpoint accepts
                          response_format: json_schema (Round 0b).
                          Stored under the "provider_api_support" key.

Strategy:
  Round 0  (format)    -- Detect the model's tool-call output format:
                          OpenAI structured tool_calls, [TOOL_CALLS],
                          <tool_call> XML, <toolcall> XML, or inline JSON.
  Round 0b (provider)  -- Test provider-level response_format: json_schema
                          support (API feature, not model behaviour).
  Round 1  (elicit)    -- Ask the model to freely name the function it would
                          call; parse candidate names / parameter keys.
  Round 2  (probe)     -- Build a minimal tool schema from elicited candidates
                          and call the model with tools enabled.  Record actual
                          tool_calls, or detect content-embedded JSON.
  Round 3  (dispatch)  -- Build a tool_dispatch table: model tool_name ->
                          {python_function, param_map}.  Unrecognised tools
                          trigger LLM-synthesised Python implementations.

The final inferred schema is printed as JSON.  behaviour records whether the
model emits structured tool_calls or falls back to inline JSON in content.

All probe traces (saved to probes/<model>/) contain a "ts" timestamp field
recording when the probe round was executed.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from textwrap import indent

import openai

from llmprobe.exceptions import AgentProbeError, AuthenticationError

# -- configuration ------------------------------------------------------------

ENDPOINT = "https://openrouter.ai/api/v1"
MODEL    = "qwen2.5-coder:7b"

_PROBE_DIR: Path | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe a model's preferred tool names/parameters.")
    p.add_argument("--endpoint", default=None, help="OpenAI-compatible base URL")
    p.add_argument("--model",    default=None, help="Model ID")
    p.add_argument("--key-name", default="OPENROUTER_API_KEY", dest="key_name",
                   help="Env-var name / keyring slot holding the API key (default: OPENROUTER_API_KEY).")
    p.add_argument("--output",   default=None, help="Output JSON file (default: inferred_tool_schema_<model>.json)")
    p.add_argument("--quick-summary", action="store_true", dest="quick_summary",
                   help="Read local inferred_tool_schema_*.json files and list models with native "
                        "structured tool_call support along with their main tool parameters.")
    p.add_argument("--quote-test", action="store_true", dest="quote_test",
                   help="Run an extra round that probes whether the model correctly escapes "
                        "double-quotes inside JSON argument values.")
    p.add_argument("--tool", default=None, metavar="OP",
                   help=f"Probe a single canonical op only (one of: {', '.join(ELICIT_TASKS)}). "
                        "Skips writing output JSON and the agent file.")
    p.add_argument("--tool-call-type-only", action="store_true", dest="tool_call_type_only",
                   help="Only output the tool-call type (behaviour.call_delivery_mode) and "
                        "the inferred tool schema (inferred_tool_schema) as JSON on stdout. "
                        "Suppresses all other output (progress text, agent file generation, "
                        "full report). Honours the on-disk probe cache (one week TTL) just "
                        "like a normal run -- pass --force-reprobe to bypass it.")
    p.add_argument("--force-reprobe", action="store_true", dest="force_reprobe",
                   help="Ignore any cached agent_spec JSON (even if fresh) and re-run all "
                        "probe rounds against the live API.")
    return p.parse_args()


# -- result caching (1 week TTL) ----------------------------------------------

CACHE_TTL = datetime.timedelta(days=7)


def _cache_is_fresh(path: Path) -> bool:
    """Return True if *path* exists and was last written within CACHE_TTL."""
    if not path.exists():
        return False
    age = datetime.datetime.now() - datetime.datetime.fromtimestamp(path.stat().st_mtime)
    return age < CACHE_TTL


def _load_cached_output(path: Path) -> dict | None:
    """Return parsed JSON from *path* if it exists, is valid, and is fresh."""
    if not _cache_is_fresh(path):
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_api_key(key_name: str = "OPENROUTER_API_KEY") -> str:
    if key_name == "OPENROUTER_API_KEY":
        from openrouter_key import ensure_api_key
        return ensure_api_key()
    key = os.environ.get(key_name)
    if key:
        return key
    try:
        result = subprocess.run(
            ["keyring", "get", "login2", key_name],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except Exception as e:
        raise AuthenticationError(f"Cannot obtain API key for '{key_name}': {e}") from e


# -- helpers ------------------------------------------------------------------

def make_client(api_key: str) -> openai.OpenAI:
    return openai.OpenAI(api_key=api_key, base_url=ENDPOINT)


def _init_probe_dir(safe_model: str) -> None:
    global _PROBE_DIR
    _PROBE_DIR = Path("probes") / safe_model
    _PROBE_DIR.mkdir(parents=True, exist_ok=True)


def _save_probe(label: str, messages: list[dict],
                resp: openai.types.chat.ChatCompletion,
                tools: list[dict] | None = None) -> None:
    if _PROBE_DIR is None:
        return
    safe_label = re.sub(r"[^a-zA-Z0-9_\-]", "_", label)
    data: dict = {"label": label, "messages": messages, "response": resp.model_dump(),
                  "ts": datetime.datetime.now().isoformat(timespec="seconds")}
    if tools is not None:
        data["tools"] = tools
    with open(_PROBE_DIR / f"{safe_label}.json", "w") as f:
        json.dump(data, f, indent=2)


def chat(client: openai.OpenAI, messages: list[dict], tools: list[dict] | None = None,
         tool_choice="auto") -> openai.types.chat.ChatCompletion:
    kwargs: dict = dict(model=MODEL, messages=messages, temperature=0, timeout=300)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
    try:
        return client.chat.completions.create(**kwargs)
    except openai.APITimeoutError as e:
        raise AgentProbeError(
            f"LLM call timed out after 300 seconds. Model={MODEL}, "
            f"messages={json.dumps(messages, indent=2, default=str)[:500]}..."
        ) from e


def extract_json_block(text: str) -> dict | list | None:
    """Pull the first JSON object or array out of free text."""
    for pattern in (r"```json\s*([\s\S]+?)\s*```", r"```\s*([\s\S]+?)\s*```"):
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    for pattern in (r"\[[\s\S]+\]", r"\{[\s\S]+\}"):
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# -- unified call extraction --------------------------------------------------

class ToolCallResult:
    """Holds whatever the model returned -- structured tool_call or inline JSON."""

    def __init__(self, function_name: str, arguments: dict, structured: bool):
        self.function_name = function_name
        self.arguments = arguments
        self.structured = structured

    def to_dict(self) -> dict:
        return {"function_name": self.function_name, "arguments": self.arguments}

    def __repr__(self):
        mode = "structured tool_call" if self.structured else "inline JSON in content"
        return f"ToolCallResult({self.function_name}, mode={mode}, args={self.arguments})"


def _extract_xml_tool_call(text: str) -> tuple[str, dict] | None:
    """Extract (name, arguments) from <tool_call> or <toolcall> XML in content.

    Falls back to regex extraction when the JSON payload is truncated/malformed.
    """
    m = re.search(r"<tool_?call[^>]*>\s*([\s\S]+?)(?:\s*</tool_?call>|$)", text, re.IGNORECASE)
    if not m:
        return None
    body = m.group(1).strip()

    parsed = extract_json_block(body)
    if isinstance(parsed, dict):
        fn   = parsed.get("name") or parsed.get("function_name")
        args = parsed.get("arguments") or parsed.get("parameters") or {}
        if fn:
            return fn, args if isinstance(args, dict) else {}

    # Optimistic: name is almost always intact even when the rest is truncated.
    name_m = re.search(r'"name"\s*:\s*"([^"]+)"', body)
    if not name_m:
        return None
    fn = name_m.group(1)

    args: dict = {}
    args_m = re.search(r'"arguments"\s*:\s*(\{[\s\S]*)', body)
    if args_m:
        candidate = extract_json_block(args_m.group(1))
        if isinstance(candidate, dict):
            args = candidate
    return fn, args


def extract_call_from_response(resp: openai.types.chat.ChatCompletion) -> ToolCallResult | None:
    """Extract a tool call from the response regardless of delivery mechanism."""
    msg = resp.choices[0].message
    if msg.tool_calls:
        tc = msg.tool_calls[0]
        try:
            arguments = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, ValueError):
            # Some models (e.g. kimi-k2.6) emit truncated/malformed JSON in
            # tool_call arguments.  Fall back to partial extraction so the
            # caller receives a ToolCallResult instead of a crash.
            arguments = extract_json_block(tc.function.arguments) or {}
            if not isinstance(arguments, dict):
                arguments = {}
        return ToolCallResult(
            function_name=tc.function.name,
            arguments=arguments,
            structured=True,
        )
    if msg.content:
        xml_result = _extract_xml_tool_call(msg.content)
        if xml_result:
            fn, args = xml_result
            return ToolCallResult(function_name=fn, arguments=args, structured=False)
        parsed = extract_json_block(msg.content)
        if isinstance(parsed, dict):
            fn = parsed.get("name") or parsed.get("function_name")
            args = parsed.get("arguments") or parsed.get("parameters") or {}
            if fn and isinstance(args, dict):
                return ToolCallResult(function_name=fn, arguments=args, structured=False)
    return None


# -- Round 0: tool-call format detection -------------------------------------

_FORMAT_PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from disk.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "file path"}},
            "required": ["path"],
        },
    },
}]

_FORMAT_PATTERNS: list[tuple[str, str]] = [
    ("TOOL_CALLS_bracket", r"\[TOOL_CALLS\]"),
    ("xml_tool_call",      r"<tool_call\b"),
    ("xml_toolcall",       r"<toolcall\b"),
]


def format_detection_round(client: openai.OpenAI) -> dict:
    """Round 0 -- detect the model's preferred tool-call output format."""
    section("Round 0 -- Tool-call format detection")
    messages = [
        {"role": "system", "content": "You are a helpful assistant with tool access."},
        {"role": "user",   "content": "Read the file /etc/hostname."},
    ]
    resp = chat(client, messages, tools=_FORMAT_PROBE_TOOL)
    _save_probe("round0_format_detection", messages, resp, tools=_FORMAT_PROBE_TOOL)

    msg         = resp.choices[0].message
    raw_content = msg.content or ""

    detected = "unknown"
    if msg.tool_calls:
        detected = "structured_tool_calls"
    else:
        for fmt, pattern in _FORMAT_PATTERNS:
            if re.search(pattern, raw_content, re.IGNORECASE):
                detected = fmt
                break
        else:
            if extract_json_block(raw_content):
                detected = "inline_json"

    result = {
        "detected_format":            detected,
        "has_structured_tool_calls":  bool(msg.tool_calls),
        "raw_content_snippet":        raw_content[:300] or None,
    }
    print(f"  detected_format           : {detected}")
    print(f"  has_structured_tool_calls : {result['has_structured_tool_calls']}")
    if raw_content:
        print(f"  raw content snippet       : {raw_content[:200]!r}")
    return result


# -- Round 0b: provider API capabilities -------------------------------------

def provider_capabilities_round(client: openai.OpenAI) -> dict:
    """Round 0b -- test provider-level API features, independent of model behaviour.

    The same model served by different providers can differ on these features,
    which is why they are stored separately from model_capabilities.

    Currently probes: response_format: json_schema
      - This is a provider/endpoint feature (not a model feature).
      - Tool-call probing already tests whether the *model* can emit
        schema-conforming JSON; this tests whether the *provider API*
        exposes the response_format path for doing so without tool calls.
    """
    section("Round 0b -- Provider API capabilities")
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user",   "content": "Reply with the word 'hello'."},
    ]
    entry: dict = {"supported": False, "schema_conformant": None, "error": None}
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0,
            timeout=30,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": schema},
            },
        )
        _save_probe("round0b_provider_json_schema", messages, resp)
        content = resp.choices[0].message.content or ""
        try:
            parsed = json.loads(content)
            conformant = isinstance(parsed, dict) and "value" in parsed
        except json.JSONDecodeError:
            parsed = None
            conformant = False
        # Store head + tail so thinking-token prefixes don't hide the actual JSON.
        snippet = content[:100] + ("…" + content[-100:] if len(content) > 200 else content[100:])
        entry = {"supported": True, "schema_conformant": conformant,
                 "parsed_value": parsed if conformant else None,
                 "raw_content": snippet, "error": None}
        print("  response_format=json_schema : supported")
        print(f"  schema_conformant           : {conformant}")
        if content:
            print(f"  raw content                 : {content[:100]!r}")
    except Exception as e:
        entry["error"] = str(e)[:300]
        print("  response_format=json_schema : NOT supported")
        print(f"  error                       : {str(e)[:200]}")

    return {"response_format_json_schema": entry}


# -- Round 1: elicit free-form descriptions -----------------------------------

ELICIT_TASKS = {
    "read_file": (
        "You need to read the contents of the file /etc/hostname. "
        "What function would you call and what arguments would you pass? "
        "Respond with a JSON object like: "
        '{"function_name": "...", "arguments": {"arg1": "val1", ...}}'
    ),
    "write_file": (
        "You need to write the text 'hello world' to the file /tmp/test.txt. "
        "What function would you call and what arguments would you pass? "
        "Respond with a JSON object like: "
        '{"function_name": "...", "arguments": {"arg1": "val1", ...}}'
    ),
    "update_file": (
        "The file /tmp/test.py already exists and contains Python code. "
        "You need to make a targeted edit: replace the exact string 'x = 1' with 'x = 42', "
        "without rewriting the whole file. "
        "What function would you call and what arguments would you pass? "
        "Respond with a JSON object like: "
        '{"function_name": "...", "arguments": {"arg1": "val1", ...}}'
    ),
    "execute_bash": (
        "You need to run the shell command `ls -la /tmp`. "
        "What function would you call and what arguments would you pass? "
        "Respond with a JSON object like: "
        '{"function_name": "...", "arguments": {"arg1": "val1", ...}}'
    ),
    "ask_user_question": (
        "You need to ask the user a clarifying question: "
        "'Should I overwrite the existing file, or create a backup first?' "
        "with options 'Overwrite' and 'Backup'. "
        "What function would you call and what arguments would you pass? "
        "Respond with a JSON object like: "
        '{"function_name": "...", "arguments": {"arg1": "val1", ...}}'
    ),
    "list_directory": (
        "You need to list all files and subdirectories inside /tmp. "
        "What function would you call and what arguments would you pass? "
        "Respond with a JSON object like: "
        '{"function_name": "...", "arguments": {"arg1": "val1", ...}}'
    ),
    "search_files": (
        "You need to find every line containing the string 'def main' "
        "in any file under /tmp/myproject (search recursively). "
        "What function would you call and what arguments would you pass? "
        "Respond with a JSON object like: "
        '{"function_name": "...", "arguments": {"arg1": "val1", ...}}'
    ),
    "glob": (
        "You need to find all Python source files (matching *.py) "
        "anywhere under /tmp/myproject, recursively. "
        "What function would you call and what arguments would you pass? "
        "Respond with a JSON object like: "
        '{"function_name": "...", "arguments": {"arg1": "val1", ...}}'
    ),
}

FALLBACK_ELICITED = {
    "read_file":         {"function_name": "read_file",          "arguments": {"file_path": ""}},
    "write_file":        {"function_name": "write_file",         "arguments": {"file_path": "", "content": ""}},
    "update_file":       {"function_name": "str_replace",        "arguments": {"file_path": "", "old_str": "", "new_str": ""}},
    "execute_bash":      {"function_name": "run_shell_command",  "arguments": {"command": ""}},
    "ask_user_question": {"function_name": "ask_user_question",  "arguments": {"question": ""}},
    "list_directory":    {"function_name": "list_directory",     "arguments": {"path": ""}},
    "search_files":      {"function_name": "search_files",       "arguments": {"path": "", "pattern": ""}},
    "glob":              {"function_name": "glob",               "arguments": {"pattern": ""}},
}


def elicit_round(client: openai.OpenAI, tasks: dict | None = None) -> dict[str, dict]:
    section("Round 1 -- Free-form elicitation (no tool schema)")
    results: dict[str, dict] = {}
    for op, task in (tasks if tasks is not None else ELICIT_TASKS).items():
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant with access to tools. "
                    "When asked what function to call, respond ONLY with a JSON object."
                ),
            },
            {"role": "user", "content": task},
        ]
        resp = chat(client, messages)
        _save_probe(f"round1_elicit_{op}", messages, resp)
        text = resp.choices[0].message.content or ""
        parsed = extract_json_block(text)
        print(f"\n[{op}] raw response:\n{indent(text.strip(), '  ')}")
        if isinstance(parsed, dict) and ("function_name" in parsed or "name" in parsed):
            fn = parsed.get("function_name") or parsed.get("name")
            args = parsed.get("arguments", {})
            results[op] = {"function_name": fn, "arguments": args}
            print(f"[{op}] parsed: {json.dumps(results[op], indent=2)}")
        else:
            results[op] = FALLBACK_ELICITED[op]
            print(f"[{op}] could not parse -- using fallback: {results[op]}")
    return results


# -- Round 2: probe with tool schema ------------------------------------------

def args_to_schema_properties(args: dict) -> dict:
    props: dict[str, dict] = {}
    for key, val in args.items():
        if isinstance(val, bool):
            typ = "boolean"
        elif isinstance(val, int):
            typ = "integer"
        elif isinstance(val, float):
            typ = "number"
        elif isinstance(val, list):
            typ = "array"
        else:
            typ = "string"
        props[key] = {"type": typ, "description": key.replace("_", " ")}
    return props


def _deduplicate_elicited(elicited: dict[str, dict]) -> dict[str, dict]:
    """Noop any canonical op whose elicited function_name duplicates an earlier op's name.

    When a model maps two distinct canonical ops (e.g. write_file and update_file)
    to the same tool name, the second one cannot be meaningfully distinguished at
    runtime.  Mark it with function_name=None so it is excluded from the schema
    and dispatch table.
    """
    seen: dict[str, str] = {}
    result: dict[str, dict] = {}
    for op, info in elicited.items():
        fn = info.get("function_name")
        if fn and fn in seen:
            print(f"  [dedup] '{op}' elicited name '{fn}' already claimed by "
                  f"'{seen[fn]}' -- marking as noop")
            result[op] = {**info, "function_name": None}
        else:
            if fn:
                seen[fn] = op
            result[op] = info
    return result


def _sanitize_tool_name(name: str) -> str:
    """Ensure a tool name matches the OpenAI pattern ^[a-zA-Z0-9_-]{1,64}$."""
    sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
    return sanitized[:64] or "tool"


def build_tool_schema(elicited: dict[str, dict]) -> list[dict]:
    tools = []
    for op, info in elicited.items():
        fn_name = info.get("function_name")
        if not fn_name:   # None => nooped due to name collision
            continue
        fn_name = _sanitize_tool_name(fn_name)
        args    = info.get("arguments", {})

        canon = _CANONICAL_OPS.get(op, {})
        tool_description   = canon.get("description") or f"Perform the '{op}' operation."
        param_descriptions = canon.get("param_descriptions", {})
        role_to_kwarg      = {role: kwarg for kwarg, role in canon.get("kwarg_roles", {}).items()}

        props: dict[str, dict] = {}
        for key, val in args.items():
            if isinstance(val, bool):
                typ = "boolean"
            elif isinstance(val, int):
                typ = "integer"
            elif isinstance(val, float):
                typ = "number"
            elif isinstance(val, list):
                typ = "array"
            else:
                typ = "string"
            role  = _classify_param(key)
            kwarg = role_to_kwarg.get(role) if role else None
            desc  = param_descriptions.get(kwarg) if kwarg else None
            props[key] = {"type": typ, "description": desc or key.replace("_", " ")}

        tools.append({
            "type": "function",
            "function": {
                "name": fn_name,
                "description": tool_description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": list(props.keys()),
                },
            },
        })
    return tools


PROBE_TASKS = {
    "read_file":         "Please read the file /etc/hostname and tell me its contents.",
    "write_file":        "Please write 'hello world\\n' to the file /tmp/test.txt.",
    "update_file":       "In the file /tmp/test.py, replace the exact string 'x = 1' with 'x = 42'. Do not rewrite the whole file.",
    "execute_bash":      "Please run `ls -la /tmp` and show me the output.",
    "ask_user_question": "Before you start, ask the user whether they want to overwrite /tmp/test.txt or create a backup first.",
    "list_directory":    "List the files and directories inside /tmp.",
    "search_files":      "Search for the string 'hello' in all files under /tmp.",
    "glob":              "Find all .py files anywhere under /tmp.",
}


def probe_round(client: openai.OpenAI, tools: list[dict],
                label: str = "Round 2",
                tasks: dict | None = None) -> dict[str, ToolCallResult | None]:
    section(f"{label} -- Probing with tool schema")
    print("\nSchema offered to model:")
    print(json.dumps(tools, indent=2))

    calls: dict[str, ToolCallResult | None] = {}
    for op, task in (tasks if tasks is not None else PROBE_TASKS).items():
        messages = [
            {"role": "system", "content": "You are a helpful assistant with tool access."},
            {"role": "user",   "content": task},
        ]
        resp = chat(client, messages, tools=tools)
        _save_probe(f"{re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')}_{op}",
                    messages, resp, tools=tools)
        result = extract_call_from_response(resp)
        calls[op] = result
        if result:
            mode = "structured tool_call" if result.structured else "inline JSON in content"
            print(f"\n[{op}] ({mode}): {json.dumps(result.to_dict(), indent=2)}")
        else:
            raw = resp.choices[0].message.content
            print(f"\n[{op}] no call detected. content: {raw!r}")
    return calls


# -- behavioural summary ------------------------------------------------------

def behavioural_summary(probe_calls: dict[str, ToolCallResult | None]) -> dict:
    """Summarise the model's tool-call delivery behaviour ("behaviour" key in output).

    This captures what the *model* does when given tool schemas — it is
    independent of provider API support (see provider_api_support / Round 0b).
    Tool calling and structured output share the same underlying model
    capability: producing schema-conforming JSON.  The call_delivery_mode
    field records which delivery mechanism the model uses.
    """
    structured = sum(1 for r in probe_calls.values() if r and r.structured)
    inline     = sum(1 for r in probe_calls.values() if r and not r.structured)
    missing    = sum(1 for r in probe_calls.values() if r is None)
    if structured == len(probe_calls):
        mode = "structured_tool_calls"
    elif inline > 0 and structured == 0:
        mode = "inline_json_in_content"
    else:
        mode = "mixed"
    return {
        "call_delivery_mode": mode,
        "structured_tool_calls": structured,
        "inline_json_in_content": inline,
        "no_call_detected": missing,
        "note": (
            "This model outputs tool invocations as JSON inside the message "
            "content field rather than the structured tool_calls API field. "
            "Callers must parse the content field to extract function calls."
            if mode == "inline_json_in_content" else
            "Model correctly uses the structured tool_calls API field."
        ),
    }


# -- tool dispatch table -------------------------------------------------------
#
# _CANONICAL_OPS maps each canonical operation name to:
#   python_function : name of the callable in agent_probe.TOOL_LIBRARY
#   kwarg_roles     : {kwarg_name: semantic_role}
#
# _PARAM_ROLES is the ordered list of (role, hint_substrings) used to classify
# a model's parameter name into a semantic role.  More-specific patterns first.

_CANONICAL_OPS: dict[str, dict] = {
    "read_file": {
        "python_function": "t_read",
        "kwarg_roles": {"path": "path"},
        "description": "Read the contents of a file from disk.",
        "param_descriptions": {
            "path": "Absolute or relative path to the file to read.",
        },
    },
    "write_file": {
        "python_function": "t_write",
        "kwarg_roles": {"path": "path", "content": "content"},
        "description": "Write text to a file, creating or overwriting it.",
        "param_descriptions": {
            "path": "Absolute or relative path to the file to write.",
            "content": "Text content to write to the file.",
        },
    },
    "update_file": {
        "python_function": "t_update",
        "kwarg_roles": {"path": "path", "old": "old", "new": "new", "patch": "patch"},
        "description": "Make a targeted edit to a file by replacing an exact string.",
        "param_descriptions": {
            "path": "Absolute or relative path to the file to edit.",
            "old": "Exact string to find and replace.",
            "new": "Replacement string.",
            "patch": "Unified patch string in apply_patch format.",
        },
    },
    "execute_bash": {
        "python_function": "t_run",
        "kwarg_roles": {"command": "command"},
        "description": "Execute a shell command and return its output.",
        "param_descriptions": {
            "command": "Shell command to execute.",
        },
    },
    "ask_user_question": {
        "python_function": "t_ask_user",
        "kwarg_roles": {"question": "question"},
        "description": "Ask the user a clarifying question and wait for their answer.",
        "param_descriptions": {
            "question": "The question to present to the user.",
        },
    },
    "list_directory": {
        "python_function": "t_list_dir",
        "kwarg_roles": {"path": "path"},
        "description": "List files and subdirectories inside a directory.",
        "param_descriptions": {
            "path": "Path to the directory to list.",
        },
    },
    "search_files": {
        "python_function": "t_search",
        "kwarg_roles": {"path": "path", "pattern": "query"},
        "description": "Search for lines matching a pattern across files in a directory tree.",
        "param_descriptions": {
            "path": "Root directory to search recursively.",
            "pattern": "Search string, keyword, or regex pattern to look for.",
        },
    },
    "glob": {
        "python_function": "t_glob",
        "kwarg_roles": {"pattern": "glob_pattern"},
        "description": "Find files matching a glob pattern (e.g. **/*.py) recursively.",
        "param_descriptions": {
            "pattern": "Glob pattern to match files against (e.g. **/*.py).",
        },
    },
}

_PARAM_ROLES: list[tuple[str, tuple]] = [
    ("old",          ("old_str", "old_string", "old_text", "search", "find", "before", "original")),
    ("new",          ("new_str", "new_string", "new_text", "replac", "after", "replacement")),
    ("patch",        ("patch", "diff", "unified_diff")),
    ("question",     ("question", "questions", "prompt")),
    ("query",        ("query", "grep", "regex", "keyword", "term", "search_string", "search_term", "pattern")),
    ("glob_pattern", ("glob", "wildcard")),
    ("content",      ("content", "text", "data", "body")),
    ("command",      ("command", "cmd", "shell_command", "bash")),
    ("path",         ("path", "file", "filename", "file_path", "filepath", "directory", "dir")),
]


def _classify_param(name: str) -> str | None:
    """Return the semantic role for a parameter name, or None if unrecognised."""
    n = name.lower()
    for role, hints in _PARAM_ROLES:
        if any(h in n for h in hints):
            return role
    return None


def _match_op(tool_name: str, param_names: list[str],
               elicited_names: dict[str, str]) -> str | None:
    """Return the canonical op name for a tool, or None if unrecognised.

    Priority:
      1. elicited_names reverse lookup (probe already told us the op).
      2. Parameter-role fingerprint (set of roles present in the tool).
    """
    # 1. Direct lookup: elicited_names maps tool_name -> canonical op.
    if tool_name in elicited_names:
        return elicited_names[tool_name]

    # 1b. If the tool name is itself a canonical op name, use it directly.
    if tool_name in _CANONICAL_OPS:
        return tool_name

    # 2. Role fingerprint -- more-specific patterns first.
    roles = {_classify_param(p) for p in param_names} - {None}
    if "old" in roles and "new" in roles:
        return "update_file"
    if "question" in roles:
        return "ask_user_question"
    if "query" in roles:
        return "search_files"
    if "glob_pattern" in roles:
        return "glob"
    if "content" in roles and "path" in roles:
        return "write_file"
    if "command" in roles:
        return "execute_bash"
    if "path" in roles:
        return "read_file"   # list_directory is indistinguishable here; elicited_names wins
    return None


def _build_param_map(param_names: list[str], kwarg_roles: dict[str, str]) -> dict[str, str]:
    """Map each model param name -> Python kwarg name.

    kwarg_roles: {kwarg_name: role}  (from _CANONICAL_OPS)
    Returns:     {model_param_name: kwarg_name}

    Any param whose role is not in kwarg_roles is passed through unchanged
    (identity mapping), so the function still receives it even if we don't
    know what to do with it.
    """
    role_to_kwarg = {role: kwarg for kwarg, role in kwarg_roles.items()}
    param_map: dict[str, str] = {}
    for p in param_names:
        role = _classify_param(p)
        kwarg = role_to_kwarg.get(role) if role else None
        if kwarg:
            # Only include params that map to a known kwarg for this op;
            # extras (e.g. the 'command' enum on str_replace_editor) are dropped.
            param_map[p] = kwarg
    return param_map


def _append_to_tool_library(fn_name: str, source: str,
                             tool_library_path: Path | None = None) -> None:
    """Append a generated function to tool_library.py and register it in TOOL_LIBRARY."""
    if tool_library_path is None:
        return
    addition = f"\n\n# --- generated: {fn_name} ---\n{source}\n\nTOOL_LIBRARY[{fn_name!r}] = {fn_name}\n"
    with tool_library_path.open("a") as f:
        f.write(addition)
    print(f"  [codegen] appended {fn_name} to {tool_library_path.name}")


def _synthesise_function(
    client: openai.OpenAI,
    tool_name: str,
    tool_description: str,
    param_names: list[str],
) -> tuple[str, str]:
    """Ask the LLM to write a Python implementation for an unrecognised tool.

    Returns (python_function_name, source_code).

    Contract for the generated function:
      - Named  t_<sanitised_tool_name>
      - Accepts the model's exact parameter names as keyword arguments (str defaults)
      - Returns tuple[str, dict]: (human-readable result, log dict with 'result' key)
      - Uses only stdlib (pathlib, subprocess, json, os are pre-imported in scope)
      - Handles exceptions; returns "ERROR: ..." on failure
    """
    fn_name = "t_" + re.sub(r"[^a-z0-9_]", "_", tool_name.lower()).strip("_")
    params_sig = ", ".join(f"{p}: str = ''" for p in param_names)

    lines = [
        f"Write a Python function called `{fn_name}` that implements the tool described below.",
        "",
        f"Tool name       : {tool_name}",
        f"Tool description: {tool_description}",
        f"Parameters      : {param_names}",
        "",
        "Requirements:",
        f"  - Function signature: def {fn_name}({params_sig}) -> tuple[str, dict]:",
        "  - Return a tuple: (human-readable result string, dict with at least a 'result' key)",
        "  - Use only the Python standard library (pathlib, subprocess, json, os, etc.)",
        "  - Handle exceptions and return an 'ERROR: ...' string on failure",
        "  - Do NOT include import statements -- assume Path, subprocess, json, os are in scope",
        "",
        "Respond with ONLY the function source code, no prose, no markdown fences.",
    ]
    prompt = "\n".join(lines)

    section(f"Code generation -- synthesising {fn_name} for unrecognised tool '{tool_name}'")
    messages = [
        {"role": "system", "content": "You are an expert Python programmer. "
                                       "Respond with only raw Python source code."},
        {"role": "user", "content": prompt},
    ]
    resp = chat(client, messages)
    _save_probe(f"codegen_{fn_name}", messages, resp)
    raw = resp.choices[0].message.content or ""

    # Strip markdown fences if the model added them despite instructions.
    source = re.sub(r"^```(?:python)?\s*", "", raw.strip(), flags=re.MULTILINE)
    source = re.sub(r"\s*```$", "", source.strip(), flags=re.MULTILINE)
    source = source.strip()

    print(f"\nGenerated source for {fn_name}:\n{indent(source, '  ')}")
    return fn_name, source


def build_tool_dispatch(
    elicited: dict[str, dict],
    final_probes: dict[str, "ToolCallResult | None"],
    client: openai.OpenAI,
    tool_library_path: Path | None = None,
) -> dict[str, dict]:
    """Build the tool_dispatch table stored in the probe JSON.

    For each tool observed in final_probes:
      - Match it to a canonical op (via elicited_names or param-role fingerprint).
      - Build a param_map: model param name -> Python kwarg name.
      - If no canonical op matches, ask the LLM to synthesise a Python function
        and store its source in generated_source.

    Returned structure (keyed by model tool name):

      {
        "str_replace_editor": {
          "python_function": "t_update",
          "param_map": {"path": "path", "old_str": "old", "new_str": "new"}
        },
        "some_unknown_tool": {
          "python_function": "t_some_unknown_tool",
          "param_map": {"x": "x"},
          "generated_source": "def t_some_unknown_tool(x: str = '') -> tuple[str, dict]: ..."
        }
      }
    """
    section("Round 4 -- Building tool dispatch table")

    # Reverse map: tool_name -> canonical op name (from elicited_names).
    elicited_names: dict[str, str] = {
        v["function_name"]: op
        for op, v in elicited.items()
        if v.get("function_name")
    }

    dispatch: dict[str, dict] = {}

    for op, result in final_probes.items():
        if result is None:
            print(f"  [{op}] no probe result -- skipping")
            continue

        tool_name   = result.function_name
        param_names = list(result.arguments.keys())

        if tool_name in dispatch:
            # Two canonical ops share the same tool name -- already handled.
            continue

        canonical_op = _match_op(tool_name, param_names, elicited_names)

        if canonical_op and canonical_op in _CANONICAL_OPS:
            canon     = _CANONICAL_OPS[canonical_op]
            param_map = _build_param_map(param_names, canon["kwarg_roles"])
            dispatch[tool_name] = {
                "python_function": canon["python_function"],
                "param_map":       param_map,
            }
            print(f"  [{tool_name}] -> {canon['python_function']}  param_map={param_map}")
        else:
            # Unrecognised tool -- synthesise a Python implementation.
            print(f"  [{tool_name}] unrecognised -- requesting code generation")
            fn_name, source = _synthesise_function(
                client,
                tool_name=tool_name,
                tool_description=f"Tool '{tool_name}' with parameters {param_names}",
                param_names=param_names,
            )
            _append_to_tool_library(fn_name, source, tool_library_path)
            dispatch[tool_name] = {
                "python_function": fn_name,
                "param_map":       {p: p for p in param_names},  # identity
            }
            print(f"  [{tool_name}] -> {fn_name} (generated)")

    # Fallback: register any elicited tool names the probe missed.
    # This happens when the model answers a task with a multi-step call chain
    # (e.g. reads the file before updating it) and the probe only captures the
    # first call, leaving the elicited tool name absent from dispatch.
    for op, info in elicited.items():
        fn = info.get("function_name")
        if not fn or fn in dispatch:
            continue
        canon = _CANONICAL_OPS.get(op, {})
        if not canon:
            continue
        param_names = list(info.get("arguments", {}).keys())
        param_map   = _build_param_map(param_names, canon["kwarg_roles"])
        dispatch[fn] = {
            "python_function": canon["python_function"],
            "param_map":       param_map,
        }
        print(f"  [{fn}] -> {canon['python_function']}  (from elicit fallback, probe missed it)")

    return dispatch


# -- quick summary from local JSON files --------------------------------------

def _tool_param_signature(tool: dict) -> str:
    """Return 'name(p1*, p2)' where '*' marks required params."""
    fn = tool.get("function") or tool
    name = fn.get("name", "?")
    params = fn.get("parameters") or {}
    props = params.get("properties")
    if not isinstance(props, dict):
        props = {k: v for k, v in params.items()
                 if isinstance(v, dict) and "type" in v}
    required = set(params.get("required") or [])
    parts = []
    for pname, pinfo in props.items():
        marker = "*" if pname in required or (isinstance(pinfo, dict) and pinfo.get("required")) else ""
        ptype = pinfo.get("type", "?") if isinstance(pinfo, dict) else "?"
        parts.append(f"{pname}{marker}:{ptype}")
    return f"{name}({', '.join(parts)})"


def quick_summary() -> None:
    import glob
    paths = sorted(glob.glob("agent_spec_*.json"))
    if not paths:
        print("No agent_spec_*.json files found in the current directory.")
        return

    structured_list: list[dict] = []
    other: list[tuple[str, str]] = []
    for path in paths:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            other.append((path, f"unreadable: {e}"))
            continue
        model = data.get("model", path)
        behaviour = data.get("behaviour") or {}
        mode = behaviour.get("call_delivery_mode")
        status = data.get("status", "ok")
        if mode == "structured_tool_calls":
            structured_list.append(data)
        else:
            other.append((model, mode or status or "unknown"))

    print(f"Models with native structured tool_calls support  ({len(structured_list)}/{len(paths)}):\n")
    for data in structured_list:
        model    = data.get("model", "?")
        endpoint = data.get("endpoint", "?")
        print(f"  * {model}   [{endpoint}]")
        for tool in data.get("inferred_tool_schema") or []:
            print(f"      - {_tool_param_signature(tool)}")
        print()

    if other:
        print(f"Models without native structured tool_calls  ({len(other)}):")
        for model, mode in other:
            print(f"  * {model}  ->  {mode}")

    print("\n('*' marks required parameters)")


# -- quote-escaping test ------------------------------------------------------

QUOTE_TEST_TASKS = {
    "write_file": (
        r'Write the following text exactly to /tmp/quote_test.txt: '
        r'She said "hello" and he replied "goodbye, world".'
    ),
    "execute_bash": (
        r'Run this exact shell command: echo "hello \"world\""'
    ),
    "update_file": (
        r'In /tmp/test.py, replace the string x = "old value" with x = "new value". '
        r'Do not rewrite the whole file.'
    ),
}

QUOTE_TEST_EXPECTED = {
    "write_file":   '"',
    "execute_bash": '"',
    "update_file":  '"',
}


def quote_test_round(client: openai.OpenAI, tools: list[dict]) -> dict:
    section("Quote-escaping test -- arguments must contain literal double-quotes")
    print("\nEach task requires a double-quote character inside a JSON string value.")
    print("PASS = model emits valid JSON with the quote present in the parsed value.")
    print("FAIL = JSON parse error, or the quote is silently dropped/mangled.\n")

    results: dict[str, dict] = {}
    for op, task in QUOTE_TEST_TASKS.items():
        messages = [
            {"role": "system", "content": "You are a helpful assistant with tool access."},
            {"role": "user",   "content": task},
        ]
        resp = chat(client, messages, tools=tools)
        _save_probe(f"quote_test_{op}", messages, resp, tools=tools)
        entry: dict = {"task": task, "pass": False, "error": None,
                       "structured": None, "parsed_args": None}
        result = extract_call_from_response(resp)
        if result is None:
            raw = resp.choices[0].message.content
            entry["error"] = "no tool call detected"
            entry["raw_content"] = raw
            print(f"[{op}] FAIL -- no call detected. content: {raw!r}")
            results[op] = entry
            continue
        entry["structured"]    = result.structured
        entry["function_name"] = result.function_name
        entry["parsed_args"]   = result.arguments
        expected_char = QUOTE_TEST_EXPECTED[op]
        found = any(isinstance(v, str) and expected_char in v
                    for v in result.arguments.values())
        if found:
            entry["pass"] = True
            mode = "structured" if result.structured else "inline JSON"
            print(f"[{op}] PASS  ({mode})  args={json.dumps(result.arguments)}")
        else:
            entry["error"] = "double-quote not found in any argument value"
            print(f"[{op}] FAIL -- quote missing from args: {json.dumps(result.arguments)}")
        results[op] = entry

    passed = sum(1 for r in results.values() if r["pass"])
    total  = len(results)
    print(f"\nQuote-test summary: {passed}/{total} passed")
    return {"quote_test_results": results, "quote_test_passed": passed, "quote_test_total": total}


# -- agent file generation ----------------------------------------------------

_AGENT_TEMPLATE = '''\
#!/usr/bin/env python3
"""Auto-generated wrapper -- runs agent_probe with model {model}.

Usage:
    agent-{model} "<task>"           # one-shot
    agent-{model}                    # interactive REPL
    agent-{model} --non-interactive  # disable ask_user_question tool
"""

import os, sys, importlib.util

project_root = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, project_root)
script_path = os.path.join(project_root, "agent_probe.py")

spec = importlib.util.spec_from_file_location("agent_probe", script_path)
agent_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_probe)

_k = os.environ.get({key_name_repr}, "")
if _k:
    os.environ["OPENROUTER_API_KEY"] = _k

sys.argv.insert(1, {model_repr})
sys.argv.insert(2, "--endpoint")
sys.argv.insert(3, {endpoint_repr})
agent_probe.main()
'''


def create_agent_file(model: str, safe_model: str, endpoint: str = "", key_name: str = "OPENROUTER_API_KEY") -> Path:
    """Write agent-<safe_model>.py in the current working directory and symlink it into ~/bin."""
    here       = Path.cwd()
    agent_path = here / f"agent-{safe_model}.py"
    agent_path.write_text(_AGENT_TEMPLATE.format(
        model=model,
        model_repr=repr(model),
        endpoint_repr=repr(endpoint or ENDPOINT),
        key_name_repr=repr(key_name),
    ))
    agent_path.chmod(0o755)
    print(f"\nAgent file written: {agent_path}")
    bin_dir   = Path.home() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link_path = bin_dir / f"agent-{safe_model}"
    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(agent_path)
    print(f"Symlink created:    {link_path} -> {agent_path}")
    return agent_path


# -- main ---------------------------------------------------------------------

def _run_probe_silently(quiet: bool):
    """Context manager that redirects stdout to /dev/null while *quiet* is True."""
    import contextlib
    if not quiet:
        return contextlib.nullcontext()
    return contextlib.redirect_stdout(open(os.devnull, "w"))


def main():
    global ENDPOINT, MODEL

    args = parse_args()
    if args.quick_summary:
        quick_summary()
        return
    if args.endpoint:
        ENDPOINT = args.endpoint
    if args.model:
        MODEL = args.model

    safe_model      = MODEL.replace("/", "_").replace(":", "_")
    safe_model_name = MODEL.split("/", 1)[-1].replace(":", "_")  # drop provider prefix for agent filename
    out_path        = args.output or f"agent_spec_{safe_model}.json"

    quiet = args.tool_call_type_only

    # --tool-call-type-only: serve from the on-disk cache (1 week TTL) when
    # possible, run the probe silently otherwise, then print only the
    # tool-call type + inferred tool schema as JSON.
    if quiet:
        cache_path = Path(out_path)
        cached = None if args.force_reprobe else _load_cached_output(cache_path)
        if cached is not None:
            behaviour = cached.get("behaviour") or {}
            print(json.dumps({
                "call_delivery_mode":  behaviour.get("call_delivery_mode"),
                "inferred_tool_schema": cached.get("inferred_tool_schema") or [],
            }, indent=2))
            return
        with _run_probe_silently(True):
            _init_probe_dir(safe_model)
            result = _probe_and_build_output(args, safe_model, safe_model_name, out_path)
        behaviour = result.get("behaviour") or {}
        print(json.dumps({
            "call_delivery_mode":  behaviour.get("call_delivery_mode"),
            "inferred_tool_schema": result.get("inferred_tool_schema") or [],
        }, indent=2))
        return

    _init_probe_dir(safe_model)
    _probe_and_build_output(args, safe_model, safe_model_name, out_path)


def _probe_and_build_output(args, safe_model: str, safe_model_name: str, out_path: str) -> dict:
    single_op: str | None = args.tool
    if single_op:
        if single_op not in ELICIT_TASKS:
            sys.exit(f"Unknown op '{single_op}'. Choose from: {', '.join(ELICIT_TASKS)}")
        elicit_tasks_filter = {single_op: ELICIT_TASKS[single_op]}
        probe_tasks_filter  = {single_op: PROBE_TASKS[single_op]}
    else:
        elicit_tasks_filter = None
        probe_tasks_filter  = None

    output: dict = {
        "model":                MODEL,
        "endpoint":             ENDPOINT,
        "status":               "incomplete",
        "error":                None,
        # provider_api_support: features the *endpoint* exposes (e.g. response_format),
        # independent of which model is running behind it.
        "provider_api_support": {},
        # format_detection + behaviour: what the *model* does when given tool schemas —
        # its ability to emit schema-conforming JSON.  Tool calling and structured output
        # share this capability; provider_api_support captures the API-level distinction.
        "format_detection":     {},
        "elicited_names":       {},
        "inferred_tool_schema": [],
        "behaviour":            {},
        "tool_dispatch":        {},
        "quote_test":           None,
    }

    def save(note: str = ""):
        if single_op:
            return
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)
        msg = f"\nReport written to {out_path}"
        if note:
            msg += f"  ({note})"
        print(msg)

    api_key = get_api_key(args.key_name)
    client  = make_client(api_key)

    print(f"Target: {ENDPOINT}")
    print(f"Model:  {MODEL}")

    try:
        output["provider_api_support"] = provider_capabilities_round(client)
    except Exception as e:
        output["provider_api_support"] = {"error": str(e)}
        print(f"\nWARNING in Round 0b: {e}")

    try:
        output["format_detection"] = format_detection_round(client)
    except Exception as e:
        if "No endpoints found that support tool use" in str(e):
            print("\nModel does not support tool use -- aborting.")
            raise SystemExit(1)
        output["format_detection"] = {"error": str(e)}
        print(f"\nWARNING in Round 0: {e}")

    try:
        elicited = elicit_round(client, tasks=elicit_tasks_filter)
        elicited = _deduplicate_elicited(elicited)
        output["elicited_names"] = {op: v["function_name"] for op, v in elicited.items()}
    except Exception as e:
        output["error"] = f"elicit_round failed: {e}"
        print(f"\nERROR in Round 1: {e}")
        save("failed at Round 1")
        raise SystemExit(1)

    initial_tools = build_tool_schema(elicited)

    try:
        probe_calls = probe_round(client, initial_tools, tasks=probe_tasks_filter)
    except Exception as e:
        output["error"] = f"probe_round failed: {e}"
        print(f"\nERROR in Round 2: {e}")
        save("failed at Round 2 -- elicited names preserved")
        raise SystemExit(1)

    final_tools  = initial_tools
    final_probes = probe_calls
    behaviour    = behavioural_summary(final_probes)

    output["status"]               = "ok"
    output["inferred_tool_schema"] = final_tools
    output["behaviour"]            = behaviour

    section("Final inferred tool schema")
    print(json.dumps(final_tools, indent=2))

    section("Behavioural findings")
    print(json.dumps(behaviour, indent=2))

    try:
        tool_dispatch = build_tool_dispatch(elicited, final_probes, client)
        output["tool_dispatch"] = tool_dispatch
        section("Tool dispatch table")
        print(json.dumps(tool_dispatch, indent=2))
    except Exception as e:
        output["tool_dispatch"] = {"error": str(e)}
        print(f"\nERROR building tool dispatch: {e}")

    if args.quote_test:
        try:
            qt = quote_test_round(client, final_tools)
            output["quote_test"] = qt
        except Exception as e:
            output["quote_test"] = {"error": str(e)}
            print(f"\nERROR in quote-test round: {e}")

    if not single_op:
        save()
        if not args.tool_call_type_only:
            create_agent_file(MODEL, safe_model_name, endpoint=ENDPOINT, key_name=args.key_name)

    return output


if __name__ == "__main__":
    main()

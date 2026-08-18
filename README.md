# llmprobe

Reverse-engineer an LLM endpoint's tool-calling behaviour and API-surface
capabilities by actually calling it, instead of trusting vendor docs.

Point `probe_inference.py` at any OpenAI-compatible `chat/completions`
endpoint and it works out:

- What tool/function names and parameter names the model *itself* prefers
  for common coding-agent operations (read a file, write a file, edit a
  file, run a shell command, ask the user a question) — by eliciting them
  in free text first, then testing the model against its own answers.
- Whether it emits structured `tool_calls`, or falls back to inline JSON /
  XML tags in the message content.
- A fixed set of finer-grained capabilities: quote escaping, tool-call
  efficiency, when it asks clarifying questions, whether it knows and can
  use OpenAI's `apply_patch` grammar, structured-output support, real SSE
  streaming, and reasoning-token exposure. See [CAPABILITIES.md](CAPABILITIES.md)
  for the full reference.

Results are written as JSON + Markdown per model under `reports/<model>/`,
so different models and endpoints can be compared side by side.

## Install

```bash
pip install openai lark keyring
```

API keys are read from an environment variable or, if unset, from the
system keyring (`keyring get login2 <key-name>`).

## Usage

```bash
python3 probe_inference.py \
  --endpoint https://api.deepseek.com \
  --model deepseek-v4-flash \
  --key-name deepseek_api_key
```

`--endpoint` is the API's base URL, not the full `chat/completions` path
(the `openai` client appends that itself).

This runs the full probe — tool-name/parameter elicitation plus every
capability test below — and writes:

- `reports/<model>/capabilities_<model>.json`
- `reports/<model>/capabilities_<model>.md`

Every extra capability test is on by default; pass its `--no-*` flag to
skip one, e.g. `--no-stream-test`. Local wrapper scripts (auth handled
internally, non-OpenAI transport) can be probed in place of an HTTP
endpoint with `--script path/to/script.py`; the script must read one
Chat-Completions JSON payload from stdin and print one JSON response to
stdout.

Run `python3 probe_inference.py --help` for every flag.

### Re-render a report without re-probing

```bash
python3 probe_inference.py --model deepseek-v4-flash --render-md
```

### Compare tool-calling support across all probed models

```bash
python3 probe_inference.py --quick-summary
```

## Capabilities

| Codename | What it measures |
|---|---|
| `TCALL` | Structured `tool_calls` vs. inline JSON/XML/no call |
| `QUOTE` | Literal double-quotes preserved inside JSON string args |
| `GREP` | Prefers filtered/targeted calls over pulling whole files/outputs into context |
| `ASKQ` | How strongly task phrasing drives use of an `ask_user_question` tool |
| `GRAMK` | Model's own knowledge of OpenAI's `apply_patch` grammar (no tool schema) |
| `GRAMT` | Endpoint's transport support for real grammar-constrained custom tools |
| `RJSON` | Endpoint honours strict `response_format:{type:"json_schema"}` |
| `STRM` | Endpoint delivers real incremental SSE chunks under `stream:true` |
| `REASN` | Reasoning tokens exposed, and whether reasoning-effort syntax is accepted |
| `TSEL` | Model calls the tool it itself proposed, without a naming conflict |

Full definitions, units, and ranges: [CAPABILITIES.md](CAPABILITIES.md).

## Reports

Each `reports/<model>/capabilities_<model>.md` includes a capabilities
summary table, the inferred tool schema, the tool-dispatch table, and a
per-capability breakdown with pass/fail detail. `probes/<model>/` (not
checked in) holds the raw request/response JSON for every probe call, for
debugging.

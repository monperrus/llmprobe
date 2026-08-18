# Capabilities reference

`probe_inference.py` scores each probed model on a fixed set of capabilities.
Every capability has a stable codename (max 5 characters) used as its key in
`tool_schema_<model>.json` and as a heading tag in `tool_schema_<model>.md`.
Codenames do not change across probe runs or script versions, so results are
comparable model-to-model and run-to-run.

## `TCALL`

**Capability:** Model emits tool invocations via the structured OpenAI
`tool_calls` API field rather than falling back to inline JSON in the
message content (or XML tags, or no detectable call at all).

- **Unit:** tasks passed / tasks run
- **Range:** 0 to N tasks (N = 1 for the round-0 single-call check, N = 8
  for the full multi-op probe)
- **Source:** `format_detection`, `behaviour`

## `QUOTE`

**Capability:** Model preserves literal double-quote characters inside JSON
string arguments without dropping or mangling them. Each task advertises
*only* the one tool relevant to it (not the full schema), so a model that
would otherwise pick a different tool for the task (see `GREP`/dispatch
notes) is still exercised on quoting specifically, rather than failing here
for an unrelated tool-selection reason.

- **Unit:** tasks passed / tasks run
- **Range:** 0 to 3 tasks
- **Source:** `quote_test` (enabled with `--quote-test`)

## `GREP`

**Capability:** Model prefers a filtered/targeted call (`grep`, `sed -n`,
`head`, `wc -l`, a dedicated search tool, or a read with offset/limit) over
pulling an entire large file/output into context.

- **Unit:** tasks passed / tasks run
- **Range:** 0 to 6 tasks
- **Source:** `token_efficiency_test` (enabled with `--efficiency-test`)

## `ASKQ`

**Capability:** Model is able to ask users questions — it calls its own
`ask_user_question` tool (rather than guessing or investigating
unilaterally) when a task calls for user input, across 8 phrasings of the
same underlying "should I overwrite or back up this file?" task, all run
against the full tool schema.

- **Unit:** variants passed / variants run, plus a 5-point Likert frequency
  label
- **Range:** 0 to 8 variants → `Never` / `Rarely` / `Sometimes` / `Often` /
  `Always`
- **Source:** `askq_test` (enabled with `--askq-test`)

## `TSEL`

**Capability:** Model calls the tool it itself proposed for an operation
during Round 1 elicitation, rather than substituting a different tool
already claimed by another op, when probed against the full competing
schema in Round 2. Derived from `tool_dispatch`/`dispatch_conflicts`, not a
separate probe round — it's always computed whenever the main probe runs.

- **Unit:** ops without a conflict / ops with an elicited tool name
- **Range:** 0 to 8 ops
- **Source:** `dispatch_conflicts` (built inside `build_tool_dispatch()`,
  always populated as part of the main probe — no flag needed)

## `GRAMK`

**Capability:** Model naturally knows OpenAI's `apply_patch` envelope grammar
from pretraining alone — no tool schema is offered, no
example is shown. The model is asked in free text to produce only the raw
patch, and the result is parsed against the real grammar (not a loose
regex), so this measures grammar knowledge specifically, decoupled from
whether the probing endpoint actually exposes the freeform `apply_patch`
tool.

- **Unit:** tasks passed / tasks run
- **Range:** 0 to 2 tasks
- **Source:** `gram_knowledge_test` (enabled by default; disable with `--no-gram-knowledge-test`)

## `GRAMT`

**Capability:** Endpoint honours a genuine OpenAI custom/freeform tool
(`type:"custom"`, `format:{type:"grammar", syntax:"lark", ...}`) end to end —
a `custom` tool_call comes back with grammar-valid input, rather than the
endpoint falling back to classic function calling or rejecting the request.

- **Unit:** tasks passed / tasks run
- **Range:** 0 to 1 task
- **Source:** `gram_transport_test` (enabled by default; disable with `--no-gram-transport-test`)

## `RJSON`

**Capability:** Endpoint honours strict structured output — a
`response_format:{type:"json_schema"}` request (no tool schema) is accepted
and the reply content parses as JSON conforming to the given schema. Like
`GRAMT`, this is an endpoint/provider feature, not a model behaviour.

- **Unit:** tasks passed / tasks run
- **Range:** 0 to 1 task
- **Source:** `rjson_test` (enabled by default; disable with `--no-rjson-test`)

## `STRM`

**Capability:** Endpoint delivers a `stream:true` response as real
incremental Server-Sent Events rather than rejecting the parameter or
accepting it but buffering the whole reply into a single chunk. PASS
requires more than one `ChatCompletionChunk`, non-empty content
reconstructed from `delta.content` across chunks, and a chunk carrying
`finish_reason`. An endpoint/provider feature, not a model behaviour.

- **Unit:** tasks passed / tasks run
- **Range:** 0 to 1 task
- **Source:** `stream_test` (enabled by default; disable with `--no-stream-test`)

## `REASN`

**Capability:** Two related endpoint/provider checks around reasoning
tokens:

1. `reasoning_tokens_present` — with no special params, does the reply
   carry a reasoning trace? Checked via a `reasoning_content` / `reasoning`
   / `thinking` field on the message, or a non-zero
   `usage.completion_tokens_details.reasoning_tokens`.
2. `effort_control_native` / `effort_control_extra_body` — does the
   endpoint accept a way to tune reasoning effort without a 400? Two wire
   syntaxes are tried: the native top-level `reasoning_effort` Chat
   Completions param (OpenAI o-series/gpt-5), and the OpenRouter-style
   `extra_body={"reasoning": {"effort": ...}}` passthrough many other
   providers proxy. Accepting the parameter is the bar — this does not
   attempt to prove the setting changed model behaviour, only that the
   syntax is honoured rather than rejected.

- **Unit:** checks passed / checks run
- **Range:** 0 to 3 checks
- **Source:** `reasoning_test` (enabled by default; disable with `--no-reasoning-test`)

## `TSEL`

**Capability:** Model calls the tool it itself proposed for an operation
during Round 1 elicitation, rather than substituting a different tool
when both are available in the offered schema. See `dispatch_conflicts`
in the report for which tool the model substituted instead.

## GRAMT detail

The *endpoint* implements OpenAI's real freeform/custom-tool
transport end to end — `type: "custom"` with
`format: {type: "grammar", syntax: "lark", ...}` — rather than only classic
JSON-schema function calling. Sends the actual upstream `apply_patch.lark`
grammar (unmodified, as OpenAI/Codex define it) as a genuine custom tool;
PASS requires a real `custom` tool_call back (not a `function` one, and not
silently dropped/ignored) whose raw `input` text is grammar-valid.

This is a transport/capability question distinct from `GRAMK`: a
model can score `GRAMK` 2/2 (know the grammar cold) while its
endpoint scores `GRAMT` 0/1 (can't accept the tool that would let
it use that knowledge natively) — confirmed directly: `gpt-5-mini`'s
Copilot endpoint 400s on this request shape entirely; `gpt-5.6-luna`'s
Responses API returns a genuine `custom_tool_call` once its wrapper's
translator passes `type:"custom"` tools through instead of silently
dropping them. See `~/bin/copilot-notes.md` for the full writeup.

- **Unit:** tasks passed / tasks run
- **Range:** 0 to 1 task
- **Source:** `gram_transport_test` (enabled by default; disable with `--no-gram-transport-test`)

## Notes on scoring

- All ten capabilities are pass/fail per task; the reported value is
  `passed/total`, not a normalized score. Compare N against the same
  codename's range before comparing two models' fractions.
- `TCALL` is measured twice at different granularity: once as a single
  boolean check in round 0 (`format_detection.has_structured_tool_calls`),
  and once as a per-task breakdown across the 8-op probe
  (`behaviour.structured_tool_calls` / `.inline_json_in_content` /
  `.no_call_detected`). The markdown report shows both under one `TCALL`
  heading.
- `QUOTE`, `GREP`, `ASKQ`, `GRAMK`, `GRAMT`, `RJSON`, `STRM`, and `REASN`
  are on by default (`--quote-test` / `--efficiency-test` / `--askq-test` /
  `--gram-knowledge-test` / `--gram-transport-test` / `--rjson-test` /
  `--stream-test` / `--reasoning-test`); pass the `--no-*` form of any flag
  to skip it, in which case its JSON field is `null` and the markdown
  report omits the section. `TSEL` has no flag — it's always available
  once the main probe runs.
- `TSEL` failures are what used to show up as a bare, uncoded "Tool X was
  never dispatched" line in "Missing capabilities". They're now tagged
  `TSEL_<op>` for consistency with `QUOTE_<op>`, `GREP_<op>`, and
  `ASKQ_<variant>` — every per-item failure line names the capability
  family it belongs to.
- A `FAIL` on `GREP` does not mean the model got the *answer* wrong — the
  probe never executes the tool call, so correctness of results is out of
  scope. It only measures whether the model *chose* a token-cheap call
  shape (see `probe_inference.py::_classify_bash_command` /
  `_classify_read_args` for the exact heuristics).
- `ASKQ` deliberately runs each of its 8 phrasings **once**, with no retries.
  This is by design, not a shortcut: retrying to smooth out sampling noise
  would hide the fact that borderline phrasings can flip pass/fail run to
  run even at temperature 0 (observed directly while developing this test —
  the exact same phrasing and schema produced ASKED in one run and SKIPPED
  in another). `ASKQ` measures sensitivity to *phrasing*, across a fixed set
  of phrasings chosen to span explicit-imperative through
  implicit/ambiguous framing; it is not a statistically robust reliability
  estimate for any single phrasing. A low `ASKQ` score means "this model's
  ask-the-user behavior is phrasing-fragile," not "this model never asks."

# Capabilities reference

`probe_inference.py` scores each probed model on a fixed set of capabilities.
Every capability has a stable codename (max 5 characters) used as its key in
`tool_schema_<model>.json` and as a heading tag in `tool_schema_<model>.md`.
Codenames do not change across probe runs or script versions, so results are
comparable model-to-model and run-to-run.

| Codename | Meaning | Unit | Range | Source |
|---|---|---|---|---|
| `TCALL` | Model emits tool invocations via the structured OpenAI `tool_calls` API field rather than falling back to inline JSON in the message content (or XML tags, or no detectable call at all). | tasks passed / tasks run | 0 to N tasks (N = 1 for the round-0 single-call check, N = 8 for the full multi-op probe) | `format_detection`, `behaviour` |
| `QUOTE` | Model preserves literal double-quote characters inside JSON string arguments without dropping or mangling them. | tasks passed / tasks run | 0 to 3 tasks | `quote_test` (enabled with `--quote-test`) |
| `GREP` | Model prefers a filtered/targeted call (`grep`, `sed -n`, `head`, `wc -l`, a dedicated search tool, or a read with offset/limit) over pulling an entire large file/output into context. | tasks passed / tasks run | 0 to 6 tasks | `token_efficiency_test` (enabled with `--efficiency-test`) |

## Notes on scoring

- All three capabilities are pass/fail per task; the reported value is
  `passed/total`, not a normalized score. Compare N against the same
  codename's range column before comparing two models' fractions.
- `TCALL` is measured twice at different granularity: once as a single
  boolean check in round 0 (`format_detection.has_structured_tool_calls`),
  and once as a per-task breakdown across the 8-op probe
  (`behaviour.structured_tool_calls` / `.inline_json_in_content` /
  `.no_call_detected`). The markdown report shows both under one `TCALL`
  heading.
- `QUOTE` and `GREP` are opt-in (`--quote-test` / `--efficiency-test`); when
  not requested, their JSON field is `null` and the markdown report omits
  the section.
- A `FAIL` on `GREP` does not mean the model got the *answer* wrong — the
  probe never executes the tool call, so correctness of results is out of
  scope. It only measures whether the model *chose* a token-cheap call
  shape (see `probe_inference.py::_classify_bash_command` /
  `_classify_read_args` for the exact heuristics).

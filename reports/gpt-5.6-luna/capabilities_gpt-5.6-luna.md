# Model capability probe: gpt-5.6-luna

- **Endpoint:** script:/home/martin/bin/copilot-gpt-5.6-luna.py
- **API type:** OpenAI Responses

## Capabilities summary

See `CAPABILITIES.md` for what each codename measures, its unit, and its range.

| Codename | Value |
|---|---|
| `TCALL` | 7/8 |
| `QUOTE` | *(not run — rerun without `--no-quote-test`)* |
| `GREP` | *(not run — rerun without `--no-efficiency-test`)* |
| `ASKQ` | *(not run — rerun without `--no-askq-test`)* |
| `GRAMK` | *(not run — rerun without `--no-gram-knowledge-test`)* |
| `GRAMT` | 1/1 |
| `RJSON` | 1/1 |
| `STRM` | *(not run — rerun without `--no-stream-test`)* |
| `REASN` | *(not run — rerun without `--no-reasoning-test`)* |
| `TSEL` | 7/8 |

## Format detection & call delivery (`TCALL`)

- Round-0 probe (single call): detected format `structured_tool_calls`, structured tool_calls used: True

- Full probe (8 tasks): call delivery mode `mixed`
  - Structured tool_calls: 7/8 tasks
  - Inline JSON in content (model ignored the tools API and put the call as JSON text in the message body instead): 0/8 tasks
  - No call detected (neither a structured tool_call nor parseable inline JSON): 1/8 tasks
- Note: Model correctly uses the structured tool_calls API field.

## Elicited tool names

Round 1 asks the model, in free text with no tool schema attached, what function/arguments it would use for each task. The prompt never names a tool — the model must invent the name itself.

| Operation | Elicitation prompt | Model function name |
|---|---|---|
| read_file | You need to read the contents of the file /etc/hostname. What function would you call and what arguments would you pass? Respond with a JSON object like: {"function_name": "...", "arguments": {"arg1": "val1", ...}} | `read_file` |
| write_file | You need to write the text 'hello world' to the file /tmp/test.txt. What function would you call and what arguments would you pass? Respond with a JSON object like: {"function_name": "...", "arguments": {"arg1": "val1", ...}} | `write_file` |
| update_file | The file /tmp/test.py already exists and contains Python code. You need to make a targeted edit: replace the exact string 'x = 1' with 'x = 42', without rewriting the whole file. What function would you call and what arguments would you pass? Respond with a JSON object like: {"function_name": "...", "arguments": {"arg1": "val1", ...}} | `replace_in_file` |
| execute_bash | You need to run the shell command `ls -la /tmp`. What function would you call and what arguments would you pass? Respond with a JSON object like: {"function_name": "...", "arguments": {"arg1": "val1", ...}} | `shell` |
| ask_user_question | You need to ask the user a clarifying question: 'Should I overwrite the existing file, or create a backup first?' with options 'Overwrite' and 'Backup'. What function would you call and what arguments would you pass? Respond with a JSON object like: {"function_name": "...", "arguments": {"arg1": "val1", ...}} | `ask_user_question` |
| list_directory | You need to list all files and subdirectories inside /tmp. What function would you call and what arguments would you pass? Respond with a JSON object like: {"function_name": "...", "arguments": {"arg1": "val1", ...}} | `list_directory` |
| search_files | You need to find every line containing the string 'def main' in any file under /tmp/myproject (search recursively). What function would you call and what arguments would you pass? Respond with a JSON object like: {"function_name": "...", "arguments": {"arg1": "val1", ...}} | `grep` |
| glob | You need to find all Python source files (matching *.py) anywhere under /tmp/myproject, recursively. What function would you call and what arguments would you pass? Respond with a JSON object like: {"function_name": "...", "arguments": {"arg1": "val1", ...}} | `glob` |

## Inferred tool schema

### `read_file`

Perform the 'read_file' operation.

| Parameter | Type | Required |
|---|---|---|
| path | string | yes |

### `write_file`

Perform the 'write_file' operation.

| Parameter | Type | Required |
|---|---|---|
| path | string | yes |
| content | string | yes |

### `replace_in_file`

Perform the 'update_file' operation.

| Parameter | Type | Required |
|---|---|---|
| path | string | yes |
| old_string | string | yes |
| new_string | string | yes |

### `shell`

Perform the 'execute_bash' operation.

| Parameter | Type | Required |
|---|---|---|
| command | string | yes |

### `ask_user_question`

Perform the 'ask_user_question' operation.

| Parameter | Type | Required |
|---|---|---|
| question | string | yes |

### `list_directory`

Perform the 'list_directory' operation.

| Parameter | Type | Required |
|---|---|---|
| path | string | yes |

### `grep`

Perform the 'search_files' operation.

| Parameter | Type | Required |
|---|---|---|
| pattern | string | yes |
| path | string | yes |
| recursive | boolean | yes |

### `glob`

Perform the 'glob' operation.

| Parameter | Type | Required |
|---|---|---|
| pattern | string | yes |
| recursive | boolean | yes |

## Tool dispatch table

| Model tool name | Python function | Param map |
|---|---|---|
| `read_file` | `t_read` | path→path |
| `write_file` | `t_write` | path→path, content→content |
| `replace_in_file` | `t_update` | path→path, old_string→old, new_string→new |
| `shell` | `t_run` | command→command |
| `list_directory` | `t_list_dir` | path→path |
| `glob` | `t_glob` | *(none)* |

## Constrained-decoding / custom-tool test (`GRAMT`)

**1/1 passed** — sends a real OpenAI `type:"custom"` freeform tool with `format:{type:"grammar", syntax:"lark"}`; PASS requires a genuine `custom` tool_call back with grammar-valid input (not a classic `function` tool_call, and not silently ignored). Tests the *endpoint's* transport support, independent of whether the model knows the syntax (`GRAMK`) — see `~/bin/copilot-notes.md`.

| Operation | Result | Tool call type | Notes |
|---|---|---|---|
| apply_patch | PASS | `custom` |  |

## Structured-output test (`RJSON`)

**1/1 passed** — sends a strict `response_format:{type:"json_schema"}` request with no tool schema; PASS requires the endpoint to accept the request and return content that parses as JSON conforming to the schema. Tests the *endpoint's* structured-output support, independent of tool calling.

| Task | Result | Conformant | Notes |
|---|---|---|---|
| json_schema | PASS | yes |  |

## Missing capabilities

- `TSEL_ask_user_question` FAILED — tool `ask_user_question` is in the inferred schema but was never dispatched (the model didn't call it with a matching signature during Round 2/3 probing).
- `TSEL_search_files` FAILED — op 'search_files' probe call resolved to `shell` instead of `grep`.
- 1 probe task(s) produced no detectable tool call at all (see probes/<model>/round2_*.json for which ones).
- `QUOTE` capability not tested (rerun without --no-quote-test).
- `GREP` capability not tested (rerun without --no-efficiency-test).
- `ASKQ` capability not tested (rerun without --no-askq-test).
- `GRAMK` capability not tested (rerun without --no-gram-knowledge-test).
- `STRM` capability not tested (rerun without --no-stream-test).
- `REASN` capability not tested (rerun without --no-reasoning-test).

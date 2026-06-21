# llmprobe

Probe any OpenAI-compatible model's tool-call behaviour.

Reverse-engineers a model's preferred tool names and parameters across five canonical operations (read_file, write_file, update_file, execute_bash, ask_user_question) and produces a JSON spec used by [agentknit](https://github.com/monperrus/agentknit) to drive a coding agent.

## Install

```
pip install llmprobe
```

## Usage

```
llmprobe --model qwen/qwen3-8b --endpoint https://openrouter.ai/api/v1
```

Produces `agent_spec_<model>.json` with the probed tool schema and dispatch table.

### Programmatic

```python
from llmprobe import make_client, get_api_key, elicit_round, build_tool_schema, probe_round, behavioural_summary

client = make_client(get_api_key())
elicited = elicit_round(client)
tools = build_tool_schema(elicited)
calls = probe_round(client, tools)
print(behavioural_summary(calls))
```

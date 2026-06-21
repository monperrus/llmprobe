"""llmprobe — probe any OpenAI-compatible model's tool-call behaviour."""

__version__ = "0.1.0"

from .exceptions import AgentProbeError, AuthenticationError
from .probe import (
    main,
    parse_args,
    get_api_key,
    make_client,
    format_detection_round,
    provider_capabilities_round,
    elicit_round,
    probe_round,
    build_tool_schema,
    build_tool_dispatch,
    behavioural_summary,
    quick_summary,
    quote_test_round,
    create_agent_file,
    extract_call_from_response,
    extract_json_block,
    ToolCallResult,
    ELICIT_TASKS,
    PROBE_TASKS,
    ENDPOINT,
    MODEL,
)

__all__ = [
    "AgentProbeError", "AuthenticationError",
    "main", "parse_args", "get_api_key", "make_client",
    "format_detection_round", "provider_capabilities_round",
    "elicit_round", "probe_round", "build_tool_schema", "build_tool_dispatch",
    "behavioural_summary", "quick_summary", "quote_test_round", "create_agent_file",
    "extract_call_from_response", "extract_json_block", "ToolCallResult",
    "ELICIT_TASKS", "PROBE_TASKS", "ENDPOINT", "MODEL",
]

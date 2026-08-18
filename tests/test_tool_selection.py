"""Unit tests for the TSEL full-schema tool-selection score."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import probe_inference as pi


def test_tool_selection_scores_the_expected_tool_not_dispatch_uniqueness():
    calls = {
        "read_file": pi.ToolCallResult("read", {"path": "/tmp/a"}, True),
        "write_file": pi.ToolCallResult("read", {"path": "/tmp/a"}, True),
        "update_file": None,
    }
    result = pi.tool_selection_test(calls, {
        "read_file": "read",
        "write_file": "write",
        "update_file": "replace",
    })

    assert result["tsel_passed"] == 1
    assert result["tsel_total"] == 3
    assert result["tsel_results"]["write_file"]["error"] == "called 'read' instead of 'write'"
    assert result["tsel_results"]["update_file"]["error"] == "no tool call detected"

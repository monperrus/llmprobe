"""Tests: a 429 rerun keeps the previous run's report results.

Covers _load_previous_report, _is_rate_limit_error, _keep_previous_result
and _restore_previous_on_429 from probe_inference.py.
"""
import json
import sys
from pathlib import Path

import httpx
import openai

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import probe_inference as pi


def make_429(msg="Weekly/Monthly Limit Exhausted"):
    req = httpx.Request("POST", "https://api.z.ai/v1/chat/completions")
    resp = httpx.Response(429, request=req, json={"error": {"code": "1310", "message": msg}})
    return openai.RateLimitError(message=msg, response=resp, body=resp.json())


def test_rate_limit_detection():
    e = make_429()
    assert pi._is_rate_limit_error(e), "RateLimitError should be detected"

    class FakeStatus:
        status_code = 429
    assert pi._is_rate_limit_error(FakeStatus())

    class FakeStr(Exception):
        pass
    assert not pi._is_rate_limit_error(FakeStr("boom"))


def test_keep_previous_result():
    prev_ok = {"format_detection": {"detected_format": "openai", "has_structured_tool_calls": True}}
    e = make_429()
    assert pi._keep_previous_result(e, prev_ok, "format_detection")
    assert not pi._keep_previous_result(e, prev_ok, "quote_test")           # missing key
    assert not pi._keep_previous_result(e, {"format_detection": {"error": "x"}}, "format_detection")  # prev also failed
    assert not pi._keep_previous_result(Exception("boom"), prev_ok, "format_detection")  # not 429


def test_restore_keeps_fresh_results(tmp_path):
    previous = {
        "status": "ok",
        "format_detection": {"detected_format": "openai", "has_structured_tool_calls": True},
        "elicited_names": {"read_file": "read_file"},
        "behaviour": {"structured_tool_calls": 8},
        "gram_test": {"gram_passed": 0, "gram_total": 1, "gram_results": {}},
    }
    output = {
        "status": "incomplete", "error": None,
        "format_detection": {}, "elicited_names": {},
        "behaviour": None, "gram_test": None,
    }
    # a fresh GRAM result measured this run must not be overwritten
    fresh_gram = {"gram_passed": 1, "gram_total": 1, "gram_results": {"apply_patch": {"pass": True}}}
    output["gram_test"] = fresh_gram

    pi._restore_previous_on_429(previous, output)

    assert output["format_detection"] == previous["format_detection"]
    assert output["elicited_names"] == previous["elicited_names"]
    assert output["behaviour"] == previous["behaviour"]
    assert output["gram_test"] == fresh_gram

    # error-only previous entries are not restored
    output2 = {"format_detection": {}}
    pi._restore_previous_on_429({"format_detection": {"error": "x"}}, output2)
    assert output2["format_detection"] == {}


def test_load_previous_report(tmp_path):
    path = tmp_path / "capabilities_m.json"
    assert pi._load_previous_report(str(path)) == {}
    prev = {"status": "ok", "elicited_names": {"read_file": "read_file"}}
    path.write_text(json.dumps(prev))
    assert pi._load_previous_report(str(path)) == prev
    path.write_text("{not json")
    assert pi._load_previous_report(str(path)) == {}

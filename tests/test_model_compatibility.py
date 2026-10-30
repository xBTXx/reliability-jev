"""Provider settings, grounded citation compaction, progress and interrupted requests; offline."""

import json
from copy import deepcopy
from threading import Event
from unittest.mock import Mock

import httpx
import pytest
import test_agent as contracts
from test_reliability import act
from test_supervision import advice

from jev_ultrafast import costs, model, progress
from jev_ultrafast import supervision as s


@pytest.fixture
def runner():
    return contracts.runner.__wrapped__()


@pytest.mark.parametrize("base", ["https://openrouter.ai/api/v1", "https://api.cerebras.ai/v1"])
def test_gpt_oss_none_uses_low_and_preserves_the_adjustment_in_helper_evidence(monkeypatch, base):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", base)
    monkeypatch.setenv("TEXT_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "none")
    post = Mock(return_value={"choices": [{"finish_reason": "stop", "message": {"content": '{"text":"Example"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    value, info = model.field_text({"goal": "Enter Example", "field": {"label": "Label"}})
    body = post.call_args.args[2]
    expected = {"effort": "low"} if "openrouter" in base else "low"
    assert body.get("reasoning", body.get("reasoning_effort")) == expected
    assert body["max_tokens"] == 2048
    assert value == "Example" and info["reasoning"]["applied"] == "low"
    assert info["reasoning"]["requested"] == "none" and info["reasoning"]["adjustment"]


def test_hybrid_model_can_still_disable_reasoning():
    options, info = model.reasoning_options("https://openrouter.ai/api/v1", "qwen/qwen3.8-flash", "none")
    assert options == {"reasoning": {"enabled": False}}
    assert info["adjustment"] is None


def test_completion_with_extra_grounded_citations_is_compacted_with_current_evidence_first(runner):
    context = s.review_context(runner.state, "proposed_completion")
    context["observations"][0]["data"]["text"] = "Old A Old B Old C Old D Old E"
    context["observations"].append({"id": "o2", "data": {"text": "Current final result"}})
    context["current_observation"] = "o2"
    output = advice(context, "COMPLETE")
    output["requirements"][0].update(status="supported", evidence=[
        *[{"source": "o1", "quote": "Old " + letter} for letter in "ABCDE"],
        {"source": "o2", "quote": "Current final result"},
    ])
    original = deepcopy(output)
    result = s.validate_review(output, context)
    assert result["status"] == "COMPLETE"
    assert len(result["requirements"][0]["evidence"]) == 3
    assert result["requirements"][0]["evidence"][0]["source"] == "o2"
    assert output == original  # Raw provider evidence stays intact.
    schema = s.review_schema()["properties"]["requirements"]["items"]["properties"]["evidence"]
    assert schema["maxItems"] == 3
    output["requirements"][0]["evidence"][-1]["quote"] = "Invented evidence beyond the first three"
    with pytest.raises(s.SupervisorError, match="absent"):
        s.validate_review(output, context)


def test_http_400_stops_cleanly_with_redacted_diagnostics_and_no_browser_input(runner, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "private-test-key")
    error = {"error": {"message": "Provider error", "code": 400, "metadata": {
        "provider_name": "Cerebras", "raw": json.dumps({"error": {"message":
            "Reasoning cannot be disabled. private-test-key Bearer sk-test-secret"},
            "headers": {"Authorization": "Never copy these headers"}}),
    }}}
    post = Mock(return_value=httpx.Response(400, json=error))
    monkeypatch.setattr(model.CLIENT, "post", post)
    result = act(runner)
    assert result["status"] == "blocked"
    assert result["stop_reason"]["code"] == "model_provider_error"
    assert "Reasoning cannot be disabled" in result["stop_reason"]["message"]
    ledger = json.dumps(result["model_calls"])
    assert "private-test-key" not in ledger and "sk-test-secret" not in ledger
    assert "Never copy these headers" not in ledger
    assert len(result["model_calls"]) == 1 and post.call_count == 1
    runner.state["browser"].act.assert_not_called()


def test_slow_request_reports_heartbeats_and_stops_them_after_the_response():
    events, waiting = [], Event()

    def receive(event):
        events.append(event)
        if event["phase"] == "waiting":
            waiting.set()

    with progress.progress_scope(receive):
        with progress.request_progress(role="supervisor", model="test", attempt=1, timeout=90, interval=0.01) as item:
            assert waiting.wait(2)
            item["status"] = "HTTP 200"
    assert events[0]["phase"] == "started" and events[-1]["phase"] == "finished"
    assert events[-1]["status"] == "HTTP 200"
    assert progress.CALLBACK.get() is None


def test_interrupt_during_repair_preserves_the_rejected_reply_and_pending_cost(runner, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    first = httpx.Response(200, json={"usage": {"cost": 0.001}, "choices": [
        {"finish_reason": "stop", "message": {"content": '{}'}}]})
    post = Mock(side_effect=[first, KeyboardInterrupt()])
    monkeypatch.setattr(model.CLIENT, "post", post)
    runner.state["supervisor_calls"] = []
    with pytest.raises(KeyboardInterrupt):
        runner.command("tick")
    review = runner.state["supervisor_calls"][0]
    assert review["attempt_count"] == 2 and review["model"]
    assert review["attempts"][0]["response_content"] == '{}'
    assert "error" in review["attempts"][0]
    assert review["attempts"][1]["error"] == "interrupted"
    assert runner.state["stop_reason"]["code"] == "interrupted"
    assert len(runner.state["model_calls"]) == 2
    summary = costs.cost_summary(runner.state)
    assert summary["known_cost_usd"] == 0.001 and summary["total_usd"] is None
    runner.state["browser"].act.assert_not_called()


def test_supervisor_reasoning_effort_is_separate_and_explicit(runner, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "none")
    monkeypatch.setenv("SUPERVISOR_REASONING_EFFORT", "low")
    context = s.review_context(runner.state, "initialize")
    post = Mock(return_value={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(advice(context))}}]})
    monkeypatch.setattr(s, "post_json", post)
    output, info = s.supervise(context)
    assert post.call_args.args[2]["reasoning"] == {"effort": "low"}
    assert info["reasoning"]["applied"] == "low" and output["status"] == "CONTINUE"

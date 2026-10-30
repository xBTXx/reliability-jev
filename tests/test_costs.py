"""Billing regressions: failed replies, repairs, caching, retry coverage, and CLI output; offline."""

import json
import runpy
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import test_agent as contracts
from test_reliability import act
from test_supervision import advice

import jev_ultrafast
from jev_ultrafast import costs, model
from jev_ultrafast.browser import StalePage


def call(role, usage, model_id="test-model"):
    return {"role": role, "model": model_id, "usage": usage}


def state(calls):
    return {"model_calls": calls, "cost_pricing": costs.pricing()}


def response(content, cost, status=200):
    return httpx.Response(status, json={"model": "deepseek/test", "provider": "Together",
                                       "usage": {"cost": cost},
                                       "choices": [{"finish_reason": "stop", "message": {"content": content}}]})


@pytest.fixture
def runner():
    return contracts.runner.__wrapped__()


def test_total_combines_reported_costs_and_billable_jev_input_only():
    summary = costs.cost_summary(state([
        call("jev", {"input_tokens": 1_000_000, "output_tokens": 999_999}, "jev-1.14.0"),
        call("text_helper", {"cost": "0.001", "cost_details": {"upstream_inference_cost": 100}}),
        call("supervisor", {"cost": 0.002}),
    ]))
    assert summary["total_usd"] == 0.045
    assert summary["reported_cost_usd"] == 0.003
    assert summary["estimated_cost_usd"] == 0.042
    assert summary["complete"] and summary["has_estimates"]
    assert "Total (estimated): $0.045000" in costs.format_cost(summary)


def test_zero_cost_is_reported_and_no_requests_costs_zero():
    empty = costs.cost_summary(state([]))
    assert empty["total_usd"] == 0 and "Total: $0.000000" in costs.format_cost(empty)
    summary = costs.cost_summary(state([call("supervisor", {"cost": 0})]))
    assert summary["by_role"]["supervisor"]["reported_requests"] == 1
    assert summary["complete"]


@pytest.mark.parametrize("value", [None, True, -1, "NaN", float("inf"), {}, "invalid"])
def test_invalid_cost_never_becomes_zero_or_a_complete_total(value):
    summary = costs.cost_summary(state([call("supervisor", {"cost": value})]))
    assert summary["total_usd"] is None
    assert summary["unavailable_requests"] == 1
    assert "Supervisor: unavailable" in costs.format_cost(summary)


def test_partial_cost_is_a_known_subtotal_and_preserves_coverage():
    summary = costs.cost_summary(state([call("text_helper", {"cost": 0.001}),
                                        call("text_helper", {}), call("supervisor", {"cost": 0.002})]))
    assert summary["known_cost_usd"] == 0.003 and summary["total_usd"] is None
    assert "Known subtotal: $0.003000; total unavailable" in costs.format_cost(summary)
    assert "Text helper: $0.001000 known" in costs.format_cost(summary)


def test_foreign_currency_and_unknown_jev_model_are_unavailable_without_override(monkeypatch):
    calls = [call("supervisor", {"cost": 1, "currency": "EUR"}),
             call("jev", {"input_tokens": 1_000_000}, "jev-future")]
    assert costs.cost_summary(state(calls))["unavailable_requests"] == 2
    monkeypatch.setenv("TYPESAFE_INPUT_USD_PER_MILLION", "0.10")
    summary = costs.cost_summary(state(calls[1:]))
    assert summary["total_usd"] == 0.10 and summary["has_estimates"]


def test_saved_rate_and_reported_cost_take_precedence_over_changed_defaults(monkeypatch):
    original = state([call("jev", {"input_tokens": 1_000_000}, "jev-1.14.0")])
    monkeypatch.setenv("TYPESAFE_INPUT_USD_PER_MILLION", "50")
    assert costs.cost_summary(original)["total_usd"] == 0.042
    original["model_calls"][0]["usage"]["cost"] = 0.012
    assert costs.cost_summary(original)["total_usd"] == 0.012


def test_legacy_supervisor_attempts_are_used_instead_of_the_aggregate():
    saved = {"decisions": [{"model": "jev-1.14.0", "usage": {"input_tokens": 1_000_000}}],
             "text_calls": [{"usage": {"cost": 0.001}}],
             "supervisor_calls": [{"usage": {"cost": 0.007},
                                   "attempts": [{"usage": {"cost": 0.003}}, {"usage": {"cost": 0.004}}]}],
             "history": [{"text": "cached value"}] * 5}
    summary = costs.cost_summary(saved)
    assert summary["total_usd"] == 0.05
    assert summary["by_role"]["supervisor"]["requests"] == 2
    assert summary["basis"] == "response_logs"
    saved["model_calls"] = []  # An explicit empty ledger means no HTTP requests occurred.
    assert costs.cost_summary(saved)["total_usd"] == 0


def test_cost_of_rejected_jev_response_survives_validation_failure(runner, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "private-test-key")
    post = Mock(return_value=httpx.Response(200, json={"model": "jev-1.14.0", "answers": {},
                                                      "usage": {"input_tokens": 1_000_000}}))
    monkeypatch.setattr(model.CLIENT, "post", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        runner.command("predict")
    assert runner.snapshot()["cost_summary"]["total_usd"] == 0.042
    assert runner.state["decisions"] == []
    assert "private-test-key" not in json.dumps(runner.state["model_calls"])
    runner.state["browser"].act.assert_not_called()


def test_rejected_helper_value_still_has_a_cost(runner, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model.CLIENT, "post", Mock(return_value=response('{"text":null}', 0.001)))
    act(runner)
    assert runner.snapshot()["cost_summary"]["by_role"]["text_helper"]["known_cost_usd"] == 0.001
    assert runner.state["review_pending"] == "missing_field_value"
    runner.state["browser"].act.assert_not_called()


def test_stale_cached_text_does_not_add_another_paid_request(runner, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value=response('{"text":"book"}', 0.001))
    monkeypatch.setattr(model.CLIENT, "post", post)
    runner.state["browser"].act.side_effect = [StalePage("Before input"), None]
    with pytest.raises(StalePage):
        act(runner)
    act(runner)
    assert post.call_count == 1
    assert runner.snapshot()["cost_summary"]["by_role"]["text_helper"]["requests"] == 1


def test_real_supervisor_repair_cost_counts_both_responses_once(runner, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model.CLIENT, "post", Mock(side_effect=[response('{}', 0.001),
        response(json.dumps(advice({"goal": runner.state["goal"], "task_state": runner.state["task_state"]})), 0.002)]))
    runner.review("initialize")
    assert len(runner.state["supervisor_calls"]) == 2  # Fixture's prior review plus this one.
    assert runner.state["supervisor_calls"][-1]["attempt_count"] == 2
    summary = runner.snapshot()["cost_summary"]
    assert summary["by_role"]["supervisor"]["requests"] == 2
    assert summary["total_usd"] == 0.003


def test_http_retries_keep_missing_usage_visible_and_do_not_leak_scopes(monkeypatch):
    post = Mock(side_effect=[httpx.Response(503, json={"error": "Unavailable"}), response('{}', 0.001)])
    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model.time, "sleep", Mock())
    task = {}
    with costs.track_calls(task, "supervisor"):
        model.post_json("https://example.test/chat/completions", "test", {"model": "test"})
    summary = costs.cost_summary(task)
    assert summary["by_role"]["supervisor"]["requests"] == 2
    assert summary["known_cost_usd"] == 0.001 and not summary["complete"]
    costs.record_call("test", result={"usage": {"cost": 1}})
    assert len(task["model_calls"]) == 2


def test_transport_failure_is_an_unpriced_request(monkeypatch):
    monkeypatch.setattr(model.CLIENT, "post", Mock(side_effect=httpx.ConnectError("Connection failed")))
    task = {}
    with costs.track_calls(task, "jev"), pytest.raises(RuntimeError, match="connection failed"):
        model.post_json("https://example.test/systemone", "test", {"model": "jev-1.14.0"})
    assert costs.cost_summary(task)["total_usd"] is None
    assert task["model_calls"][0]["error"] == "connection_failed"


@pytest.mark.parametrize("failure", [False, True])
def test_cli_prints_and_saves_cost_at_the_end_even_on_failure(runner, monkeypatch, tmp_path, capsys, failure):
    runner.state.update(status="blocked" if failure else "done", elapsed_ms=1234,
                        model_calls=[call("supervisor", {"cost": 0.001})])
    final = runner.snapshot()

    def run():
        if failure:
            raise RuntimeError("Task failed")
        yield final

    fake = Mock(run=run, snapshot=Mock(return_value=deepcopy(final)))
    monkeypatch.setattr(jev_ultrafast, "Agent", Mock(return_value=fake))
    output = tmp_path / "run.json"
    monkeypatch.setattr(sys, "argv", ["run.py", "--url", "https://example.test", "--goal", "Find a book",
                                      "--output", str(output)])
    script = Path(__file__).parents[1] / "examples" / "run.py"
    if failure:
        with pytest.raises(RuntimeError, match="Task failed"):
            runpy.run_path(str(script), run_name="__main__")
    else:
        runpy.run_path(str(script), run_name="__main__")
    text = capsys.readouterr().out
    assert text.rstrip().endswith("Total: $0.001000")
    assert "Task: Find a book" in text
    assert json.loads(output.read_text(encoding="utf-8"))["cost_summary"]["total_usd"] == 0.001
    fake.close.assert_called_once()

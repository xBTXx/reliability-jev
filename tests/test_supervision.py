"""Offline end-to-end controller contracts with scripted models, never paid calls."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
import test_agent as contracts
from test_agent import choice, decision, page
from test_reliability import act

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast import supervision as s
from jev_ultrafast.browser import StalePage, fingerprint


@pytest.fixture
def runner():
    return contracts.runner.__wrapped__()


def advice(context, status="CONTINUE"):
    requirements = deepcopy(context["task_state"]["requirements"]) or [
        {"id": "r1", "text": context["goal"], "status": "pending", "evidence": []},
    ]
    return {"status": status, "objective": "Resolve the current obstacle using observed controls",
            "requirements": requirements, "constraints": ["Preserve accepted form values"],
            "blockers": [], "missing_information": [], "question": None,
            "summary": "Review the observed controls and avoid the failed approach"}


def mock_supervisor(monkeypatch, factory=advice):
    stub = Mock(side_effect=lambda context, **kwargs: (factory(context), {"model": "test", "latency_ms": 1}))
    monkeypatch.setattr(loop, "supervise", stub)
    return stub


def test_gate_review_discards_all_target_heads_in_same_request(monkeypatch):
    def reply(url, key, body):
        q = body["questions"]
        return {"model": "test", "answers": {
            "operation": choice(q["operation"]["criteria"], "TYPE_TEXT"),
            "review_gate": choice(q["review_gate"]["criteria"], "REVIEW"),
            "review_reason": choice(q["review_reason"]["criteria"], "MISSING_INFORMATION"),
            "type_text_target": {"choice": "invented"}, "click_target": {"choice": "bad"},
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    post = Mock(side_effect=reply)
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(page(), "Find a quote", [], task_state={"objective": "Resolve dialog"})
    assert result["choice"] is None and result["target"] is None
    assert result["review_reason"] == "MISSING_INFORMATION"
    assert result["request"]["state"]["task_state"]["objective"] == "Resolve dialog"
    assert post.call_count == 1


def test_missing_gate_response_never_silently_continues(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"model": "test", "answers": {}}))
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a quote", [])


def test_saved_policy_request_preserves_task_state_at_call_time(monkeypatch):
    def reply(url, key, body):
        q = body["questions"]
        return {"model": "test", "answers": {
            "operation": choice(q["operation"]["criteria"], "WAIT"),
            "review_gate": choice(q["review_gate"]["criteria"], "CONTINUE"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", reply)
    task = s.initial_task("Find a quote")
    result = model.choose(page(), "Find a quote", [], task_state=task)
    task["objective"] = "A later objective"
    task["failed_approaches"].append({"code": "later_failure"})
    assert result["request"]["state"]["task_state"]["objective"] == "Find a quote"
    assert result["request"]["state"]["task_state"]["failed_approaches"] == []


def test_agent_constructor_initializes_durable_state_before_any_paid_call(monkeypatch):
    browser = Mock(observe=Mock(return_value=page()))
    monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))
    with loop.Agent("https://example.test", "Find a quote") as runner:
        assert runner.state["task_state"]["objective"] == "Find a quote"
        assert runner.state["task_state"]["reference_time"]
        assert runner.state["observations"][0]["id"] == "o1"
        assert runner.state["supervisor_calls"] == []
        assert s.forced_review(runner.state) == "initialize"
        browser.act.assert_not_called()
    browser.close.assert_called_once()


def test_initialization_and_gate_review_require_fresh_decisions(runner, monkeypatch):
    runner.state["supervisor_calls"] = []
    supervisor = mock_supervisor(monkeypatch)
    policy = Mock(return_value={**decision("e3"), "review_gate": "REVIEW", "review_reason": "OBSTACLE"})
    monkeypatch.setattr(loop, "choose", policy)
    result = runner.command("tick")
    assert result["status"] == "ready" and result["decision"] is None
    policy.assert_not_called()
    assert supervisor.call_args.args[0]["trigger"] == "initialize"
    assert result["task_state"]["requirements"][0]["text"] == "Find a book"
    runner.command("tick")
    assert supervisor.call_count == 2 and policy.call_count == 1
    assert policy.call_args.args[4]["revision"] == 2
    runner.state["browser"].act.assert_not_called()
    assert runner.state["browser"].observe.call_count == 2
    assert runner.state["decision"] is None


def test_supervisor_continue_authorizes_only_one_fresh_policy_attempt(runner, monkeypatch):
    mock_supervisor(monkeypatch)
    runner.review("gate:OBSTACLE")
    policy = Mock(return_value=decision("e3"))
    monkeypatch.setattr(loop, "choose", policy)
    runner.command("tick")
    authorization = policy.call_args.kwargs["review_authorization"]
    assert authorization["fingerprint"] == runner.state["page"]["fingerprint"]
    assert authorization["summary"]
    assert runner.state["browser"].act.call_count == 1
    runner.command("predict")
    assert policy.call_args.kwargs["review_authorization"] is None


def test_changed_observation_invalidates_supervisor_action_authorization(runner, monkeypatch):
    mock_supervisor(monkeypatch)
    runner.review("gate:OBSTACLE")
    runner.state["page"]["text"] = "A new mandatory contact dialog appeared"
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])
    policy = Mock(return_value=decision("e3"))
    monkeypatch.setattr(loop, "choose", policy)
    runner.command("predict")
    assert policy.call_args.kwargs["review_authorization"] is None
    runner.state["browser"].act.assert_not_called()


def test_approved_state_has_one_continue_gate_choice_and_fresh_observed_target(monkeypatch):
    def reply(url, key, body):
        q = body["questions"]
        assert set(q["review_gate"]["criteria"]) == {"CONTINUE"}
        return {"model": "test", "answers": {
            "operation": choice(q["operation"]["criteria"], "CLICK"),
            "review_gate": choice(q["review_gate"]["criteria"], "CONTINUE"),
            "click_target": choice(q["click_target"]["criteria"], "2"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    post = Mock(side_effect=reply)
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(page(), "Find a quote", [], review_authorization={"summary": "Dismiss optional dialog"})
    assert result["review_gate"] == "CONTINUE" and result["choice"] == "e3"
    assert post.call_count == 1


def test_successful_scroll_does_not_erase_unresolved_form_failure(runner):
    runner.feedback("input_rejected", "Field is empty")
    runner.state["recovery"]["effect"] = {"field_node": 10, "expected": "Berlin", "reason": "input_rejected"}
    p = runner.state["page"]
    p["actions"].append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560})
    p["after_action"] = {"status": "confirmed", "reason": "scroll_moved"}
    p["scroll"] = {"y": 560}
    act(runner, "scroll_down")
    assert runner.state["recovery"]["effect"]["reason"] == "input_rejected"
    assert runner.state["recovery"]["remaining"] == 1


@pytest.mark.parametrize("positions,expected", [
    ([560, 0, 560, 0], "state_cycle"),
    ([560, 1120, 1680, 2240, 2800, 3360], "navigation_stall"),
])
def test_scroll_cycles_force_review_even_when_gate_would_continue(runner, monkeypatch, positions, expected):
    p = runner.state["page"]
    p["actions"].append({"id": "scroll", "kind": "scroll", "label": "Scroll", "delta": 560})
    p["after_action"] = {"status": "confirmed", "reason": "scroll_moved"}
    for y in positions:
        p["scroll"] = {"y": y}
        act(runner, "scroll")
    assert s.forced_review(runner.state) == expected
    supervisor = mock_supervisor(monkeypatch)
    policy = Mock()
    monkeypatch.setattr(loop, "choose", policy)
    count = runner.state["browser"].act.call_count
    runner.command("tick")
    assert supervisor.call_args.args[0]["trigger"] == expected
    policy.assert_not_called()
    assert runner.state["browser"].act.call_count == count


def test_decision_interval_catches_stale_or_skipped_actions(runner, monkeypatch):
    runner.state["decisions"] = [{}] * s.MAX_IDLE_DECISIONS
    supervisor = mock_supervisor(monkeypatch)
    runner.command("tick")
    assert supervisor.call_args.args[0]["trigger"] == "decision_interval"
    runner.state["browser"].act.assert_not_called()


def test_exhausted_local_recovery_escalates_without_erasing_failed_effect(runner, monkeypatch):
    runner.feedback("failed", "No effect")
    runner.state["recovery"]["effect"] = {"reason": "input_rejected", "expected": "Berlin"}
    runner.feedback("failed", "No effect", consume=True)
    runner.feedback("failed", "No effect", consume=True)
    supervisor = mock_supervisor(monkeypatch)
    runner.command("tick")
    assert supervisor.call_args.args[0]["trigger"] == "recovery_exhausted"
    assert runner.state["recovery"]["remaining"] == 2
    assert runner.state["recovery"]["effect"]["reason"] == "input_rejected"


def test_required_contact_stops_with_specific_question_and_no_input(runner, monkeypatch):
    def missing(context):
        output = advice(context, "NEEDS_INPUT")
        output.update(question="Which email address should be used to reveal the quote?",
                      missing_information=["Email address"], blockers=["Required contact dialog"])
        return output

    mock_supervisor(monkeypatch, missing)
    runner.state["review_pending"] = "modal_obstacle"
    result = runner.command("tick")
    assert result["status"] == "blocked" and result["stop_reason"]["code"] == "needs_input"
    assert "email address" in result["question"]
    assert result["task_state"]["missing_information"] == ["Email address"]
    runner.state["browser"].act.assert_not_called()


def test_done_is_always_reviewed_and_can_be_refused(runner, monkeypatch):
    supervisor = mock_supervisor(monkeypatch)
    result = act(runner, "DONE")
    assert supervisor.call_args.args[0]["proposed_terminal"] == "DONE"
    assert result["status"] == "ready" and result["outcome"]["independently_verified"] is False
    runner.state["browser"].act.assert_not_called()


def test_blocked_policy_escalates_before_terminating(runner, monkeypatch):
    supervisor = mock_supervisor(monkeypatch)
    assert act(runner, "BLOCKED")["status"] == "ready"
    assert supervisor.call_args.args[0]["trigger"] == "policy_blocked"


def test_missing_helper_value_escalates_on_next_cycle(runner, monkeypatch):
    error = model.TextHelperError("could not infer", {"model": "test"}, code="missing_field_value")
    monkeypatch.setattr(loop, "field_text", Mock(side_effect=error))
    act(runner)
    supervisor = mock_supervisor(monkeypatch)
    runner.command("tick")
    assert supervisor.call_args.args[0]["trigger"] == "missing_field_value"
    assert runner.state["task_state"]["failed_approaches"][0]["code"] == "text_helper_rejected"
    runner.state["browser"].act.assert_not_called()


def test_completion_is_model_report_with_grounded_current_evidence(runner, monkeypatch):
    def complete(context):
        output = advice(context, "COMPLETE")
        output["requirements"][0].update(status="supported", evidence=[
            {"source": context["current_observation"], "quote": "Search"},
        ])
        return s.validate_review(output, context)

    mock_supervisor(monkeypatch, complete)
    result = act(runner, "DONE")
    assert result["status"] == "done"
    assert result["outcome"] == {"assessment": "model_reported_complete", "independently_verified": False}


def test_page_change_during_review_invalidates_terminal_assessment(runner, monkeypatch):
    mock_supervisor(monkeypatch, lambda c: {**advice(c, "NEEDS_INPUT"),
                                           "question": "Email?", "missing_information": ["Email"]})
    other = page()
    other["text"] = "The dialog has now disappeared"
    other["fingerprint"] = fingerprint(other)
    runner.state["browser"].observe.return_value = other
    result = act(runner, "DONE")
    assert result["status"] == "ready" and result["review_pending"] == "page_changed_during_review"
    assert result.get("question") is None


def test_task_state_changes_invalidate_stale_text_cache(runner, monkeypatch):
    helper = Mock(return_value=("Berlin", {"model": "test", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("before mutation"), None]
    with pytest.raises(StalePage):
        act(runner)
    runner.state["task_state"]["objective"] = "Use a different field meaning"
    act(runner)
    assert helper.call_count == 2


def test_review_limits_stop_without_mutating(runner, monkeypatch):
    supervisor = mock_supervisor(monkeypatch)
    for _ in range(4):
        runner.review("obstacle")
    assert runner.state["stop_reason"]["code"] == "review_stalled"
    assert supervisor.call_count == 3
    runner.state["supervisor_calls"] = [{}] * s.MAX_REVIEWS
    runner.review("another")
    assert runner.state["stop_reason"]["code"] == "supervisor_budget"
    runner.state["browser"].act.assert_not_called()


@pytest.fixture
def context(runner):
    return s.review_context(runner.state, "initialize")


@pytest.mark.parametrize("bad", ["unknown_source", "invented_quote", "extra_command", "pending_complete",
                                 "no_evidence", "no_question", "missing_continue", "bad_type"])
def test_supervisor_rejects_ungrounded_or_invalid_outputs(context, bad):
    output = advice(context)
    row = output["requirements"][0]
    if bad in {"unknown_source", "invented_quote"}:
        row.update(status="supported", evidence=[{"source": "missing" if bad == "unknown_source" else "o1",
                                                  "quote": "Invented quote"}])
    elif bad == "extra_command":
        output["selector"] = "#submit"
    elif bad == "pending_complete":
        output["status"] = "COMPLETE"
    elif bad == "no_evidence":
        row["status"] = "supported"
    elif bad == "no_question":
        output.update(status="NEEDS_INPUT", missing_information=["Email"])
    elif bad == "missing_continue":
        output["missing_information"] = ["Email"]
    else:
        output["status"] = []
    with pytest.raises(s.SupervisorError):
        s.validate_review(output, context)


def test_existing_requirements_cannot_be_removed_or_rewritten(context):
    context["task_state"]["requirements"] = advice(context)["requirements"]
    output = advice(context)
    output["requirements"][0]["text"] = "A different task"
    with pytest.raises(s.SupervisorError, match="rewrote"):
        s.validate_review(output, context)


def test_completion_cannot_override_unresolved_effect(context):
    output = advice(context, "COMPLETE")
    output["requirements"][0].update(status="supported", evidence=[{"source": "o1", "quote": "Search"}])
    context["unresolved_recovery"] = {"effect": {"reason": "input_rejected"}}
    with pytest.raises(s.SupervisorError, match="unresolved"):
        s.validate_review(output, context)


def test_completion_cannot_rely_only_on_historical_evidence(context):
    output = advice(context, "COMPLETE")
    output["requirements"][0].update(status="supported", evidence=[{"source": "o1", "quote": "Search"}])
    context["current_observation"] = "o2"
    with pytest.raises(s.SupervisorError, match="current-page"):
        s.validate_review(output, context)


def test_changed_page_is_observed_before_supervisor_advice(runner, monkeypatch):
    runner.state["browser"].fresh.return_value = False
    current = page()
    current["text"] = "Contact required on new page"
    current["fingerprint"] = fingerprint(current)
    runner.state["browser"].observe.return_value = current
    supervisor = mock_supervisor(monkeypatch)
    runner.review("obstacle")
    assert supervisor.call_args.args[0]["observations"][-1]["data"]["text"] == current["text"]
    runner.state["browser"].act.assert_not_called()


def test_historical_evidence_survives_observation_window(runner):
    first = s.remember(runner.state)
    runner.state["task_state"]["requirements"] = [{"id": "r1", "text": "Original requirement",
        "status": "supported", "evidence": [{"source": first["id"], "quote": "Search"}]}]
    for i in range(6):
        runner.state["page"]["text"] = f"New content {i}"
        s.remember(runner.state)
    context = s.review_context(runner.state, "obstacle")
    assert len(context["observations"]) == 5
    assert context["observations"][0]["id"] == first["id"]
    assert context["task_state"]["requirements"][0]["text"] == "Original requirement"


def test_reasoning_configuration_is_separate_from_fast_helper(context, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("TEXT_MODEL", "deepseek/example-flash")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "none")
    monkeypatch.setenv("TEXT_MODEL_PROVIDER", "together")
    post = Mock(return_value={"provider": "Together", "usage": {"completion_tokens": 2200},
                              "choices": [{"finish_reason": "stop", "message": {
                                  "content": json.dumps(advice(context))}}]})
    monkeypatch.setattr(s, "post_json", post)
    output, info = s.supervise(context)
    body = post.call_args.args[2]
    assert body["model"] == "deepseek/example-flash"
    assert body["reasoning"] == {"enabled": True}
    assert body["max_tokens"] == 8192
    assert post.call_args.kwargs["timeout"] == 90
    assert body["provider"]["order"] == ["together"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert info["usage"]["completion_tokens"] == 2200
    assert output["status"] == "CONTINUE"


@pytest.mark.parametrize("failure", ["length", "json", "http"])
def test_supervisor_failure_is_traced_and_never_executes(runner, monkeypatch, failure):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    context = s.review_context(runner.state, "obstacle")
    post = Mock(return_value={"usage": {"completion_tokens": 8192}, "choices": [{
        "finish_reason": "length" if failure == "length" else "stop",
        "message": {"content": "bad json" if failure == "json" else json.dumps(advice(context))},
    }]})
    if failure == "http":
        post.side_effect = RuntimeError("Model provider returned HTTP 503; no action executed.")
    monkeypatch.setattr(s, "post_json", post)
    runner.review("obstacle")
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"]["code"] == "supervisor_error"
    assert "error" in runner.state["supervisor_calls"][-1]
    assert post.call_count == (2 if failure == "json" else 1)
    runner.state["browser"].act.assert_not_called()


def recorded_bad_response():
    return Path(__file__).with_name("fixtures").joinpath("supervisor-invalid-shape.json").read_text(encoding="utf-8")


def completion(content, tokens=100):
    return {"provider": "Together", "usage": {"completion_tokens": tokens, "cost": 0.001},
            "choices": [{"finish_reason": "stop", "message": {"content": content}}]}


def test_actual_rejected_response_is_regenerated_with_schema_and_original_goal(context, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    post = Mock(side_effect=[completion(recorded_bad_response()), completion(json.dumps(advice(context)), 200)])
    monkeypatch.setattr(s, "post_json", post)
    output, info = s.supervise(context)
    assert output["requirements"][0]["text"] == context["goal"]
    assert info["attempt_count"] == 2 and len(info["attempts"]) == 2
    assert info["usage"]["completion_tokens"] == 300
    assert info["usage"]["cost"] == 0.002
    assert "missing keys" in info["attempts"][0]["error"]
    assert "error" not in info["attempts"][1]
    second_body = post.call_args_list[1].args[2]
    second_input = json.loads(second_body["messages"][1]["content"])
    assert second_input["goal"] == context["goal"]
    assert "Regenerate" in second_input["response_repair"]["instruction"]
    assert second_input["response_repair"]["rejected_response"] == recorded_bad_response()
    assert second_body["response_format"]["json_schema"]["strict"] is True


def test_actual_schema_failure_exhausts_one_repair_without_browser_input(runner, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value=completion(recorded_bad_response()))
    monkeypatch.setattr(s, "post_json", post)
    runner.review("initialize")
    assert post.call_count == 2
    call = runner.state["supervisor_calls"][-1]
    assert call["attempt_count"] == 2 and all("error" in a for a in call["attempts"])
    assert call["response_content"] == recorded_bad_response()
    assert "status" in call["error"] and "next_action" in call["error"]
    assert runner.state["stop_reason"]["code"] == "supervisor_error"
    runner.state["browser"].act.assert_not_called()


@pytest.mark.parametrize("changed", ["Milan to Milan?", "120 x 120? cm"])
def test_schema_valid_output_cannot_change_user_requirement_values(context, changed):
    context["goal"] = "Calculate price from Milan to Berlin 1 pallet 120x80x140cms, 200kgs"
    output = advice(context)
    output["requirements"][0]["text"] = changed
    with pytest.raises(s.SupervisorError, match="verbatim excerpt"):
        s.validate_review(output, context)


def test_direct_deepseek_uses_documented_json_mode_with_the_same_schema_and_validation(context, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1")
    post = Mock(return_value=completion(json.dumps(advice(context))))
    monkeypatch.setattr(s, "post_json", post)
    output, info = s.supervise(context)
    body = post.call_args.args[2]
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled"}
    assert '"additionalProperties": false' in body["messages"][0]["content"]
    assert output["requirements"][0]["text"] == context["goal"]
    assert info["attempt_count"] == 1

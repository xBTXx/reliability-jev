"""Offline contracts for observed context and bounded recovery. No paid requests."""

from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
import test_agent as contracts
from test_agent import choice, decision, page

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import Browser, MutationUncertain, browser_operation


@pytest.fixture
def runner():
    return contracts.runner.__wrapped__()


def act(agent, selected="e1"):
    agent.state["decision"] = decision(selected)
    agent.state["status"] = "predicted"
    return agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})


def test_both_models_receive_observed_field_identity_and_context(monkeypatch):
    p = page()
    p["actions"][0]["context"] = {"name": "Arrival", "form": 7, "nearby_text": "Destination"}
    p["actions"].insert(2, {"id": "other", "node": 20, "kind": "fill", "role": "textbox",
                             "label": "City", "value": "Milan", "context": {"name": "Departure", "form": 7}})
    post = Mock(side_effect=lambda url, key, body: {
        "model": "test", "answers": {
            "review_gate": choice(["CONTINUE", "REVIEW"], "CONTINUE"),
            "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
            "type_text_target": choice(body["questions"]["type_text_target"]["criteria"], "1"),
        },
    })
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(p, "Travel from Milan to Berlin", [], [{"code": "same_value"}])
    assert result["request"]["state"]["elements"][0]["context"]["name"] == "Arrival"
    assert result["request"]["questions"]["type_text_target"]["criteria"]["1"]["context"]["name"] == "Arrival"
    assert result["request"]["state"]["recent_feedback"] == [{"code": "same_value"}]
    context = model.field_context("Travel from Milan to Berlin", p["actions"][0], p, [])
    assert context["field"]["node"] == 10
    assert context["field"]["context"]["name"] == "Arrival"
    assert context["form_fields"][1]["value"] == "Milan"


def test_helper_context_is_bounded_and_excludes_other_forms():
    p = page()
    p["actions"][0]["context"] = {"form": 1}
    for i in range(30):
        p["actions"].append({"id": str(i), "kind": "fill", "node": i + 100, "role": "textbox",
                             "label": "City", "context": {"form": 1}, "value": ""})
    p["actions"].append({"id": "outside", "kind": "fill", "node": 500, "role": "textbox",
                         "label": "Unrelated", "context": {"form": 2}, "value": ""})
    context = model.field_context("Travel", p["actions"][0], p, [])
    assert len(context["form_fields"]) == 24
    assert all(e["context"]["form"] == 1 for e in context["form_fields"])


@pytest.mark.parametrize("changed", ["name", "node", "document", "inplace_context"])
def test_helper_cache_is_invalidated_by_field_meaning_or_identity(runner, monkeypatch, changed):
    helper = Mock(return_value=("Berlin", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["page"]["actions"][0]["context"] = {"name": "Departure"}
    from jev_ultrafast.browser import StalePage
    runner.state["browser"].act.side_effect = [StalePage("changed before input"), None]
    with pytest.raises(StalePage):
        act(runner)
    p = runner.state["page"]
    if changed == "inplace_context":
        p["actions"][0]["context"]["name"] = "Arrival"
    elif changed == "name":
        p["actions"][0]["context"] = {"name": "Arrival"}
    elif changed == "node":
        p["actions"][0]["node"] = 999
    else:
        p["marker"] = ["new document"]
    act(runner)
    assert helper.call_count == 2


def test_scroll_suppression_distinguishes_positions():
    p = page()
    down = {"kind": "scroll", "label": "Scroll down", "delta": 560}
    first = loop.Agent.mutation_key(p, down, None)
    p["scroll"] = {"y": 560}
    assert first != loop.Agent.mutation_key(p, down, None)


def test_helper_sees_offscreen_related_fields_without_executable_targets():
    p = page()
    p["actions"][0]["context"] = {"form": 1}
    p["fields_outside_view"] = [{"node": 99, "value": "Milan", "position": "above",
                                 "context": {"form": 1, "name": "Departure"}}]
    context = model.field_context("Travel", p["actions"][0], p, [])
    assert context["form_fields"][1]["position"] == "above"
    assert 99 not in [e["node"] for e in model.action_space(p["actions"])[0]]


def test_stopped_run_elapsed_time_is_frozen(runner):
    runner.stop("test", "Stopped")
    elapsed = runner.state["elapsed_ms"]
    with patch("jev_ultrafast.agent.time.perf_counter", return_value=10**9):
        assert runner.snapshot()["elapsed_ms"] == elapsed


def test_rejected_changed_context_cannot_resurrect_old_cached_value(runner, monkeypatch):
    from jev_ultrafast.browser import StalePage
    info = {"model": "test", "latency_ms": 10}
    helper = Mock(side_effect=[("Berlin", info), model.TextHelperError("invalid JSON", info), ("Berlin", info)])
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("before input"), None]
    original = runner.state["page"]["text"]
    with pytest.raises(StalePage):
        act(runner)
    runner.state["page"]["text"] = "Changed context"
    act(runner)
    assert runner.pending_text is None
    runner.state["page"]["text"] = original
    act(runner)
    assert helper.call_count == 3


def test_same_value_is_not_executed_and_reassessment_is_bounded(runner, monkeypatch):
    runner.state["page"]["actions"][0]["value"] = "Berlin"
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("Berlin", {"model": "test", "latency_ms": 10})))
    for _ in range(3):
        act(runner)
    assert runner.state["status"] == "ready"
    assert runner.state["review_pending"] == "recovery_exhausted"
    assert runner.state["history"] == []
    assert len(runner.state["text_calls"]) == 3
    runner.state["browser"].act.assert_not_called()


def test_failed_command_is_suppressed_until_observed_state_changes(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("Berlin", {"model": "test", "latency_ms": 10})))
    p = deepcopy(runner.state["page"])
    p["after_action"] = {"status": "failed", "reason": "input_rejected", "field_node": 10,
                         "expected": "Berlin", "observed": ""}
    runner.state["browser"].observe.return_value = p
    act(runner)
    act(runner)
    assert runner.state["browser"].act.call_count == 1
    assert runner.state["events"][-1]["code"] == "suppressed_mutation"
    p["actions"][0]["value"] = "B"
    act(runner)
    assert runner.state["browser"].act.call_count == 2


def test_confirmed_form_effect_resets_recovery(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("Berlin", {"model": "test", "latency_ms": 10})))
    runner.feedback("autocomplete_timeout", "waited")
    p = deepcopy(runner.state["page"])
    p["after_action"] = {"status": "confirmed", "reason": "text_accepted", "observed": "Berlin"}
    runner.state["browser"].observe.return_value = p
    act(runner)
    assert runner.state["recovery"] is None
    assert runner.state["history"][-1]["effect"]["status"] == "confirmed"


def test_late_suggestions_resolve_recovery_during_read_only_wait(runner):
    p = runner.state["page"]
    p["actions"][0].update(role="combobox", value="Berlin")
    runner.feedback("autocomplete_timeout", "waited")
    runner.state["recovery"]["effect"] = {
        "reason": "autocomplete_timeout", "field_node": 10, "expected": "Berlin",
    }
    p["actions"].append({"id": "option", "node": 30, "kind": "click", "label": "Berlin", "owner_node": 10})
    act(runner, "wait")
    assert runner.state["recovery"] is None


def test_uncertain_mutation_is_never_replayed(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("Berlin", {"model": "test", "latency_ms": 10})))
    runner.state["browser"].act.side_effect = MutationUncertain("lost acknowledgement")
    act(runner)
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"]["code"] == "execution_uncertain"
    assert runner.state["history"] == []
    assert runner.state["events"][-1]["code"] == "execution_uncertain"
    runner.state["browser"].act.assert_called_once()


def test_post_action_observation_failure_stops_tick_without_replaying(runner, monkeypatch):
    from jev_ultrafast.browser import StalePage
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    runner.state["browser"].observe.side_effect = StalePage("document gone")
    runner.command("tick")
    assert runner.state["status"] == "blocked"
    assert len(runner.state["history"]) == 1
    runner.state["browser"].act.assert_called_once()


def test_done_with_unresolved_effect_requires_supervision(runner, monkeypatch):
    runner.feedback("input_rejected", "missing effect")
    review = Mock()
    monkeypatch.setattr(runner, "review", review)
    act(runner, "DONE")
    review.assert_called_once_with("proposed_completion", proposed="DONE")
    assert runner.state["recovery"] is not None


def test_late_confirmed_field_value_resolves_recovery_before_completion_review(runner, monkeypatch):
    runner.feedback("input_rejected", "missing effect")
    runner.state["recovery"]["effect"] = {"reason": "input_rejected", "field_node": 10, "expected": "Berlin"}
    runner.state["page"]["actions"][0]["value"] = "Berlin"
    review = Mock()
    monkeypatch.setattr(runner, "review", review)
    act(runner, "DONE")
    review.assert_called_once_with("proposed_completion", proposed="DONE")
    assert runner.state["recovery"] is None


def test_dispatch_error_is_classified_as_uncertain(monkeypatch):
    import jev_ultrafast.browser as browser
    cdp = Mock(side_effect=RuntimeError("connection lost"))
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(MutationUncertain):
        browser_operation({"operation": "act", "session": "test", "action": {
            "kind": "scroll", "id": "scroll_down", "delta": 560,
        }})
    assert cdp.call_count == 1


def test_verification_interruption_is_retained_on_observation():
    b = Browser.__new__(Browser)
    b.session = "test"
    b.after_input = {"kind": "fill"}
    b.call = Mock(return_value={"exceptionDetails": {"text": "navigation"}})
    import jev_ultrafast.browser as browser
    with patch.object(browser, "browser_operation", return_value=page()):
        p = b.observe(screenshot=False)
    assert p["after_action"]["status"] == "unknown"

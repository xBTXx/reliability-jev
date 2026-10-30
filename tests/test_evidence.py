"""Decoded citation matching and pending guidance must not depend on JSON escape syntax."""

import json
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
import test_agent as contracts
from test_supervision import advice

from jev_ultrafast import model
from jev_ultrafast import supervision as s


@pytest.fixture
def context():
    runner = contracts.runner.__wrapped__()
    context = s.review_context(runner.state, "gate:UNEXPECTED_OUTCOME")
    context["observations"][0]["data"]["text"] = 'Newest release\n\n29 Oct\nExample team'
    return context


@pytest.mark.parametrize("observed,quote", [
    ('Newest release\n29 Oct\nExample team', 'Newest release\n29 Oct\nExample team'),
    ('Newest release\n\n29 Oct\tExample team', 'Newest release\n29 Oct\nExample team'),
    ('Release "Example" at C:\\releases\\new', '"Example" at C:\\releases\\new'),
    ('Cafe\u0301\u00a0release', 'Café release'),
])
def test_citations_match_decoded_fields_and_equivalent_whitespace(observed, quote):
    assert s.citation_match(quote, {"text": observed}) is not None


@pytest.mark.parametrize("observed,quote", [
    ({"text": "29 Oct release"}, "release 29 Oct"),
    ({"text": "10 00 kg"}, "1000 kg"),
    ({"text": "100 kg"}, "200 kg"),
    ({"text": "Not complete"}, "Complete"),
    ({"title": "Newest release", "date": "29 Oct"}, "Newest release\n29 Oct"),
])
def test_matching_does_not_reorder_edit_values_or_join_observation_fields(observed, quote):
    assert s.citation_match(quote, observed) is None


def test_multiline_supported_evidence_remains_grounded_without_mutating_the_reply(context):
    output = advice(context, "COMPLETE")
    output["requirements"][0].update(status="supported", evidence=[
        {"source": "o1", "quote": "Newest release\n29 Oct\nExample team"},
    ])
    original, notes = deepcopy(output), []
    result = s.validate_review(output, context, notes=notes)
    assert result["status"] == "COMPLETE" and output == original
    assert notes[0]["code"] == "citation_whitespace_normalized"
    assert notes[0]["field"] == "data.text"


def test_unverified_pending_citation_is_omitted_without_promoting_the_requirement(context):
    output = advice(context)
    output["requirements"][0]["evidence"] = [{"source": "o1", "quote": "Invented release"}]
    original, notes = deepcopy(output), []
    result = s.validate_review(output, context, notes=notes)
    assert result["status"] == "CONTINUE"
    assert result["requirements"][0]["status"] == "pending"
    assert result["requirements"][0]["evidence"] == []
    assert notes[0]["code"] == "pending_citation_omitted" and output == original


@pytest.mark.parametrize("status", ["CONTINUE", "COMPLETE"])
def test_unsupported_completed_fact_is_still_rejected(context, status):
    output = advice(context, status)
    output["requirements"][0].update(status="supported", evidence=[{"source": "o1", "quote": "Invented release"}])
    with pytest.raises(s.SupervisorError, match="absent from observation o1"):
        s.validate_review(output, context)


def test_continuation_with_bad_pending_citation_uses_one_request_and_keeps_raw_evidence(context, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    output = advice(context)
    output["requirements"][0]["evidence"] = [{"source": "o1", "quote": "Invented release"}]
    raw = json.dumps(output)
    post = Mock(return_value={"choices": [{"finish_reason": "stop", "message": {"content": raw}}]})
    monkeypatch.setattr(s, "post_json", post)
    result, info = s.supervise(context)
    assert post.call_count == 1 and info["attempt_count"] == 1
    assert info["attempts"][0]["response_content"] == raw
    assert info["attempts"][0]["validation_notes"][0]["code"] == "pending_citation_omitted"
    assert result["requirements"][0]["evidence"] == []


def test_controller_continues_after_decoded_multiline_review_evidence(context, monkeypatch):
    runner = contracts.runner.__wrapped__()
    output = advice(context)
    output["requirements"][0]["evidence"] = [{"source": "o1", "quote": "Newest release\n29 Oct\nExample team"}]
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model.CLIENT, "post", Mock(return_value=httpx.Response(200, json={
        "usage": {"cost": 0.001}, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}],
    })))
    # Use the actual review input produced by the controller, with the same displayed text.
    runner.state["page"]["text"] = context["observations"][0]["data"]["text"]
    runner.review("gate:UNEXPECTED_OUTCOME")
    assert runner.state["status"] == "ready"
    assert runner.state["task_state"]["requirements"][0]["status"] == "pending"
    assert runner.state["review_authorization"] is not None
    runner.state["browser"].act.assert_not_called()

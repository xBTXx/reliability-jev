"""TypeSafe chooses operations, targets and reviews; a text model writes field values."""

import json
import math
import os
import re
import time
from copy import deepcopy

import httpx

from .costs import record_call
from .progress import report_progress, request_progress
from .questions import NEXT_ACTION, REVIEW_GATE, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)


class ModelProviderError(RuntimeError):
    """A provider rejection with bounded, redacted diagnostics, never raw request headers."""

    def __init__(self, status, details):
        self.info = {"http_status": status, "provider_error": details}
        message = details.get("message", "No provider explanation was returned.")
        prefix = f"Model provider returned HTTP {status}" if status is not None else "Model request failed"
        super().__init__(f"{prefix}: {message}; no action executed.")


def provider_error(result, key):
    def clean(value):
        value = str(value).replace(key, "[redacted]") if key else str(value)
        value = re.sub(r"(?i)Bearer\s+\S+|\bsk-[\w-]+", "[redacted]", value)
        return value[:800]

    error = result.get("error", {}) if isinstance(result, dict) else {}
    error = error if isinstance(error, dict) else {"message": error}
    details = {k: clean(error[k]) for k in ("message", "code", "type", "param") if k in error}
    metadata = error.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("provider_name"):
            details["provider"] = clean(metadata["provider_name"])
        raw = metadata.get("raw")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None  # Do not copy arbitrary raw bodies into diagnostics.
        if isinstance(raw, dict):
            upstream = raw.get("error", raw)
            if isinstance(upstream, dict) and upstream.get("message"):
                details["upstream_message"] = clean(upstream["message"])
                details["message"] = details["upstream_message"]
    return details


def reasoning_options(base, model, requested):
    """Keep reasoning controls explicit, with the documented always-on GPT-OSS exception."""
    requested = requested.lower().strip()
    modes = {"none", "default", "enabled", "minimal", "low", "medium", "high", "xhigh", "max"}
    if requested not in modes:
        raise ValueError(f"Unsupported reasoning mode: {requested}")
    mandatory = model.rsplit("/", 1)[-1].split(":")[0] in {"gpt-oss-120b", "gpt-oss-20b"}
    applied = "low" if mandatory and requested == "none" else requested
    if applied == "default":
        options = {}
    elif "api.deepseek.com/" in base:
        options = {"thinking": {"type": "disabled" if applied == "none" else "enabled"}}
    elif "api.cerebras.ai/" in base:
        options = {} if applied == "enabled" else {"reasoning_effort": applied}
    else:
        options = {"reasoning": {"enabled": applied == "enabled"}} if applied in {"none", "enabled"} else {
            "reasoning": {"effort": applied},
        }
    return options, {"requested": requested, "applied": applied,
                     "adjustment": "GPT-OSS reasoning is mandatory; using low instead of none."
                     if applied != requested else None}


class TextHelperError(ValueError):
    """A rejected model response, with evidence for the run's text-call log."""

    def __init__(self, reason, info, *, code="text_helper_rejected"):
        super().__init__(f"Text helper {reason}; nothing typed.")
        self.info = {**info, "code": code, "error": str(self)}


def post_json(url, key, body, *, timeout=None):
    for attempt in range(3):
        receipt = record_call(body.get("model"), error="pending")
        role = receipt["role"] if receipt else "model"
        try:
            options = {"timeout": timeout} if timeout is not None else {}
            with request_progress(role=role, model=body.get("model"), attempt=attempt + 1,
                                  timeout=timeout or 25) as activity:
                response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, **options)
                activity["status"] = f"HTTP {response.status_code}"
        except httpx.HTTPError:
            record_call(body.get("model"), error="connection_failed", record=receipt)
            raise ModelProviderError(None, {
                "message": "Model connection failed", "code": "connection_failed",
            }) from None
        except (KeyboardInterrupt, SystemExit):
            record_call(body.get("model"), error="interrupted", record=receipt)
            raise
        try:
            result = response.json()
            invalid_json = False
        except ValueError:
            result, invalid_json = None, True
        record_call(body.get("model"), result=result, http_status=response.status_code,
                    error="http_error" if response.is_error else "invalid_json" if invalid_json else None,
                    error_details=provider_error(result, key) if response.is_error else None, record=receipt)
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise ModelProviderError(response.status_code, provider_error(result, key))
        if invalid_json:
            raise RuntimeError("Model provider returned invalid JSON; no action executed.")
        return result
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in (
                "role", "value", "checked", "selected", "expanded", "context", "owner_node", "owner_source",
                "dialog_node"
            ) if k in action}
            element["node"] = node
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def choose(state, goal, history, feedback=None, task_state=None, *, review_authorization=None):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}},
        "review_gate": {"type": "choice", "criteria": {
            "CONTINUE": "Observed trajectory supports progress toward the current objective.",
            "REVIEW": "The objective or approach needs reasoning-based reassessment.",
        }, "instructions": {"goal": goal, "rules": REVIEW_GATE}},
        "review_reason": {"type": "choice", "criteria": {
            "ON_TRACK": "Progress is supported by observations.",
            "OBSTACLE": "A dialog or other obstacle blocks the objective.",
            "REPETITION": "Repeated states or actions without meaningful progress.",
            "MISSING_INFORMATION": "A required value is absent from the goal.",
            "UNEXPECTED_OUTCOME": "Observed effects differ from the intended result.",
            "REFINE_OBJECTIVE": "The current objective needs refinement.",
        }, "instructions": {"goal": goal, "rules": REVIEW_GATE}},
    }
    if review_authorization:
        # The supervisor has already reviewed this exact page. Give its guidance one fresh attempt.
        questions["review_gate"]["criteria"] = {
            "CONTINUE": "The supervisor approved this exact observation. Try its guidance once with observed controls.",
        }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded", "context", "owner_node") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "scroll": state["scroll"],
            "fields_outside_view": state.get("fields_outside_view", []),
            "dialogs": state.get("dialogs", []),
            "active_dialog": state.get("active_dialog"),
            "task_state": deepcopy(task_state),
            "review_authorization": review_authorization,
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "node", "text", "page_changed", "effect")}
                for h in history[-10:]
            ],
            "recent_feedback": (feedback or [])[-6:],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    gate = validate_choice(result["answers"].get("review_gate", {}), questions["review_gate"]["criteria"])
    reason = None
    if gate["choice"] == "REVIEW":
        reason = validate_choice(result["answers"].get("review_reason", {}), questions["review_reason"]["criteria"])
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets and gate["choice"] == "CONTINUE":
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    elif operation not in targets:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    else:
        choice = None  # All speculative targets are discarded when reassessment is selected.
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "review_gate": gate["choice"],
        "review_reason": reason["choice"] if reason else None,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history, task_state=None):
    elements = action_space(page["actions"])[0]
    field = next(e for e in elements if e["node"] == action["node"])
    form = action.get("context", {}).get("form")
    related = [e for e in elements if "TYPE_TEXT" in e["operations"] and
               (form is None or e.get("context", {}).get("form") == form)][:24]
    related += [e for e in page.get("fields_outside_view", []) if
                form is None or e.get("context", {}).get("form") == form][:24-len(related)]
    return deepcopy({
        "goal": goal,
        "field": field,
        "document": page.get("marker", [page["url"]])[0],
        "form_fields": related,
        "page": {"url": page["url"], "title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "node", "text", "effect")} for h in history[-6:]],
        "task_state": task_state,
        "active_dialog": page.get("active_dialog"),
    })


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    requested = os.environ.get("TEXT_MODEL_REASONING", "none" if "api.deepseek.com/" in base else "low")
    reasoning, reasoning_info = reasoning_options(base, model, requested)
    if reasoning_info["adjustment"]:
        report_progress(role="text_helper", phase="configuration", message=reasoning_info["adjustment"])
    try:
        budget = int(os.environ.get("TEXT_MODEL_MAX_TOKENS", "2048" if reasoning_info["applied"] != "none" else "1024"))
        if not 256 <= budget <= 32768:
            raise ValueError()
    except ValueError:
        raise ValueError("TEXT_MODEL_MAX_TOKENS must be between 256 and 32768") from None
    routing = {}
    if provider := os.environ.get("TEXT_MODEL_PROVIDER", "").strip():
        if "openrouter.ai/" not in base:
            raise ValueError("TEXT_MODEL_PROVIDER requires an OpenRouter TEXT_MODEL_BASE_URL.")
        routing = {"provider": {"order": [provider], "allow_fallbacks": False, "require_parameters": True}}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": budget,
            "response_format": {"type": "json_object"},
            **reasoning,
            **routing,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    info = {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
        "provider": result.get("provider"),
        "reasoning": reasoning_info,
    }
    try:
        choice = result["choices"][0]
        content = choice["message"]["content"]
        info["finish_reason"] = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise TextHelperError("response is missing a completion message", info) from None

    def reject(reason, **kwargs):
        raise TextHelperError(reason, {**info, "response_content": content}, **kwargs)

    if info["finish_reason"] == "length":
        reject("hit the output token limit before finishing")
    try:
        output = json.loads(content)
    except (ValueError, TypeError):
        reject("returned invalid JSON (see text_calls in the saved trace)")
    if not isinstance(output, dict) or set(output) != {"text"}:
        reject('must return exactly {"text": "value"}')
    value = output["text"]
    if value is None:
        reject("could not infer a value for the selected field from the task", code="missing_field_value")
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        reject("returned an empty, non-string, or oversized field value")
    return value, info

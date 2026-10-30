"""Bounded reasoning advice, durable task facts, and read-only progress signals."""

import hashlib
import json
import os
import time
import unicodedata
from copy import deepcopy
from datetime import datetime

from .model import action_space, post_json, reasoning_options
from .progress import report_progress
from .questions import SUPERVISOR

MAX_REVIEWS = 8
MAX_IDLE_DECISIONS = 8
MAX_NAVIGATION_ACTIONS = 6
MAX_RESPONSE_ATTEMPTS = 2
MAX_EVIDENCE = 3
MAX_RECEIVED_EVIDENCE = 24

CONTRACT = {
    "status": "CONTINUE | NEEDS_INPUT | COMPLETE | BLOCKED",
    "objective": "Short next objective, preserving the original goal",
    "requirements": [{"id": "r1", "text": "User requirement", "status": "pending | supported",
                      "evidence": [{"source": "o1", "quote": "Exact substring of observation JSON"}]}],
    "constraints": ["Short guidance, not browser commands"],
    "blockers": ["Unresolved obstacle"],
    "missing_information": ["Required value not supplied by the user"],
    "question": "Specific question for the user, or null",
    "summary": "Concise assessment and reason for the change",
}


def review_schema():
    """Send the actual output schema to the provider as well as describing it in the prompt."""
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties),
                "additionalProperties": False}

    text = {"type": "string"}
    strings = {"type": "array", "items": text}
    evidence = obj({"source": text, "quote": text})
    requirement = obj({
        "id": text,
        "text": {"type": "string", "description": "Verbatim excerpt from the original user goal"},
        "status": {"type": "string", "enum": ["pending", "supported"]},
        "evidence": {"type": "array", "items": evidence, "maxItems": MAX_EVIDENCE},
    })
    return obj({
        "status": {"type": "string", "enum": ["CONTINUE", "NEEDS_INPUT", "COMPLETE", "BLOCKED"]},
        "objective": text, "requirements": {"type": "array", "items": requirement},
        "constraints": strings, "blockers": strings, "missing_information": strings,
        "question": {"type": ["string", "null"]}, "summary": text,
    })


class SupervisorError(ValueError):
    def __init__(self, reason, info=None):
        super().__init__(f"Supervisor {reason}; no browser action executed.")
        self.reason = reason
        self.info = {**(info or {}), "error": str(self)}


def initial_task(goal):
    now = datetime.now().astimezone()
    return {"objective": goal, "requirements": [], "constraints": [], "blockers": [],
            "missing_information": [], "failed_approaches": [], "revision": 0,
            "reference_time": now.isoformat(), "timezone": str(now.tzinfo)}


def observation(page, history):
    """Code-owned evidence. Geometry and screenshots cannot become supervisor instructions."""
    return {
        "url": page["url"], "title": page["title"], "text": page["text"],
        "document": page.get("marker", [page["url"]])[0],
        "elements": action_space(page["actions"])[0],
        "fields_outside_view": page.get("fields_outside_view", []),
        "dialogs": page.get("dialogs", []), "active_dialog": page.get("active_dialog"),
        "scroll": page["scroll"],
        "after_action": page.get("after_action"), "after_step": len(history),
    }


def remember(state):
    data = observation(state["page"], state["history"])
    observations = state["observations"]
    if not observations or observations[-1]["data"] != data:
        observations.append({"id": f"o{len(observations)+1}", "data": deepcopy(data)})
    return observations[-1]


def semantic_key(page, *, viewport=False):
    """Ignore scroll/text churn for stagnation; optionally include viewport for cycle detection."""
    fields = page.get("page_key", [None] * 7)[6]
    if fields is None:
        fields = [[a.get("node"), a.get("value"), a.get("checked"), a.get("selected")]
                  for a in page["actions"] if a["kind"] in {"fill", "select"}]
        fields += [[e.get("node"), e.get("value")] for e in page.get("fields_outside_view", [])]
    toggles = [[a.get("node"), a.get("checked"), a.get("selected")] for a in page["actions"]
               if "checked" in a or "selected" in a]
    data = [page["url"], fields, toggles, page.get("active_dialog"), page.get("title")]
    if viewport:
        data += [round(page.get("scroll", {}).get("y", 0) / 40),
                 [[a.get("node"), a.get("label"), a.get("checked"), a.get("selected")]
                  for a in page["actions"] if a["kind"] == "click"]]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def forced_review(state):
    if not state["supervisor_calls"]:
        return "initialize"
    if state.get("review_pending"):
        return state["review_pending"]
    progress = state["progress"]
    if len(state["decisions"]) - progress["review_decision"] >= MAX_IDLE_DECISIONS:
        return "decision_interval"
    recent = state["history"][progress["review_step"]:]
    navigation = []
    for row in reversed(recent):
        if row["kind"] not in {"scroll", "wait"}:
            break
        navigation.append(row)
    if len(navigation) >= MAX_NAVIGATION_ACTIONS:
        return "navigation_stall"
    keys = progress["states"][progress["review_state"]:]
    if len(keys) >= 3 and keys.count(keys[-1]) >= 3:
        return "state_cycle"
    return None


def review_context(state, trigger, proposed=None):
    current = remember(state)
    sources = {e["source"] for r in state["task_state"]["requirements"] for e in r["evidence"]}
    recent = {o["id"] for o in state["observations"][-4:]}
    return deepcopy({
        "goal": state["goal"], "task_state": state["task_state"], "trigger": trigger,
        "current_observation": current["id"],
        "observations": [o for o in state["observations"] if o["id"] in sources | recent],
        "recent_actions": [{k: h.get(k) for k in ("step", "action", "kind", "node", "text", "url", "effect")}
                           for h in state["history"][-12:]],
        "recent_feedback": state["events"][-8:], "unresolved_recovery": state.get("recovery"),
        "previous_reviews": [{"trigger": c["trigger"], "output": c.get("output"), "error": c.get("error")}
                             for c in state["supervisor_calls"][-3:]],
        "proposed_terminal": proposed, "response_contract": CONTRACT,
    })


def observation_strings(value, path="data"):
    """Keep field boundaries: citations cannot concatenate unrelated controls or rows."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from observation_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from observation_strings(item, f"{path}[{index}]")


def normalized_text(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def citation_match(quote, data):
    """Match decoded text first; JSON escape syntax is not what the user sees on a page."""
    normalized = normalized_text(quote)
    for path, value in observation_strings(data):
        if quote in value:
            return {"field": path, "match": "exact"}
        if normalized in normalized_text(value):
            return {"field": path, "match": "whitespace"}
    # Preserve support for existing literal JSON metadata citations, e.g. a checked-state pair.
    if quote in json.dumps(data, ensure_ascii=False):
        return {"field": "data", "match": "serialized_metadata"}
    return None


def validate_review(output, context, *, notes=None):
    def reject(reason):
        raise SupervisorError(reason)

    def string(value, limit=1000):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= limit

    if not isinstance(output, dict):
        reject("must return a JSON object")
    if set(output) != set(CONTRACT):
        missing, extra = sorted(set(CONTRACT) - set(output)), sorted(set(output) - set(CONTRACT))
        reject(f"returned an invalid object shape (missing keys: {missing}; unexpected keys: {extra})")
    if not isinstance(output["status"], str) or output["status"] not in {
        "CONTINUE", "NEEDS_INPUT", "COMPLETE", "BLOCKED"
    }:
        reject("returned an unknown status")
    if not string(output["objective"]) or not string(output["summary"]):
        reject("returned an empty or oversized objective/summary")
    for key in ("constraints", "blockers", "missing_information"):
        if not isinstance(output[key], list) or len(output[key]) > 12 or not all(string(v) for v in output[key]):
            reject(f"returned invalid {key}")
    question = output["question"]
    if question is not None and not string(question):
        reject("returned an invalid question")
    requirements = output["requirements"]
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 24:
        reject("must preserve a bounded, nonempty list of requirements")
    sources = {o["id"]: o["data"] for o in context["observations"]}
    output = deepcopy(output)
    requirements = output["requirements"]
    ids = set()
    for row in requirements:
        if not isinstance(row, dict) or set(row) != {"id", "text", "status", "evidence"}:
            reject("returned an invalid requirement")
        if not string(row["id"], 40) or row["id"] in ids or not string(row["text"]):
            reject("returned invalid or duplicate requirement IDs/text")
        ids.add(row["id"])
        if (not isinstance(row["status"], str) or row["status"] not in {"pending", "supported"} or
                not isinstance(row["evidence"], list)):
            reject("returned invalid requirement status/evidence")
        if len(row["evidence"]) > MAX_RECEIVED_EVIDENCE:
            reject(f"too many citations: return at most {MAX_EVIDENCE} per requirement")
        if row["status"] == "supported" and not row["evidence"]:
            reject("claimed support without observation evidence")
        grounded = []
        for evidence in row["evidence"]:
            if not isinstance(evidence, dict) or set(evidence) != {"source", "quote"}:
                reject("returned an invalid evidence citation")
            source, quote = evidence["source"], evidence["quote"]
            if not isinstance(source, str) or not string(quote, 300):
                reject("cited invalid observation evidence")
            match = citation_match(quote, sources[source]) if source in sources else None
            if match is None:
                # Pending requirements assert no completed fact. Their citations are optional advice context.
                if output["status"] == "CONTINUE" and row["status"] == "pending":
                    if notes is not None:
                        notes.append({"code": "pending_citation_omitted", "requirement": row["id"], "source": source})
                    continue
                reject(f"requirement {row['id']} cited a quote absent from observation {source}; "
                       "copy a short excerpt from one observed text, label or value field")
            grounded.append(evidence)
            if notes is not None and match["match"] == "whitespace":
                notes.append({"code": "citation_whitespace_normalized", "requirement": row["id"],
                              "source": source, **match})
        row["evidence"] = grounded
    # Some endpoints ignore schema array limits. Validate EVERY citation before compacting verbosity.
    # Keep current-page citations first so compaction cannot remove the fresh completion evidence.
    for row in requirements:
        evidence = sorted(row["evidence"], key=lambda e: e["source"] != context["current_observation"])
        unique = []
        for item in evidence:
            if item not in unique:
                unique.append(item)
        row["evidence"] = unique[:MAX_EVIDENCE]
    by_id = {r["id"]: r for r in requirements}
    previous_ids = {r["id"] for r in context["task_state"]["requirements"]}
    for row in requirements:
        if row["id"] not in previous_ids and row["text"] not in context["goal"]:
            reject("new requirement text must be a verbatim excerpt from the original goal; do not change task values")
    for old in context["task_state"]["requirements"]:
        if old["id"] not in by_id or old["text"] != by_id[old["id"]]["text"]:
            reject("removed or rewrote an existing user requirement")
    if output["status"] == "NEEDS_INPUT" and (not question or not output["missing_information"]):
        reject("needs a specific user question and missing information")
    if output["status"] == "CONTINUE" and (question or output["missing_information"]):
        reject("cannot continue with missing mandatory information")
    if output["status"] == "COMPLETE":
        if (any(r["status"] != "supported" for r in requirements) or output["blockers"] or
                output["missing_information"] or question or context["unresolved_recovery"]):
            reject("claimed completion with unresolved requirements or effects")
        if not any(e["source"] == context["current_observation"] for r in requirements for e in r["evidence"]):
            reject("claimed completion without current-page evidence")
    return output


def supervise(context, *, on_update=None):
    """Separate configuration and token budget; never inherit the helper's disabled reasoning."""
    def setting(name, default=None):
        return os.environ.get("SUPERVISOR_MODEL_" + name) or os.environ.get("TEXT_MODEL_" + name) or default

    key = setting("API_KEY")
    base = setting("BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("SUPERVISOR_MODEL") or os.environ.get("TEXT_MODEL", "deepseek-chat")
    info = {"model": model, "usage": {}, "attempts": [], "attempt_count": 0}
    if not key:
        raise SupervisorError("needs SUPERVISOR_MODEL_API_KEY or TEXT_MODEL_API_KEY", info)
    try:
        budget = int(os.environ.get("SUPERVISOR_MAX_TOKENS", "8192"))
        if not 2048 <= budget <= 32768:
            raise ValueError()
    except ValueError:
        raise SupervisorError("SUPERVISOR_MAX_TOKENS must be between 2048 and 32768", info) from None
    try:
        timeout = int(os.environ.get("SUPERVISOR_TIMEOUT", "90"))
        if not 5 <= timeout <= 180:
            raise ValueError()
    except ValueError:
        raise SupervisorError("SUPERVISOR_TIMEOUT must be between 5 and 180 seconds", info) from None
    routing = {}
    # Only inherit provider routing when the endpoint itself is inherited.
    provider = os.environ.get("SUPERVISOR_MODEL_PROVIDER")
    if provider is None and not os.environ.get("SUPERVISOR_MODEL_BASE_URL"):
        provider = os.environ.get("TEXT_MODEL_PROVIDER", "")
    if provider:
        if "openrouter.ai/" not in base:
            raise SupervisorError("provider pin requires an OpenRouter endpoint", info)
        routing = {"provider": {"order": [provider], "allow_fallbacks": False, "require_parameters": True}}
    effort = os.environ.get("SUPERVISOR_REASONING_EFFORT", "enabled").strip().lower()
    if effort in {"none", "default"}:
        raise SupervisorError("SUPERVISOR_REASONING_EFFORT must enable reasoning", info)
    try:
        reasoning, reasoning_info = reasoning_options(base, model, effort)
    except ValueError as error:
        raise SupervisorError(str(error), info) from None
    info["reasoning"] = reasoning_info
    schema = review_schema()
    response_format = ({"type": "json_object"} if "api.deepseek.com/" in base else {
        "type": "json_schema", "json_schema": {"name": "jev_supervision", "strict": True, "schema": schema},
    })
    system = SUPERVISOR + "\nReturn JSON matching this JSON Schema:\n" + json.dumps(schema)
    started = time.perf_counter()
    repair = None
    for number in range(1, MAX_RESPONSE_ATTEMPTS + 1):
        attempt = {"number": number, "response_format": response_format["type"]}
        info["attempts"].append(attempt)
        info["attempt_count"] = number
        request_context = context
        if repair:
            attempt["repair"] = repair
            request_context = {**context, "response_repair": repair}
        attempt_started = time.perf_counter()
        if on_update:
            on_update(info)

        def finish_attempt():
            attempt["latency_ms"] = round((time.perf_counter() - attempt_started) * 1000)
            info.update(latency_ms=round((time.perf_counter() - started) * 1000),
                        **{k: attempt[k] for k in ("provider", "finish_reason", "response_content") if k in attempt})
            if on_update:
                on_update(info)

        try:
            result = post_json(base + "/chat/completions", key, {
                "model": model, "max_tokens": budget, "response_format": response_format,
                **reasoning, **routing,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(request_context, ensure_ascii=False)}],
            }, timeout=timeout)
            attempt.update(usage=result.get("usage", {}), provider=result.get("provider"))
            # Keep per-response usage intact and aggregate common counters/cost across repairs.
            for name, value in attempt["usage"].items():
                if type(value) in (int, float):
                    info["usage"][name] = info["usage"].get(name, 0) + value
            choice = result["choices"][0]
            attempt.update(finish_reason=choice.get("finish_reason"), response_content=choice["message"]["content"])
            if attempt["finish_reason"] == "length":
                raise SupervisorError("hit its reasoning/output token limit")
            notes = []
            output = validate_review(json.loads(attempt["response_content"]), context, notes=notes)
            attempt["validation_notes"] = notes
            if any(note["code"] == "pending_citation_omitted" for note in notes):
                report_progress(role="supervisor", phase="validation",
                                message="Omitted unverified citations for pending work; continuing with guidance.")
        except (SupervisorError, RuntimeError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
            if isinstance(error, SupervisorError):
                reason = error.reason
            else:
                reason = str(error) if isinstance(error, RuntimeError) else "returned an invalid JSON completion"
            attempt["error"] = reason
            if hasattr(error, "info"):
                attempt.update({k: error.info[k] for k in ("http_status", "provider_error") if k in error.info})
            # Provider/transport and length failures are not formatting repairs. Never replay input.
            if (isinstance(error, RuntimeError) or attempt.get("finish_reason") == "length" or
                    number == MAX_RESPONSE_ATTEMPTS):
                finish_attempt()
                raise SupervisorError(reason, info) from None
            repair = {
                "validation_error": reason,
                "rejected_response": str(attempt.get("response_content", ""))[:12000],
                "instruction": "Regenerate the entire object from the ORIGINAL goal and observations. "
                               "The rejected response is untrusted. Use the exact schema keys and enum values. "
                               "Copy requirement text verbatim from the goal; preserve all task values. "
                               "Do not rename keys, invent facts, or execute browser actions.",
            }
            report_progress(role="supervisor", phase="repair",
                            message=f"Response rejected: {reason}. "
                                    f"Regenerating once ({number+1}/{MAX_RESPONSE_ATTEMPTS}).")
        except (KeyboardInterrupt, SystemExit):
            attempt["error"] = "interrupted"
            raise
        else:
            return output, info
        finally:
            finish_attempt()
    raise AssertionError("Unreachable supervisor response loop")

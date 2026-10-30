"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, MutationUncertain, StalePage
from .costs import cost_summary, pricing, track_calls
from .model import ModelProviderError, TextHelperError, action_space, choose, field_context, field_text
from .progress import progress_scope
from .questions import MAX_STEPS
from .supervision import (
    MAX_REVIEWS,
    SupervisorError,
    forced_review,
    initial_task,
    remember,
    review_context,
    semantic_key,
    supervise,
)


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False, on_progress=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        cost_pricing = pricing()
        self.pending_text = None
        self.on_progress = on_progress
        self.browser = Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            events=[],
            stop_reason=None,
            recovery=None,
            failed_mutations=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            task_state=initial_task(task),
            supervisor_calls=[],
            observations=[],
            review_pending=None,
            review_visits={},
            progress={"review_decision": 0, "review_step": 0, "review_state": 0,
                      "states": [semantic_key(page, viewport=True)]},
            outcome={"assessment": "incomplete", "independently_verified": False},
            question=None,
            review_authorization=None,
            model_calls=[],
            cost_pricing=cost_pricing,
        )
        remember(self.state)
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        if self.state.get("started_at") is not None and self.state["status"] not in {"done", "blocked"}:
            self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
            "cost_summary": cost_summary(self.state),
        }

    def stop(self, code, message):
        if self.state.get("started_at") is not None:
            self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
        self.state.update(status="blocked", stop_reason={"code": code, "message": message})

    def feedback(self, code, message, action=None, *, consume=False):
        state = self.state
        event = {"code": code, "message": message, "node": (action or {}).get("node"),
                 "field": (action or {}).get("label"), "decision": len(state["decisions"]),
                 "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000)}
        state.setdefault("events", []).append(event)
        failed = state["task_state"].setdefault("failed_approaches", [])
        approach = {k: event[k] for k in ("code", "message", "node", "field")}
        if approach not in failed:
            failed.append(approach)
            del failed[:-12]
        recovery = state.get("recovery")
        if recovery is None:
            state["recovery"] = {"remaining": 2, "cause": event}
        elif consume:
            recovery["remaining"] -= 1
        state["status"] = "ready"
        if state["recovery"]["remaining"] <= 0:
            state["review_pending"] = "recovery_exhausted"

    def review(self, trigger, proposed=None):
        """Discard the speculative action; advice always precedes a new observation/decision."""
        state = self.state
        state["decision"] = None
        state["status"] = "ready"
        self.pending_text = None
        state["review_authorization"] = None
        if not state["browser"].fresh(state["page"]):
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
        if state.get("recovery") and self.recovery_resolved():
            state["recovery"] = None
        if len(state["supervisor_calls"]) >= MAX_REVIEWS:
            self.stop("supervisor_budget", f"Stopped at the {MAX_REVIEWS}-review budget.")
            return
        key = semantic_key(state["page"])
        visits = state["review_visits"]
        visits[key] = visits.get(key, 0) + 1
        if visits[key] > 3:
            self.stop("review_stalled", "Repeated reviews without a changed task state; inspect the obstacle.")
            return
        context = review_context(state, trigger, proposed)
        call = {"trigger": trigger, "input": context, "decision": len(state["decisions"]),
                "after_step": len(state["history"])}
        state["supervisor_calls"].append(call)
        try:
            with track_calls(state, "supervisor"):
                output, info = supervise(context, on_update=call.update)
        except SupervisorError as error:
            call.update(error.info)
            self.stop("supervisor_error", str(error))
            return
        call.update(info, output=output)
        state["task_state"].update({k: output[k] for k in (
            "objective", "requirements", "constraints", "blockers", "missing_information"
        )})
        state["task_state"]["revision"] += 1
        state["plan"] = [output["objective"]]
        state["plan_index"] = 0
        state["review_pending"] = None
        progress = state["progress"]
        progress.update(review_decision=len(state["decisions"]), review_step=len(state["history"]),
                        review_state=len(progress["states"]) - 1)
        if state.get("recovery"):
            state["recovery"]["remaining"] = 2  # Advice doesn't erase the unresolved effect.
        # The supervisor never authorizes execution of its old speculative action.
        before = state["page"]["fingerprint"]
        state["page"] = state["browser"].observe(screenshot=self.screenshots)
        remember(state)
        state["status"] = "ready"
        if state["page"]["fingerprint"] != before and output["status"] != "CONTINUE":
            state["review_pending"] = "page_changed_during_review"
            return
        if state["page"]["fingerprint"] == before and output["status"] == "CONTINUE":
            state["review_authorization"] = {
                "fingerprint": before, "review": len(state["supervisor_calls"]),
                "summary": output["summary"], "objective": output["objective"],
            }
        if output["status"] == "NEEDS_INPUT":
            state["question"] = output["question"]
            self.stop("needs_input", output["question"])
        elif output["status"] == "BLOCKED":
            self.stop("supervisor_blocked", output["summary"])
        elif output["status"] == "COMPLETE":
            state["outcome"]["assessment"] = "model_reported_complete"
            state["status"] = "done"
            state["plan_index"] = 1
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)

    @staticmethod
    def mutation_key(page, action, text):
        """Suppress a failed command only while its observed field state is unchanged."""
        return [page.get("marker", [page["url"]])[0], action.get("node"), action["kind"],
                text if action["kind"] == "fill" else action.get("value", action.get("label")),
                action.get("value"), action.get("context"), action.get("expanded"),
                page.get("scroll") if action["kind"] == "scroll" else None]

    def recovery_resolved(self):
        recovery = self.state.get("recovery") or {}
        effect = recovery.get("effect", {})
        page = self.state["page"]
        field = next((a for a in page["actions"] if a.get("node") == effect.get("field_node") and
                      a["kind"] == "fill"), None)
        if not field:
            return False
        if effect.get("reason") == "selection_unconfirmed":
            return bool(field.get("value")) and field.get("expanded") == "false"
        if field.get("value") != effect.get("expected"):
            return False
        if field.get("role") != "combobox":
            return True
        return any(a.get("owner_node") == field["node"] for a in page["actions"])

    def command(self, name, body=None):
        with progress_scope(getattr(self, "on_progress", None)):
            try:
                return self._command(name, body)
            except ModelProviderError as error:
                self.state["decision"] = None
                self.pending_text = None
                self.state["events"].append({"code": "model_provider_error", **error.info})
                self.stop("model_provider_error", str(error))
                return self.snapshot()
            except KeyboardInterrupt:
                self.state["decision"] = None
                self.pending_text = None
                self.stop("interrupted", "Run interrupted; inspect the final page before starting again.")
                raise

    def _command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                if state["status"] != "predicted":
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                if state["status"] == "blocked" and state.get("stop_reason"):
                    return self.snapshot()
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                state.setdefault("events", []).append({
                    "code": "stale_observation", "message": "Page changed; observed again without replaying input.",
                    "decision": len(state["decisions"]),
                })
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                self.stop("decision_budget", "Reached the model-decision budget.")
                return self.snapshot()
            remember(state)
            trigger = forced_review(state)
            if trigger:
                self.review(trigger)
                return self.snapshot()
            authorization = state.pop("review_authorization", None)
            if authorization and authorization["fingerprint"] != state["page"]["fingerprint"]:
                authorization = None
            with track_calls(state, "jev"):
                state["decision"] = choose(state["page"], state["goal"], state["history"], state.get("events"),
                                           state["task_state"], review_authorization=authorization)
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            if state["status"] == "blocked" and state.get("stop_reason"):
                return self.snapshot()
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            recovering = state.get("recovery") is not None
            selected = decision["choice"]
            if decision.get("review_gate") == "REVIEW":
                self.review("gate:" + decision["review_reason"])
                return self.snapshot()
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                if recovering and self.recovery_resolved():
                    state["recovery"] = None
                    recovering = False
                self.review("proposed_completion" if selected == "DONE" else "policy_blocked", proposed=selected)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                self.stop("action_budget", f"Stopped at the {MAX_STEPS}-action budget.")
                return self.snapshot()
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"], state["task_state"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    self.pending_text = None
                    try:
                        with track_calls(state, "text_helper"):
                            text, helper = field_text(context)
                    except TextHelperError as error:
                        state["text_calls"].append({**error.info, "field": action["label"],
                                                    "value": None, "input": context})
                        self.feedback("text_helper_rejected", str(error), action, consume=recovering)
                        if error.info.get("code") == "missing_field_value":
                            state["review_pending"] = "missing_field_value"
                        return self.snapshot()
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text, "input": context})
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed after text generation.")
                if text.replace("\r\n", "\n") == action.get("value", "").replace("\r\n", "\n"):
                    self.pending_text = None
                    self.feedback("same_value", "Text is already present; select a suggestion or change course.",
                                  action, consume=recovering)
                    return self.snapshot()
            mutation_key = self.mutation_key(page, action, text)
            if mutation_key in state.get("failed_mutations", []):
                self.pending_text = None
                self.feedback("suppressed_mutation", "Command failed in unchanged field state; choose another action.",
                              action, consume=recovering)
                return self.snapshot()
            # Browser.act checks freshness immediately before input, including after text generation.
            try:
                state["browser"].act(action, page, text=text)
            except MutationUncertain as error:
                self.pending_text = None
                state.setdefault("events", []).append({
                    "code": "execution_uncertain", "message": str(error), "action": action,
                    "text": text, "decision": len(state["decisions"]),
                })
                self.stop("execution_uncertain", "Browser input may have executed; stopped without replaying it.")
                try:
                    state["page"] = state["browser"].observe(screenshot=self.screenshots)
                except (RuntimeError, StalePage):
                    pass
                return self.snapshot()
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "node": action.get("node"),
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            try:
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            except (RuntimeError, StalePage):
                state["history"][-1]["effect"] = {"status": "unknown", "reason": "observation_interrupted"}
                self.stop("execution_uncertain", "Action executed but its effect could not be observed.")
                return self.snapshot()
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
                effect=state["page"].get("after_action", {"status": "not_applicable", "reason": "not_form_action"}),
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            effect = state["history"][-1]["effect"]
            state["progress"]["states"].append(semantic_key(state["page"], viewport=True))
            remember(state)
            state["status"] = "ready"
            if effect["status"] == "confirmed" and action["kind"] != "scroll":
                state["recovery"] = None
            elif effect["status"] == "confirmed":
                # A settled scroll is physical movement, not resolution of a failed form action.
                if recovering:
                    if self.recovery_resolved():
                        state["recovery"] = None
                    else:
                        self.feedback("reassessment_pending", "The earlier field effect remains unconfirmed.",
                                      action, consume=True)
            elif effect["status"] == "unknown":
                self.stop("execution_uncertain", "Input executed but its field effect could not be confirmed.")
            elif effect["status"] == "failed":
                state.setdefault("failed_mutations", []).append(mutation_key)
                self.feedback(effect["reason"], "Expected field effect was not confirmed; change course.",
                              action, consume=recovering)
                state["recovery"]["effect"] = effect
            elif not state["history"][-1]["page_changed"] and action["kind"] != "wait":
                state.setdefault("failed_mutations", []).append(mutation_key)
                self.feedback("no_observed_change", "The action produced no observed change.",
                              action, consume=recovering)
            elif recovering:
                if self.recovery_resolved():
                    state["recovery"] = None
                else:
                    self.feedback("reassessment_pending", "The earlier field effect remains unconfirmed.",
                                  action, consume=True)
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

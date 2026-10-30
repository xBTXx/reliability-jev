"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, MutationUncertain, StalePage
from .model import TextHelperError, action_space, choose, field_context, field_text
from .questions import MAX_STEPS


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
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
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        if self.state.get("started_at") is not None and self.state["status"] not in {"done", "blocked"}:
            self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
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
        recovery = state.get("recovery")
        if recovery is None:
            state["recovery"] = {"remaining": 2, "cause": event}
        elif consume:
            recovery["remaining"] -= 1
        state["status"] = "ready"
        if state["recovery"]["remaining"] <= 0:
            self.stop(code, f"Reassessment exhausted: {message}")

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
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
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
            state["decision"] = choose(state["page"], state["goal"], state["history"], state.get("events"))
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
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                if recovering and self.recovery_resolved():
                    state["recovery"] = None
                    recovering = False
                if selected == "DONE" and recovering:
                    self.feedback("unresolved_effect", "Completion is unconfirmed after an unresolved action.",
                                  consume=True)
                    return self.snapshot()
                state["status"] = "done" if selected == "DONE" else "blocked"
                if selected == "BLOCKED":
                    self.stop("policy_blocked", "The policy found no supported next action.")
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                self.stop("action_budget", f"Stopped at the {MAX_STEPS}-action budget.")
                return self.snapshot()
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    self.pending_text = None
                    try:
                        text, helper = field_text(context)
                    except TextHelperError as error:
                        state["text_calls"].append({**error.info, "field": action["label"],
                                                    "value": None, "input": context})
                        self.feedback("text_helper_rejected", str(error), action, consume=recovering)
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
            state["status"] = "ready"
            if effect["status"] == "confirmed":
                state["recovery"] = None
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

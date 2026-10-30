"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
SETTLE_STATE = Path(__file__).with_name("settle.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class MutationUncertain(RuntimeError):
    """Input may have reached Chrome. Never replay this operation automatically."""


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)
        # Let visible form fields finish their entrance animation before the first decision.
        # A hidden inactive form with no layout box does not hold up observation.
        self.call("Runtime.evaluate", expression="""new Promise(resolve=>{
          const start=performance.now();let last='',since=start;
          const tick=()=>{
            const fields=[...document.querySelectorAll('input,textarea,select')]
              .filter(e=>!['hidden','password','file'].includes(e.type)).map(e=>{
                const r=e.getBoundingClientRect();
                const inView=r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;
                return inView ? [e.id,e.getAttribute('name'),Math.round(r.x),Math.round(r.y),
                  e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})] : null;
              }).filter(Boolean);
            const signature=JSON.stringify(fields),now=performance.now();
            if(signature!==last){last=signature;since=now;}
            if(now-start>=3000 || (now-since>=100 && fields.every(f=>f[4]))) resolve();
            else setTimeout(tick,25);
          };tick();
        })""", awaitPromise=True, returnByValue=True)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        effect = None
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # Verification reads happen only after the agent has logged execution.
            try:
                response = self.call(
                    "Runtime.evaluate",
                    expression=("new Promise(resolve=>setTimeout(resolve,50))" if action["kind"] == "generic"
                                else f"({SETTLE_STATE})({json.dumps(action)})"),
                    awaitPromise=True, returnByValue=True,
                )
                effect = response.get("result", {}).get("value")
                if action["kind"] == "generic":
                    effect = None
                elif response.get("exceptionDetails") or not isinstance(effect, dict):
                    effect = {"status": "unknown", "reason": "verification_interrupted"}
            except RuntimeError:
                effect = {"status": "unknown", "reason": "verification_interrupted"}
        for attempt in range(10):
            try:
                page = browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
                if effect:
                    page["after_action"] = effect
                return page
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        if action["kind"] == "scroll":
            self.after_input = {"kind": "scroll", "before_y": page["scroll"]["y"],
                                "expected": action["delta"], "document": page["marker"][0]}
        else:
            self.after_input = result.get("verification") or ({"kind": "generic"} if action["kind"] != "wait" else None)
        return result

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state.get(k) for k in ("url", "text", "actions", "scroll", "dialogs", "active_dialog")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def mutate(method, **params):
        try:
            return call(method, **params)
        except (RuntimeError, OSError, TimeoutError) as error:
            raise MutationUncertain("Browser input acknowledgement failed; execution is uncertain.") from error

    def evaluate(expression):
        try:
            result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        except RuntimeError as error:
            if operation == "act" and request["action"]["kind"] == "select":
                raise MutationUncertain("Dropdown execution acknowledgement failed.") from error
            raise
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise MutationUncertain("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        verification = None
        if kind == "scroll":
            mutate("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              if (action.owner_node && window.__jevFast.owner(e.closest('[role="option"]'))?.node!==action.owner_node)
                return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              const c=window.__jevFast, field=action.kind==='fill' ? e : c.nodes.get(action.owner_node);
              const verification=field ? {
                kind:action.kind,node:action.node,field_node:action.kind==='fill' ? action.node : action.owner_node,
                document:performance.timeOrigin,expected:action.kind==='fill' ? action.expected : action.label,
                combobox:field.getAttribute('role')==='combobox',
                before_value:String('value' in field ? field.value : field.innerText||''),
                before_options:JSON.stringify(c.optionSignature(c.optionsFor(field)))
              } : null;
              return {x,y,verification};
            })(""" + json.dumps({**action, "expected": request.get("text")}) + ")")
            if target is None:
                if kind == "select":
                    raise MutationUncertain("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                verification = target.get("verification")
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    mutate("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    mutate(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    mutate(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    mutate("Input.insertText", text=request["text"])
        return {"executed": action["id"], "verification": verification}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info

"""Per-run model billing, using response costs and explicitly labelled token-rate estimates."""

import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation

# https://docs.typesafe.ai/models, checked 2026-10-30. Output tokens are free.
# Pin resolved versions: a future jev-latest release must not silently inherit an old price.
TYPESAFE_RATES = {"jev-1.14.0": "0.042", "jev-1.13.0": "0.042"}
ROLES = {"jev": "Jev", "text_helper": "Text helper", "supervisor": "Supervisor"}
ACTIVE_CALLS = ContextVar("jev_model_billing", default=None)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0 and math.isfinite(float(amount)):
            return amount
    except (InvalidOperation, ValueError, OverflowError):
        pass
    return None


def pricing():
    override = os.environ.get("TYPESAFE_INPUT_USD_PER_MILLION", "").strip()
    if override and number(override) is None:
        raise ValueError("TYPESAFE_INPUT_USD_PER_MILLION must be a finite, nonnegative USD rate.")
    return {"currency": "USD", "typesafe_input_per_million": dict(TYPESAFE_RATES),
            "typesafe_override": override or None, "checked_at": "2026-10-30",
            "source": "https://docs.typesafe.ai/models"}


@contextmanager
def track_calls(state, role):
    """Scope billing to this agent, including rejected responses and HTTP retries."""
    token = ACTIVE_CALLS.set((state.setdefault("model_calls", []), role))
    try:
        yield
    finally:
        ACTIVE_CALLS.reset(token)


def record_call(model, *, result=None, http_status=None, error=None, error_details=None, record=None):
    active = ACTIVE_CALLS.get()
    if active is None:
        return
    calls, role = active
    result = result if isinstance(result, dict) else {}
    resolved = result.get("model")
    resolved = resolved if isinstance(resolved, str) and resolved else model
    provider = result.get("provider")
    if record is None:
        record = {}
        calls.append(record)
    record.update({"role": role, "model": resolved if isinstance(resolved, str) else None,
                   "provider": provider if isinstance(provider, str) else None, "http_status": http_status,
                   "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
                   "error": error, "error_details": error_details})
    return record


def recorded_calls(state):
    if "model_calls" in state:
        return state["model_calls"], "request_ledger"
    # Historical traces predate the request ledger. Never count history rows or cached text twice.
    calls = [{"role": role, **call} for role, key in (("jev", "decisions"), ("text_helper", "text_calls"))
             for call in state.get(key, [])]
    for review in state.get("supervisor_calls", []):
        if "attempts" in review:
            calls.extend({"role": "supervisor", "model": review.get("model"), **attempt}
                         for attempt in review["attempts"])
        elif review.get("attempt_count", 1):
            calls.append({"role": "supervisor", **review})
    return calls, "response_logs"


def call_cost(call, rates):
    usage = call.get("usage") or {}
    currency = usage.get("currency", "USD")
    if currency != "USD":
        return None, "unavailable"
    if "cost" in usage:
        amount = number(usage["cost"])
        return amount, "reported" if amount is not None else "unavailable"
    if call["role"] == "jev":
        rate = rates.get("typesafe_override")
        if rate is None:
            rate = rates["typesafe_input_per_million"].get(call.get("model"))
        tokens = number(usage.get("input_tokens"))
        rate = number(rate)
        if tokens is not None and tokens == tokens.to_integral_value() and rate is not None:
            return tokens * rate / Decimal(1_000_000), "estimated"
    return None, "unavailable"


def cost_summary(state):
    """One row per HTTP attempt; aggregate supervisor usage is deliberately not added again."""
    rates = state.get("cost_pricing") or pricing()
    calls, basis = recorded_calls(state)
    roles, total, reported, estimated = {}, Decimal(0), Decimal(0), Decimal(0)
    missing = 0
    for role in ROLES:
        rows = [c for c in calls if c["role"] == role]
        subtotal, role_reported, role_estimated, unavailable = Decimal(0), 0, 0, 0
        input_tokens, output_tokens = 0, 0
        for call in rows:
            usage = call.get("usage") or {}
            for keys, is_input in ((("input_tokens", "prompt_tokens"), True),
                                   (("output_tokens", "completion_tokens"), False)):
                value = number(usage.get(keys[0], usage.get(keys[1])))
                if value is not None:
                    if is_input:
                        input_tokens += int(value)
                    else:
                        output_tokens += int(value)
            amount, source = call_cost(call, rates)
            if amount is None:
                unavailable += 1
            else:
                subtotal += amount
                if source == "reported":
                    role_reported += 1
                    reported += amount
                else:
                    role_estimated += 1
                    estimated += amount
        roles[role] = {"requests": len(rows), "known_cost_usd": float(subtotal),
                       "reported_requests": role_reported, "estimated_requests": role_estimated,
                       "unavailable_requests": unavailable, "input_tokens": input_tokens,
                       "output_tokens": output_tokens}
        total += subtotal
        missing += unavailable
    return {"currency": "USD", "total_usd": float(total) if missing == 0 else None,
            "known_cost_usd": float(total), "reported_cost_usd": float(reported),
            "estimated_cost_usd": float(estimated), "complete": missing == 0,
            "has_estimates": any(r["estimated_requests"] for r in roles.values()),
            "unavailable_requests": missing, "basis": basis, "by_role": roles, "pricing": rates}


def money(value):
    amount = Decimal(str(value))
    return "<$0.000001" if 0 < amount < Decimal("0.000001") else f"${amount:.6f}"


def format_cost(summary):
    lines = ["Model cost (USD):"]
    for role, label in ROLES.items():
        row = summary["by_role"][role]
        description = []
        if row["estimated_requests"]:
            description.append("estimated")
        if row["reported_requests"]:
            description.append("API reported")
        if row["unavailable_requests"]:
            description.append(f"{row['unavailable_requests']} unavailable")
        priced = row["reported_requests"] + row["estimated_requests"]
        value = money(row["known_cost_usd"]) if priced or not row["requests"] else "unavailable"
        if row["unavailable_requests"] and value != "unavailable":
            value += " known"
        detail = ", ".join(description) if description else "no requests"
        requests = f"{row['requests']} request" + ("s" if row["requests"] != 1 else "")
        lines.append(f"  {label}: {value} ({requests}; {detail})")
    if summary["complete"]:
        label = "Total (estimated)" if summary["has_estimates"] else "Total"
        lines.append(f"  {label}: {money(summary['total_usd'])}")
    else:
        requests = f"{summary['unavailable_requests']} request" + ("s" if summary["unavailable_requests"] != 1 else "")
        lines.append(f"  Known subtotal: {money(summary['known_cost_usd'])}; total unavailable "
                     f"({requests} without cost data)")
    if summary["basis"] == "response_logs":
        lines.append("  Historical response logs only; unrecorded failed requests are unavailable.")
    return "\n".join(lines)


def format_run_summary(state):
    lines = [f"Task: {state['goal']}",
             f"Result: {state['status']} | {len(state.get('history', []))} actions | "
             f"{state.get('elapsed_ms', 0) / 1000:.2f} s"]
    reviews = state.get("supervisor_calls", [])
    if reviews and reviews[-1].get("output", {}).get("summary"):
        lines.append("Assessment: " + reviews[-1]["output"]["summary"])
    if state.get("stop_reason"):
        lines.append(f"Stop reason: {state['stop_reason']['code']}: {state['stop_reason']['message']}")
    lines.append(format_cost(state.get("cost_summary") or cost_summary(state)))
    return "\n".join(lines)

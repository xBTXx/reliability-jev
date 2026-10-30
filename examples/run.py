"""uv run --env-file .env python examples/run.py --url URL --goal 'A narrow goal'"""

import argparse
import json
from pathlib import Path

from jev_ultrafast import Agent
from jev_ultrafast.costs import format_run_summary
from jev_ultrafast.progress import print_progress

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--goal", action="append", required=True, help="Repeat for an ordered list of goals.")
parser.add_argument("--keep-open", action="store_true", help="Leave the owned browser tab open for inspection.")
parser.add_argument("--output", type=Path, help="Save the observed page and decisions as a JSON trace.")
args = parser.parse_args()

agent = Agent(args.url, args.goal, on_progress=print_progress)
try:
    for state in agent.run():
        decision = state["decisions"][-1] if state["decisions"] else {}
        print(
            f"{state['elapsed_ms']:>5} ms  {len(state['history'])} actions  {state['status']}"
            f"  operation={decision.get('operation', 'none')}"
            f"  reviews={len(state['supervisor_calls'])}",
            flush=True,
        )
finally:
    state = agent.snapshot()
    try:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Trace: {args.output}", flush=True)
        print(f"Final observed URL: {state['page']['url']}", flush=True)
        print(f"Page title: {state['page']['title']}", flush=True)
        if state.get("question"):
            print("Provide the missing information in the goal and start a new run.", flush=True)
        print(f"Model calls: {len(state['decisions'])} Jev decisions, {len(state['text_calls'])} text helpers, "
              f"{sum(c.get('attempt_count', 1) for c in state['supervisor_calls'])} supervisor requests "
              f"in {len(state['supervisor_calls'])} reviews", flush=True)
        print("Observed text:\n" + state["page"]["text"][:2000], flush=True)
        print("The model's status does not independently verify the task outcome.", flush=True)
        print("\n" + format_run_summary(state), flush=True)
    finally:
        if not args.keep_open:
            agent.close()

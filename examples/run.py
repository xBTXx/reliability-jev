"""uv run --env-file .env python examples/run.py --url URL --goal 'A narrow goal'"""

import argparse
import json
from pathlib import Path

from jev_ultrafast import Agent

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--goal", action="append", required=True, help="Repeat for an ordered list of goals.")
parser.add_argument("--keep-open", action="store_true", help="Leave the owned browser tab open for inspection.")
parser.add_argument("--output", type=Path, help="Save the observed page and decisions as a JSON trace.")
args = parser.parse_args()

agent = Agent(args.url, args.goal)
try:
    for state in agent.run():
        decision = state["decisions"][-1] if state["decisions"] else {}
        print(
            f"{state['elapsed_ms']:>5} ms  {len(state['history'])} actions  {state['status']}"
            f"  operation={decision.get('operation', 'none')}",
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
        if state.get("stop_reason"):
            print(f"Stop reason: {state['stop_reason']['code']}: {state['stop_reason']['message']}", flush=True)
        print("Observed text:\n" + state["page"]["text"][:2000], flush=True)
        print("The model's status does not independently verify the task outcome.", flush=True)
    finally:
        if not args.keep_open:
            agent.close()

"""Paid supervisor smoke check against recorded observations; no browser input or pytest use."""

import argparse
import json
from pathlib import Path

from jev_ultrafast.supervision import SupervisorError, supervise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path, help="Saved trace with a supervisor input.")
    parser.add_argument("--output", required=True, type=Path, help="New JSON evidence file; never overwrite a run.")
    parser.add_argument("--runs", default=1, type=int, choices=range(1, 6))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path to preserve existing evidence.")
    trace = json.loads(args.trace.read_text(encoding="utf-8"))
    context = trace["supervisor_calls"][0]["input"]
    results = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print("LIVE model calls using recorded observations. No browser input will be sent.", flush=True)
    for number in range(1, args.runs + 1):
        try:
            output, info = supervise(context)
            result = {"run": number, "passed": True, "output": output, **info}
        except SupervisorError as error:
            result = {"run": number, "passed": False, **error.info}
        results.append(result)
        args.output.write_text(json.dumps({"source_trace": str(args.trace), "results": results},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Run {number}: {'PASS' if result['passed'] else 'FAIL'}; "
              f"{result.get('attempt_count', 0)} requests; {result.get('latency_ms', 0)} ms", flush=True)
        if not result["passed"]:
            print(result["error"], flush=True)
            break
    print(f"Evidence: {args.output}", flush=True)
    return 0 if len(results) == args.runs and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

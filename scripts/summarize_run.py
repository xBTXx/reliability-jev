"""Print a saved task's outcome and model cost without browser or model requests."""

import argparse
import json
from pathlib import Path

from jev_ultrafast.costs import format_run_summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    state = json.loads(args.trace.read_text(encoding="utf-8"))
    print(format_run_summary(state))
    print("The model's assessment does not independently verify the task outcome.")


if __name__ == "__main__":
    main()

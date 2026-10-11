"""Read-only summary of exported observation JSONL; no instance or network access."""

import argparse
import json
import sys
import time
from pathlib import Path

from signalnest.observation_summary import (
    ObservationError,
    read_observations,
    summarize_observations,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--start", required=True, type=int)
    parser.add_argument("--end", type=int, help="actual summary time; defaults to now")
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--tolerance-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        records, diagnostics = read_observations(args.files)
        report = summarize_observations(
            records,
            start=args.start,
            end=int(time.time()) if args.end is None else args.end,
            expected_sources=tuple(args.source),
            interval_seconds=args.interval_seconds,
            tolerance_seconds=args.tolerance_seconds,
        )
        report["input_diagnostics"] = diagnostics
    except ObservationError as exc:
        print(f"observation summary failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit local source snapshot; never deploy, commit, connect or read secrets."""

import argparse
import json
import sys
from pathlib import Path

from signalnest.rollout import RolloutError, create_release_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Save a deterministic allowlisted SignalNest release and evidence sidecar."
    )
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument(
        "--output", required=True, type=Path, help="New .tar.gz path outside source."
    )
    arguments = parser.parse_args(argv)
    try:
        result = create_release_snapshot(arguments.release_root, arguments.output)
    except RolloutError as exc:
        print(json.dumps({"error_code": exc.code}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

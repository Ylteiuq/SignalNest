"""Explicit local source snapshot; never deploy, commit, connect or read secrets."""

import argparse
import json
import sys
from pathlib import Path

from signalnest.rollout import RolloutError, create_release_snapshot, verify_release_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Save or verify an allowlisted SignalNest release without instance access."
    )
    parser.add_argument(
        "--release-root", type=Path, help="Source root to freeze or compare with a saved release."
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--output", type=Path, help="New .tar.gz path outside source.")
    operation.add_argument("--verify", type=Path, help="Existing archive with its sibling sidecar.")
    parser.add_argument(
        "--expected-content-sha256", help="Trusted content fingerprint; only used with --verify."
    )
    arguments = parser.parse_args(argv)
    if arguments.output is not None and arguments.release_root is None:
        parser.error("--output requires --release-root")
    if arguments.expected_content_sha256 is not None and arguments.verify is None:
        parser.error("--expected-content-sha256 requires --verify")
    try:
        if arguments.verify is not None:
            result = verify_release_snapshot(
                arguments.verify,
                repo_root=arguments.release_root,
                expected_content_sha256=arguments.expected_content_sha256,
            )
        else:
            result = create_release_snapshot(arguments.release_root, arguments.output)
    except RolloutError as exc:
        print(json.dumps({"error_code": exc.code}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""One actual-time, read-only observation for an external timer; never schedules work."""

import argparse
import json
import sys
import time
from pathlib import Path

from signalnest.config import ConfigurationError, load_config
from signalnest.rollout import RolloutError, observe_instance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--profile-confirmed", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = load_config(args.config)
        report = observe_instance(
            settings,
            at=int(time.time()),
            release_root=args.release_root,
            profile_path=args.profile,
            profile_confirmed=args.profile_confirmed,
        )
    except ConfigurationError:
        print("观测失败: config_invalid；请先执行 config-check", file=sys.stderr)
        return 2
    except RolloutError as exc:
        print(f"观测失败: {exc.code}", file=sys.stderr)
        return 1
    # A produced observation may describe blocked/attention states. Exit 0 is not
    # a health declaration; journald retains the report with its actual timestamp.
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

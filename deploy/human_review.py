"""Prepare independent review bundles, record explicit labels, or report confirmed results."""

import argparse
import json
import sys
from pathlib import Path

from signalnest.human_review import ReviewError, make_label, prepare_review, review_report


def _read(path):
    try:
        with path.open("rb") as stream:
            content = stream.read(32 * 1024 * 1024 + 1)
        if len(content) > 32 * 1024 * 1024:
            raise ReviewError("review_file_too_large")
        return json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, ReviewError):
            raise
        raise ReviewError("review_file_invalid") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze full snapshots; no automatic labels")
    prepare.add_argument("--notifications", type=Path, nargs="*", default=[])
    prepare.add_argument("--search", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    label = commands.add_parser("label", help="record one explicit human judgement")
    label.add_argument("--bundle", type=Path, required=True)
    label.add_argument("--item-id", required=True)
    label.add_argument("--reviewer", required=True)
    label.add_argument("--at", required=True, help="actual review ISO time with timezone")
    label.add_argument("--pending", action="store_true")
    label.add_argument("--action", choices=["PUSH_NOW", "DIGEST", "STORE_ONLY", "IGNORE"])
    label.add_argument(
        "--relevant-id", nargs="*", help="complete relevant corpus set; empty allowed"
    )
    label.add_argument("--note", default="")
    label.add_argument("--output", type=Path, required=True)
    report = commands.add_parser("report", help="read-only metrics; unlabelled remains pending")
    report.add_argument("--bundle", type=Path, required=True)
    report.add_argument("--labels", type=Path, nargs="*", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            snapshots = {p.stem: _read(p) for p in args.notifications}
            if len(snapshots) != len(args.notifications):
                raise ReviewError("review_group_duplicate")
            result = prepare_review(snapshots, _read(args.search) if args.search else None)
        elif args.command == "label":
            result = make_label(
                _read(args.bundle),
                args.item_id,
                reviewer=args.reviewer,
                reviewed_at=args.at,
                status="pending" if args.pending else "confirmed",
                action=args.action,
                relevant_corpus_ids=args.relevant_id,
                note=args.note,
            )
        else:
            result = review_report(_read(args.bundle), [_read(p) for p in args.labels])
        if args.command in {"prepare", "label"}:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
                )
            print(json.dumps({"format": result["format"], "output_created": True}))
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except ReviewError as exc:
        print(f"human review failed: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("human review failed: review_file_unavailable_or_exists", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

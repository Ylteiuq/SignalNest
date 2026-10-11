"""Independent, input-bound human review of frozen offline evaluation results.

No production state, decisions or mail are modified. Engineering expectations are
deliberately excluded from review bundles and never used as human labels.
"""

import json
import re
from collections import Counter
from datetime import datetime

from signalnest.notifications.contracts import canonical_sha256

ACTION = frozenset({"PUSH_NOW", "DIGEST", "STORE_ONLY", "IGNORE"})
REMIND = frozenset({"PUSH_NOW", "DIGEST"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class ReviewError(ValueError):
    """Finite validation codes; never serialize an input page or arbitrary error."""


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ReviewError("review_identifier_invalid")
    return value


def _digest(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ReviewError("review_digest_invalid")
    return value


def _hash(value):
    try:
        json.dumps(value, allow_nan=False)
        return canonical_sha256(value)
    except (TypeError, ValueError):
        raise ReviewError("review_json_invalid") from None


def _item(identifier, kind, inputs, prediction, evidence):
    return {
        "item_id": _identifier(identifier),
        "kind": kind,
        "input": inputs,
        "input_sha256": _hash(inputs),
        "prediction": prediction,
        "evidence": evidence,
    }


def prepare_review(notification_snapshots: dict[str, dict], search_snapshot: dict | None = None):
    """Freeze full replay rows and retrieval evidence, with all labels pending.

    A compact engineering summary cannot reconstruct individual inputs and is
    rejected. The caller must retain the original snapshots and referenced HTML.
    """
    items = []
    for group, report in sorted(notification_snapshots.items()):
        _identifier(group)
        if (
            not isinstance(report, dict)
            or report.get("kind") != "offline_production_notification_replay"
            or not isinstance(report.get("results"), list)
            or not report["results"]
        ):
            raise ReviewError("review_snapshot_incomplete")
        provenance = {
            "snapshot_sha256": _hash(report),
            "manifest_sha256": _digest(report.get("manifest_sha256")),
            "source_sha256": report.get("source_sha256"),
            "versions": report.get("versions"),
        }
        for row in report["results"]:
            if not isinstance(row, dict) or not isinstance(row.get("decision"), dict):
                raise ReviewError("review_snapshot_incomplete")
            if (
                not isinstance(row["decision"].get("action"), str)
                or row["decision"]["action"] not in ACTION
            ):
                raise ReviewError("review_prediction_invalid")
            if not all(k in row for k in ("profile", "context", "evaluated_at", "input_kind")):
                raise ReviewError("review_snapshot_incomplete")
            inputs = {k: row[k] for k in ("profile", "context", "evaluated_at", "input_kind")}
            if row["input_kind"] == "fixture_html":
                if not all(k in row for k in ("fixture", "source_url", "parser_version")):
                    raise ReviewError("review_snapshot_incomplete")
                inputs.update({k: row[k] for k in ("fixture", "source_url", "parser_version")})
                inputs["fixture_sha256"] = _digest(row.get("fixture_sha256"))
            elif row["input_kind"] == "synthetic_notice_content":
                if not isinstance(row.get("synthetic_notice_content"), dict):
                    raise ReviewError("review_snapshot_incomplete")
                inputs["synthetic_notice_content"] = row["synthetic_notice_content"]
                inputs["content_sha256"] = _digest(row.get("content_sha256"))
            else:
                raise ReviewError("review_input_kind_invalid")
            items.append(
                _item(
                    f"notification:{group}:{_identifier(row.get('case_id'))}",
                    "notification",
                    inputs,
                    row["decision"],
                    {"facts": row.get("facts"), "provenance": provenance},
                )
            )
    if search_snapshot is not None:
        report = search_snapshot
        if (
            not isinstance(report, dict)
            or report.get("kind") != "offline_production_lexical_search_evaluation"
            or not isinstance(report.get("queries"), list)
            or not isinstance(report.get("corpus"), list)
        ):
            raise ReviewError("review_snapshot_incomplete")
        corpus = report["corpus"]
        identifiers = [r.get("corpus_id") for r in corpus if isinstance(r, dict)]
        if not corpus or len(identifiers) != len(corpus):
            raise ReviewError("review_corpus_invalid")
        for row in corpus:
            _identifier(row["corpus_id"])
            _digest(row.get("sha256"))
            _digest(row.get("content_sha256"))
        if len(set(identifiers)) != len(corpus):
            raise ReviewError("review_corpus_invalid")
        production = report.get("methods", {}).get("production", {}).get("query_results", {})
        for query in report["queries"]:
            if not isinstance(query, dict) or not isinstance(query.get("query"), str):
                raise ReviewError("review_query_invalid")
            identifier = _identifier(query.get("query_id"))
            ranking = production.get(identifier)
            if (
                not isinstance(ranking, list)
                or any(not isinstance(r, str) for r in ranking)
                or any(r not in identifiers for r in ranking)
                or len(set(ranking)) != len(ranking)
            ):
                raise ReviewError("review_prediction_invalid")
            inputs = {
                k: query[k] for k in ("query", "source_id", "date_from", "date_to") if k in query
            }
            inputs["corpus"] = corpus
            items.append(
                _item(
                    f"search:{identifier}",
                    "search",
                    inputs,
                    {"ranked_corpus_ids": ranking},
                    {
                        "snapshot_sha256": _hash(report),
                        "production_page": report.get("production_pages", {}).get(identifier),
                    },
                )
            )
    if not items or len({i["item_id"] for i in items}) != len(items):
        raise ReviewError("review_items_invalid")
    payload = {"format": "signalnest-human-review-bundle-v1", "items": items}
    return {**payload, "bundle_sha256": _hash(payload)}


def validate_bundle(bundle):
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"format", "items", "bundle_sha256"}
        or bundle["format"] != "signalnest-human-review-bundle-v1"
        or not isinstance(bundle["items"], list)
        or not bundle["items"]
    ):
        raise ReviewError("review_bundle_invalid")
    if _digest(bundle["bundle_sha256"]) != _hash({k: bundle[k] for k in ("format", "items")}):
        raise ReviewError("review_bundle_digest_mismatch")
    seen = set()
    for item in bundle["items"]:
        if not isinstance(item, dict) or set(item) != {
            "item_id",
            "kind",
            "input",
            "input_sha256",
            "prediction",
            "evidence",
        }:
            raise ReviewError("review_item_invalid")
        identifier = _identifier(item["item_id"])
        if (
            identifier in seen
            or not isinstance(item["kind"], str)
            or item["kind"] not in {"notification", "search"}
        ):
            raise ReviewError("review_items_invalid")
        seen.add(identifier)
        if not isinstance(item["input"], dict) or _digest(item["input_sha256"]) != _hash(
            item["input"]
        ):
            raise ReviewError("review_input_digest_mismatch")
        prediction = item["prediction"]
        if not isinstance(prediction, dict):
            raise ReviewError("review_prediction_invalid")
        if item["kind"] == "notification" and (
            not isinstance(prediction.get("action"), str) or prediction["action"] not in ACTION
        ):
            raise ReviewError("review_prediction_invalid")
        if item["kind"] == "search":
            corpus = item["input"].get("corpus", [])
            identifiers = [c.get("corpus_id") for c in corpus if isinstance(c, dict)]
            ranking = prediction.get("ranked_corpus_ids")
            if (
                not identifiers
                or any(not isinstance(r, str) for r in identifiers)
                or len(identifiers) != len(corpus)
                or len(set(identifiers)) != len(corpus)
                or not isinstance(ranking, list)
                or any(not isinstance(r, str) for r in ranking)
                or len(set(ranking)) != len(ranking)
                or any(r not in identifiers for r in ranking)
            ):
                raise ReviewError("review_prediction_invalid")


def make_label(
    bundle,
    item_id,
    *,
    reviewer,
    reviewed_at,
    status="confirmed",
    action=None,
    relevant_corpus_ids=None,
    note="",
):
    """Explicit operator entry; no default from system or engineering Action."""
    validate_bundle(bundle)
    item = next((r for r in bundle["items"] if r["item_id"] == item_id), None)
    if item is None:
        raise ReviewError("review_item_unknown")
    record = {
        "format": "signalnest-human-label-v1",
        "bundle_sha256": bundle["bundle_sha256"],
        "item_id": item_id,
        "input_sha256": item["input_sha256"],
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "status": status,
        "action": action,
        "relevant_corpus_ids": relevant_corpus_ids,
        "note": note,
    }
    _validate_label(record, item)
    return record


def _validate_label(record, item=None):
    if (
        not isinstance(record, dict)
        or set(record)
        != {
            "format",
            "bundle_sha256",
            "item_id",
            "input_sha256",
            "reviewer",
            "reviewed_at",
            "status",
            "action",
            "relevant_corpus_ids",
            "note",
        }
        or record["format"] != "signalnest-human-label-v1"
    ):
        raise ReviewError("review_label_invalid")
    _digest(record["bundle_sha256"])
    _digest(record["input_sha256"])
    _identifier(record["item_id"])
    if (
        not isinstance(record["reviewer"], str)
        or not record["reviewer"].strip()
        or len(record["reviewer"]) > 100
        or not isinstance(record["note"], str)
        or len(record["note"]) > 2000
        or not isinstance(record["status"], str)
        or record["status"] not in {"pending", "confirmed"}
    ):
        raise ReviewError("review_label_invalid")
    try:
        clock = datetime.fromisoformat(record["reviewed_at"])
        if clock.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        raise ReviewError("review_label_time_invalid") from None
    action, relevant = record["action"], record["relevant_corpus_ids"]
    if record["status"] == "pending":
        if action is not None or relevant is not None:
            raise ReviewError("review_pending_has_label")
    elif item is not None and item["kind"] == "notification":
        if not isinstance(action, str) or action not in ACTION or relevant is not None:
            raise ReviewError("review_action_invalid")
    elif item is not None and item["kind"] == "search":
        corpus = {c["corpus_id"] for c in item["input"]["corpus"]}
        if (
            action is not None
            or not isinstance(relevant, list)
            or any(not isinstance(r, str) for r in relevant)
            or len(set(relevant)) != len(relevant)
            or any(r not in corpus for r in relevant)
        ):
            raise ReviewError("review_relevance_invalid")


def review_report(bundle, labels):
    """Score only explicitly confirmed, exactly bound records; never fill gaps."""
    validate_bundle(bundle)
    items = {r["item_id"]: r for r in bundle["items"]}
    selected, stale, duplicates = {}, [], 0
    for record in labels:
        _validate_label(record)
        item = items.get(record["item_id"])
        if (
            item is None
            or record["bundle_sha256"] != bundle["bundle_sha256"]
            or record["input_sha256"] != item["input_sha256"]
        ):
            stale.append({"item_id": record["item_id"], "reason": "binding_mismatch"})
            continue
        _validate_label(record, item)
        previous = selected.get(record["item_id"])
        if previous is not None:
            if previous != record:
                raise ReviewError("review_conflicting_labels")
            duplicates += 1
        selected[record["item_id"]] = record
    confirmed = {key: r for key, r in selected.items() if r["status"] == "confirmed"}
    states = {key: "confirmed" if key in confirmed else "pending" for key in items}
    counts = Counter()
    confusion = Counter()
    urgent_misses = []
    search_rows = []
    for key, label in confirmed.items():
        item = items[key]
        if item["kind"] == "notification":
            actual, predicted = label["action"], item["prediction"]["action"]
            confusion[f"{actual}->{predicted}"] += 1
            truth, sent = actual in REMIND, predicted in REMIND
            counts[
                "true_positive"
                if truth and sent
                else "false_negative"
                if truth
                else "false_positive"
                if sent
                else "true_negative"
            ] += 1
            if actual == "PUSH_NOW" and predicted != "PUSH_NOW":
                urgent_misses.append(key)
        else:
            search_rows.append(
                (item["prediction"]["ranked_corpus_ids"], set(label["relevant_corpus_ids"]))
            )
    positive = counts["true_positive"] + counts["false_negative"]
    negative = counts["true_negative"] + counts["false_positive"]
    nonempty = [row for row in search_rows if row[1]]
    recall = {
        str(k): (
            sum(len(set(r[:k]) & gold) / len(gold) for r, gold in nonempty) / len(nonempty)
            if nonempty
            else None
        )
        for k in (1, 3, 5)
    }
    return {
        "format": "signalnest-human-review-report-v1",
        "bundle_sha256": bundle["bundle_sha256"],
        "item_states": states,
        "confirmed": len(confirmed),
        "pending": len(items) - len(confirmed),
        "stale_records": stale,
        "duplicate_records": duplicates,
        "notification": {
            "confirmed": sum(items[k]["kind"] == "notification" for k in confirmed),
            "confusion": dict(sorted(confusion.items())),
            "reminder_confusion": {
                k: counts[k]
                for k in ("true_positive", "false_negative", "false_positive", "true_negative")
            },
            "false_negative_rate": counts["false_negative"] / positive if positive else None,
            "false_positive_rate": counts["false_positive"] / negative if negative else None,
            "urgent_miss_items": urgent_misses,
        },
        "search": {
            "confirmed": len(search_rows),
            "nonempty_relevance_queries": len(nonempty),
            "macro_recall_at_k": recall,
            "empty_relevance_queries": len(search_rows) - len(nonempty),
            "empty_query_correct": sum(not r for r, gold in search_rows if not gold),
        },
        "engineering_expectations_used_as_labels": False,
        "limitations": [
            "Labels are explicit reviewer declarations, not authenticated identities.",
            "Rates describe this labelled subset, not real-world notification miss rates.",
            "Unconfirmed, stale and conflicted inputs are never presumed correct.",
        ],
    }

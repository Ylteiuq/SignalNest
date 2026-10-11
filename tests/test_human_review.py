"""Human records are independent of the real 31-case engineering replay."""

import copy
import json
import runpy
from pathlib import Path

import pytest

from signalnest.human_review import ReviewError, make_label, prepare_review, review_report
from signalnest.notifications.contracts import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy/human_review.py"
AT = "2026-10-11T20:00:00+08:00"
GROUPS = (
    "notification-production",
    "notification-production-synthetic",
    "teaching-assistant",
    "notification-current-instruction",
    "notification-original-misses",
    "cs-offline",
)


@pytest.fixture(scope="module")
def bundle():
    replay = runpy.run_path(str(ROOT / "deploy/evaluate_notifications.py"))["evaluate_cases"]
    snapshots = {
        group: replay(ROOT / "docs/validation" / (group + "-cases.json")) for group in GROUPS
    }
    search = runpy.run_path(str(ROOT / "deploy/evaluate_search.py"))["evaluate_cases"]()
    return prepare_review(snapshots, search)


def label(bundle, identifier, **values):
    return make_label(
        bundle, identifier, reviewer="test reviewer, not a real operator", reviewed_at=AT, **values
    )


def test_actual_31_notification_and_15_search_inputs_start_pending_without_gold(bundle):
    assert len(bundle["items"]) == 46
    assert sum(i["kind"] == "notification" for i in bundle["items"]) == 31
    assert sum(i["kind"] == "search" for i in bundle["items"]) == 15
    serialized = json.dumps(bundle)
    assert "engineering_expectation" not in serialized
    assert "engineering_mismatches" not in serialized
    assert "relevant_corpus_ids" not in serialized
    report = review_report(bundle, [])
    assert report["confirmed"] == 0 and report["pending"] == 46
    assert report["notification"]["false_negative_rate"] is None
    assert report["notification"]["false_positive_rate"] is None
    assert report["search"]["macro_recall_at_k"] == {"1": None, "3": None, "5": None}
    assert not report["engineering_expectations_used_as_labels"]


def test_independent_labels_produce_reminder_and_urgency_misses_not_action_equivalence(bundle):
    negative = next(
        i
        for i in bundle["items"]
        if i["kind"] == "notification" and i["prediction"]["action"] == "IGNORE"
    )
    push = next(
        i
        for i in bundle["items"]
        if i["kind"] == "notification" and i["prediction"]["action"] == "PUSH_NOW"
    )
    digest = next(
        i
        for i in bundle["items"]
        if i["kind"] == "notification" and i["prediction"]["action"] == "DIGEST"
    )
    labels = [
        label(bundle, negative["item_id"], action="PUSH_NOW"),
        label(bundle, push["item_id"], action="IGNORE"),
        label(bundle, digest["item_id"], action="PUSH_NOW"),
    ]
    report = review_report(bundle, labels)
    assert report["confirmed"] == 3 and report["pending"] == 43
    assert report["notification"]["reminder_confusion"] == {
        "true_positive": 1,
        "false_negative": 1,
        "false_positive": 1,
        "true_negative": 0,
    }
    assert report["notification"]["false_negative_rate"] == 0.5
    assert report["notification"]["false_positive_rate"] == 1
    assert set(report["notification"]["urgent_miss_items"]) == {
        negative["item_id"],
        digest["item_id"],
    }


def test_search_labels_cover_full_corpus_not_only_returned_and_empty_is_distinct(bundle):
    labels = [
        label(bundle, "search:S03", relevant_corpus_ids=["ems-250571"]),
        label(bundle, "search:S11", relevant_corpus_ids=[]),
        label(bundle, "search:S15", relevant_corpus_ids=["18135", "117011"]),
    ]
    report = review_report(bundle, labels)["search"]
    assert report["confirmed"] == 3
    assert report["nonempty_relevance_queries"] == 2
    assert report["empty_relevance_queries"] == report["empty_query_correct"] == 1
    assert report["macro_recall_at_k"]["5"] == 0.75


def test_stale_input_or_other_bundle_stays_pending_and_is_reported(bundle):
    item = bundle["items"][0]
    record = label(bundle, item["item_id"], action="IGNORE")
    for field in ("bundle_sha256", "input_sha256"):
        old = dict(record, **{field: "0" * 64})
        report = review_report(bundle, [old])
        assert report["confirmed"] == 0 and report["pending"] == 46
        assert report["stale_records"] == [
            {"item_id": item["item_id"], "reason": "binding_mismatch"}
        ]


def test_duplicate_label_does_not_count_twice_and_disagreement_requires_explicit_resolution(bundle):
    item = bundle["items"][0]
    first = label(bundle, item["item_id"], action="IGNORE")
    assert review_report(bundle, [first, first])["duplicate_records"] == 1
    second = label(bundle, item["item_id"], action="STORE_ONLY")
    with pytest.raises(ReviewError, match="conflicting_labels"):
        review_report(bundle, [first, second])


def test_pending_requires_no_action_and_naive_time_is_rejected(bundle):
    item = bundle["items"][0]
    pending = label(bundle, item["item_id"], status="pending", note="qualification still unknown")
    assert review_report(bundle, [pending])["pending"] == 46
    with pytest.raises(ReviewError, match="pending_has_label"):
        label(bundle, item["item_id"], status="pending", action="IGNORE")
    with pytest.raises(ReviewError, match="label_time_invalid"):
        make_label(
            bundle,
            item["item_id"],
            reviewer="test",
            reviewed_at="2026-10-11T20:00:00",
            action="IGNORE",
        )


@pytest.mark.parametrize(
    "values",
    [
        {"action": None},
        {"action": {}},
        {"action": "PUSH_NOW", "relevant_corpus_ids": []},
    ],
)
def test_notification_label_cannot_inherit_predicted_action_or_search_values(bundle, values):
    with pytest.raises(ReviewError, match="action_invalid"):
        label(bundle, bundle["items"][0]["item_id"], **values)


@pytest.mark.parametrize(
    "values",
    [
        {"relevant_corpus_ids": None},
        {"relevant_corpus_ids": ["unknown-id"]},
        {"relevant_corpus_ids": ["18135", "18135"]},
        {"relevant_corpus_ids": [{}]},
        {"relevant_corpus_ids": [], "action": "IGNORE"},
    ],
)
def test_search_label_rejects_unknown_duplicates_and_cross_type_action(bundle, values):
    with pytest.raises(ReviewError, match="relevance_invalid"):
        label(bundle, "search:S15", **values)


def test_bundle_and_input_digests_detect_any_untracked_input_or_prediction_change(bundle):
    changed = copy.deepcopy(bundle)
    changed["items"][0]["prediction"]["action"] = "IGNORE"
    with pytest.raises(ReviewError, match="bundle_digest_mismatch"):
        review_report(changed, [])
    changed = copy.deepcopy(bundle)
    changed["items"][0]["input"]["evaluated_at"] = "2026-10-11T22:00:00+08:00"
    changed["bundle_sha256"] = canonical_sha256({k: changed[k] for k in ("format", "items")})
    with pytest.raises(ReviewError, match="input_digest_mismatch"):
        review_report(changed, [])


def test_compact_engineering_summary_cannot_make_up_human_inputs():
    compact = json.loads(
        (ROOT / "docs/validation/notification-production-current.json").read_text()
    )
    with pytest.raises(ReviewError, match="snapshot_incomplete"):
        prepare_review({"compact": compact})


def test_cli_new_records_real_files_refuses_overwrite_and_help_has_no_side_effect(
    bundle, tmp_path, capsys
):
    main = runpy.run_path(str(ENTRY))["main"]
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0 and list(tmp_path.iterdir()) == []
    capsys.readouterr()
    source = tmp_path / "bundle.json"
    source.write_text(json.dumps(bundle))
    assert main(["report", "--bundle", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 46
    output = tmp_path / "label.json"
    args = [
        "label",
        "--bundle",
        str(source),
        "--item-id",
        "search:S11",
        "--reviewer",
        "test",
        "--at",
        AT,
        "--relevant-id",
        "--output",
        str(output),
    ]
    assert main(args) == 0
    capsys.readouterr()
    before = output.read_bytes()
    assert main(args) == 2 and output.read_bytes() == before
    assert "unavailable_or_exists" in capsys.readouterr().err
    assert main(["report", "--bundle", str(source), "--labels", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["confirmed"] == 1 and report["pending"] == 45
    assert source.read_text() == json.dumps(bundle)


def test_cli_invalid_missing_files_have_finite_errors(tmp_path, capsys):
    main = runpy.run_path(str(ENTRY))["main"]
    path = tmp_path / "bundle.json"
    assert main(["report", "--bundle", str(path)]) == 2
    assert "file_unavailable" in capsys.readouterr().err
    path.write_text("not json")
    assert main(["report", "--bundle", str(path)]) == 2
    assert "file_invalid" in capsys.readouterr().err

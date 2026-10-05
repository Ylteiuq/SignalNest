"""Real POSIX SIGKILL/restart experiments; HTTP stays synthetic and fully offline.

Unlike raising an injected exception, SIGKILL cannot run finally/rollback/finalization.
These checks establish process-interruption behavior, not power-loss durability.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from signalnest.contracts import ListPage, PageInput
from signalnest.ingestion import ResponseInput, discover_page, import_page
from signalnest.instance_lock import writer_lock
from signalnest.parsing import parse_list
from signalnest.schema import (
    documents,
    http_resources,
    ingestion_runs,
    notice_versions,
    raw_responses,
    source_ingestion_state,
)

WORKER = Path(__file__).parent / "helpers/crawl_recovery_worker.py"
FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
SOURCE = "whu-undergrad-student"


def seed_success(env):
    page = parse_list(
        PageInput(
            content=(FIXTURES / "student-notices-page1.html").read_bytes(),
            page_url="https://uc.whu.edu.cn/tzgg/xstz.htm",
        )
    )
    entry = next(entry for entry in page.entries if entry.source_document_id == "1517:128231")
    discover_page(
        env.engine,
        SOURCE,
        ListPage(entries=(entry,), next_page_url=page.next_page_url, pagination=page.pagination),
        100,
    )
    evidence = ResponseInput(
        page_type="notice",
        source_id=SOURCE,
        source_document_id=entry.source_document_id,
        requested_url=entry.detail_url,
        final_url=entry.detail_url,
        fetched_at=101,
        status_code=200,
    )
    import_page(
        env.engine, env.store, evidence, (FIXTURES / "current-notice-detail.html").read_bytes(), 102
    )
    # Keep the other first-processing item out of the measured A -> B transaction.
    # It becomes due before the independent recovery process starts at epoch 2000.
    with env.engine.begin() as connection:
        connection.execute(
            documents.insert().values(
                source_id=SOURCE,
                source_document_id="1517:127581",
                detail_url="https://uc.whu.edu.cn/info/1517/127581.htm",
                discovered_title="待处理通知",
                discovered_at=100,
                next_due_at=1500,
            )
        )
    return rows(env, notice_versions)[0]["id"]


def command(env, marker, boundary, run_id, epoch):
    return [
        sys.executable,
        str(WORKER),
        str(env.settings.database),
        str(env.settings.data_dir),
        str(marker),
        boundary,
        run_id,
        str(epoch),
    ]


def wait_for_boundary(process, marker):
    deadline = time.monotonic() + 10
    while not marker.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"Worker exited before boundary: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            pytest.fail("Worker did not reach the measured boundary within 10 seconds")
        time.sleep(0.01)
    return json.loads(marker.read_text(encoding="utf-8"))


def rows(env, table):
    with env.engine.connect() as connection:
        return connection.execute(sa.select(table)).mappings().all()


def resume(env, marker, run_id, epoch):
    process = subprocess.run(
        command(env, marker, "none", run_id, epoch),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


@pytest.mark.skipif(os.name != "posix", reason="Requires actual POSIX SIGKILL and advisory locks")
@pytest.mark.parametrize(
    "boundary,existing_success",
    [
        ("raw_registered", False),
        ("business_precommit", False),
        ("scan_precoverage", False),
        ("run_prefinish", False),
        ("business_precommit", True),
    ],
)
def test_sigkill_preserves_evidence_and_restart_rebuilds_work(
    state_env, tmp_path, boundary, existing_success
):
    env = state_env
    old_version = seed_success(env) if existing_success else None
    marker = tmp_path / "boundary.json"
    process = subprocess.Popen(
        command(env, marker, boundary, "killed-run", 1000),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        reached = wait_for_boundary(process, marker)
        assert reached["boundary"] == boundary
        if boundary == "business_precommit":
            # The signal measures all writes inside the actual open business transaction.
            assert reached["version_count"] == 1 + existing_success
            assert reached["current_version_id"] is not None
            assert reached["current_version_id"] != old_version
            assert reached["next_due_at"] > 1000
            assert reached["resource_processed_response_id"] is not None
        elif boundary == "scan_precoverage":
            assert reached["attested_pages"] == 2
        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    # No child cleanup code ran: the kernel released the actual same-instance write lock.
    with writer_lock(env.settings.database):
        pass
    old_run = rows(env, ingestion_runs)[0]
    state = rows(env, source_ingestion_state)[0]
    assert old_run["result"] == "running" and old_run["finished_at"] is None
    if boundary == "scan_precoverage":
        assert old_run["coverage"] == "pending"
        assert state["last_complete_scan_at"] is None
    else:
        assert old_run["coverage"] == "complete"
        assert state["last_complete_scan_run_id"] == "killed-run"

    discovered = rows(env, documents)
    assert len(discovered) == 2
    if existing_success:
        assert len(rows(env, notice_versions)) == 1
        first = next(row for row in discovered if row["source_document_id"] == "1517:128231")
        assert first["current_version_id"] == old_version and first["status"] == "processed"
        # Enrollment is already independently committed; the success due update rolled back.
        assert first["next_due_at"] == 1000 and first["last_success_at"] == 102
        second = next(row for row in discovered if row["source_document_id"] == "1517:127581")
        assert second["current_version_id"] is None
    elif boundary != "run_prefinish":
        # An interrupted commit must not publish any version, success pointer, due or mark.
        assert not rows(env, notice_versions)
        assert all(document["status"] == "discovered" for document in discovered)
        assert all(document["current_version_id"] is None for document in discovered)
        assert all(document["next_due_at"] is None for document in discovered)
    else:
        assert len(rows(env, notice_versions)) == 2
        assert all(document["status"] == "processed" for document in discovered)

    before = rows(env, raw_responses)
    notice_bodies = [row for row in before if row["page_type"] == "notice"]
    if boundary in {"raw_registered", "business_precommit"}:
        assert len(notice_bodies) == 1 + existing_success
        notice_evidence = notice_bodies[-1]
        assert notice_evidence["status_code"] == 200 and notice_evidence["last_attempt_at"] is None
        resource = next(
            row for row in rows(env, http_resources) if row["id"] == notice_evidence["resource_id"]
        )
        assert resource["latest_response_id"] == notice_evidence["id"]
        assert resource["last_processed_response_id"] is None
    for evidence in before:
        if evidence["body_path"] is not None:
            raw = env.store.read(evidence["body_path"], evidence["body_sha256"])
            assert hashlib.sha256(raw).hexdigest() == evidence["body_sha256"]

    restored = resume(env, marker, "recovered-run", 2000)
    summary = restored["summary"]
    assert summary["result"] == "succeeded" and summary["coverage"] == "complete"
    assert summary["new_documents"] == 0
    assert summary["remaining_due"] == summary["remaining_unprocessed"] == 0
    assert summary["details_succeeded"] == (0 if boundary == "run_prefinish" else 2)
    runs = {run["id"]: run for run in rows(env, ingestion_runs)}
    assert runs["killed-run"]["result"] == "interrupted"
    assert runs["killed-run"]["error_code"] == "previous_run_unfinished"
    assert runs["killed-run"]["coverage"] == (
        "interrupted" if boundary == "scan_precoverage" else "complete"
    )
    assert runs["recovered-run"]["result"] == "succeeded"
    if existing_success:
        first = next(
            row for row in rows(env, documents) if row["source_document_id"] == "1517:128231"
        )
        assert first["current_version_id"] != old_version
        current = next(
            row for row in rows(env, notice_versions) if row["id"] == first["current_version_id"]
        )
        assert current["raw_response_id"] == notice_evidence["id"]
    after = rows(env, raw_responses)
    if boundary in {"raw_registered", "business_precommit"}:
        reused = next(
            row
            for row in after
            if row["page_type"] == "notice"
            and row["validated_response_id"] == notice_evidence["id"]
        )
        assert reused["status_code"] == 304 and reused["body_path"] is None
        unchanged = next(row for row in after if row["id"] == notice_evidence["id"])
        assert unchanged["fetched_at"] == notice_evidence["fetched_at"]
        assert unchanged["last_attempt_at"] >= 2000

    # A third independent process actually rechecks due details using bound originals.
    again = resume(env, marker, "repeat-run", 100000)
    assert again["summary"]["details_succeeded"] == 2
    assert again["summary"]["new_documents"] == 0
    assert all(request["conditional"] for request in again["requests"])
    assert len(rows(env, documents)) == 2
    assert len(rows(env, notice_versions)) == 2 + existing_success
    assert all(row["status"] == "processed" for row in rows(env, documents))
    assert len(tuple((env.settings.data_dir / "raw").glob("*.bin"))) == 4 + existing_success

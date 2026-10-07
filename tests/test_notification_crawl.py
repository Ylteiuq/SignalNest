"""Activated N1 through the production coordinator, using synthetic HTTP only."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_crawling import HOME, SOURCE, Clock, fixture, html, listing, rows, run

from signalnest.crawling import CrawlOptions
from signalnest.errors import IngestError
from signalnest.notifications.contracts import Profile
from signalnest.notifications.state import ActivationOptions, activate_notifications
from signalnest.schema import (
    documents,
    ingestion_runs,
    notification_events,
    notification_listing_evidence,
    notification_observations,
    raw_responses,
)

NOTICE = "https://uc.whu.edu.cn/info/1517/128231.htm"
AT = int(datetime(2026, 9, 4, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
LIST = listing(current=1, total=1).replace(b"2026-09-24", b"2026-09-04")


def clock(at):
    value = Clock()
    value.epoch = at
    return value


def activate_after_full(env):
    first = run(
        env,
        lambda request: html(LIST),
        clock=clock(AT - 100),
        opts=CrawlOptions(scan_mode="full", max_pages=1, max_details=0),
    )
    assert first.coverage == "complete" and first.result == "succeeded"
    assert rows(env, notification_events) == []
    activate_notifications(
        env.engine,
        SOURCE,
        Profile(interest_topics=("course_enrollment",)),
        ActivationOptions(
            activation_id="crawl-activation",
            sender="sender@example.org",
            recipient="me@example.org",
        ),
        at=AT - 50,
    )


def handler(request):
    if "If-None-Match" in request.headers:
        return httpx.Response(304)
    return html(LIST if str(request.url) == HOME else fixture("current-notice-detail.html"))


def test_actual_coordinator_supplies_live_provenance_and_304_observation(state_env):
    env = state_env
    activate_after_full(env)
    opts = CrawlOptions(scan_mode="limited", max_pages=1, max_details=1)
    second = run(env, handler, clock=clock(AT), opts=opts)
    assert second.details_succeeded == 1
    event = rows(env, notification_events)[0]
    assert event["kind"] == "activation_recent"
    listing_evidence = rows(env, notification_listing_evidence)[0]
    assert listing_evidence["processing_origin"] == "live"
    initial = rows(env, notification_observations)[0]
    due = rows(env, documents)[0]["next_due_at"]
    third = run(env, handler, clock=clock(due + 1), opts=opts)
    assert third.details_succeeded == 1 and len(rows(env, notification_events)) == 1
    current = rows(env, notification_observations)[0]
    assert current["body_response_id"] == initial["body_response_id"]
    assert current["observed_response_id"] != initial["observed_response_id"]
    observed = next(
        r for r in rows(env, raw_responses) if r["id"] == current["observed_response_id"]
    )
    assert observed["status_code"] == 304 and observed["body_path"] is None
    assert observed["validated_response_id"] == current["body_response_id"]
    assert (
        len(
            [
                r
                for r in rows(env, raw_responses)
                if r["page_type"] == "notice" and r["status_code"] == 200
            ]
        )
        == 1
    )


def test_notification_registration_failure_terminates_coordinator_and_rolls_back_success(state_env):
    env = state_env
    activate_after_full(env)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_notification BEFORE INSERT ON notification_events "
            "BEGIN SELECT RAISE(ABORT, 'injected notification failure'); END"
        )
    with pytest.raises(IngestError, match="database_write_failed"):
        run(
            env,
            handler,
            clock=clock(AT),
            opts=CrawlOptions(scan_mode="limited", max_pages=1, max_details=1),
        )
    assert rows(env, notification_events) == []
    assert rows(env, notification_observations) == []
    doc = rows(env, documents)[0]
    assert doc["current_version_id"] is None and doc["status"] == "failed"
    assert doc["last_error_code"] == "database_write_failed"
    last_run = max(rows(env, ingestion_runs), key=lambda r: r["started_at"])
    assert last_run["result"] == "failed" and last_run["error_code"] == "database_write_failed"
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER fail_notification")
        connection.execute(documents.update().values(next_due_at=AT + 100))
    resumed = run(
        env,
        handler,
        clock=clock(AT + 101),
        opts=CrawlOptions(scan_mode="limited", max_pages=1, max_details=1),
    )
    assert resumed.details_succeeded == 1 and len(rows(env, notification_events)) == 1
    assert rows(env, notification_events)[0]["kind"] == "activation_recent"

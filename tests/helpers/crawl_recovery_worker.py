"""Offline crawl worker paused at measured boundaries for a real parent SIGKILL.

This is a test harness, not a recovery implementation. Production services perform all
archive/SQLite/Parser work. Hooks only report a reached boundary and block until killed.
"""

import argparse
import json
import os
import signal
import socket
from pathlib import Path

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from signalnest import crawling
from signalnest.config import Settings
from signalnest.crawling import CrawlOptions, crawl_once
from signalnest.schema import documents, http_resources, notice_versions

HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
SECOND = "https://uc.whu.edu.cn/tzgg/xstz/23.htm"
SOURCE = "whu-undergrad-student"
FIXTURES = Path(__file__).resolve().parents[2] / "research/fixtures"


class Clock:
    def __init__(self, epoch):
        self.epoch, self.elapsed = epoch, 0.0

    def time(self):
        return self.epoch + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


def forbidden(*args, **kwargs):
    raise AssertionError("Recovery subprocess must stay offline")


def listing(current):
    numbers = (
        f'<span class="p_no_d">1</span><span class="p_no"><a href="{SECOND}">2</a></span>'
        if current == 1
        else f'<span class="p_no"><a href="{HOME}">1</a></span><span class="p_no_d">2</span>'
    )
    controls = (
        f'<span class="p_next"><a href="{SECOND}">下页</a></span>'
        f'<span class="p_last"><a href="{SECOND}">尾页</a></span>'
        if current == 1
        else '<span class="p_next_d">下页</span><span class="p_last_d">尾页</span>'
    )
    identity = 128231 if current == 1 else 127581
    return (
        '<div><div class="list_txt"><ul class="am-list">'
        f'<li><a href="/info/1517/{identity}.htm"><span>通知{identity}</span>'
        '<i>2026-09-24</i></a></li></ul></div><div class="page">'
        f'<div class="p_pages">{numbers}{controls}</div></div></div>'
    ).encode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("signal_file", type=Path)
    parser.add_argument("boundary")
    parser.add_argument("run_id")
    parser.add_argument("epoch", type=int)
    args = parser.parse_args()
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden

    def pause(payload):
        # A parent sees only a complete marker; this is not a power-loss experiment.
        temporary = args.signal_file.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(args.signal_file)
        while True:
            signal.pause()

    original_record = crawling.record_response

    def record_response(*values, **options):
        response_id = original_record(*values, **options)
        if args.boundary == "raw_registered" and values[2].page_type == "notice":
            pause({"boundary": args.boundary, "response_id": response_id})
        return response_id

    crawling.record_response = record_response

    if args.boundary == "business_precommit":

        @sa.event.listens_for(Engine, "after_execute")
        def after_execute(connection, clause, multiparams, params, execution_options, result):
            if (
                isinstance(clause, sa.sql.dml.Update)
                and clause.table.name == "http_resources"
                and "last_processed_response_id" in str(clause)
            ):
                # The notice update follows the version and success-pointer writes.
                document = (
                    connection.execute(
                        sa.select(documents).where(documents.c.source_document_id == "1517:128231")
                    )
                    .mappings()
                    .one()
                )
                if document["status"] == "processed":
                    resource = (
                        connection.execute(
                            sa.select(http_resources).where(
                                http_resources.c.request_uri == document["detail_url"]
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if resource is not None and resource["last_processed_response_id"] is not None:
                        connection.info["recovery_boundary"] = {
                            "boundary": args.boundary,
                            "version_count": connection.scalar(
                                sa.select(sa.func.count()).select_from(notice_versions)
                            ),
                            "current_version_id": document["current_version_id"],
                            "next_due_at": document["next_due_at"],
                            "resource_processed_response_id": resource[
                                "last_processed_response_id"
                            ],
                        }

        @sa.event.listens_for(Engine, "commit")
        def before_commit(connection):
            if payload := connection.info.pop("recovery_boundary", None):
                pause(payload)

    original_coverage = crawling.record_coverage_in_transaction

    def record_coverage(*values, **options):
        if args.boundary == "scan_precoverage":
            assert values[2] == "complete"
            assert options["completion"] is not None
            pause({"boundary": args.boundary, "attested_pages": len(options["completion"].pages)})
        return original_coverage(*values, **options)

    crawling.record_coverage_in_transaction = record_coverage
    original_finish = crawling.finish_run_in_transaction

    def finish_run(*values, **options):
        if args.boundary == "run_prefinish":
            assert values[2] == "succeeded"
            pause({"boundary": args.boundary})
        return original_finish(*values, **options)

    crawling.finish_run_in_transaction = finish_run
    requests = []

    def handler(request):
        uri = str(request.url)
        requests.append({"uri": uri, "conditional": "If-None-Match" in request.headers})
        if "If-None-Match" in request.headers:
            return httpx.Response(304)
        if uri == HOME:
            body = listing(1)
        elif uri == SECOND:
            body = listing(2)
        else:
            body = (
                FIXTURES
                / ("current-notice-detail.html" if "128231" in uri else "legacy-notice-detail.html")
            ).read_bytes()
            if "128231" in uri:
                # A meaningful in-memory revision supports testing rollback to old success.
                body = body.replace(
                    "武大本函〔2026〕129号".encode(), "武大本函〔2026〕130号".encode()
                )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html", "ETag": '"recovery-stable"'},
            stream=httpx.ByteStream(body),
        )

    summary = crawl_once(
        Settings(
            storage={"database": str(args.database), "data_dir": str(args.data_dir)},
            source={"id": SOURCE, "list_url": HOME},
            http={
                "connect_timeout_seconds": 2.0,
                "read_timeout_seconds": 3.0,
                "request_interval_seconds": 1.0,
                "user_agent": "SignalNest/process-recovery-test",
            },
        ),
        CrawlOptions(scan_mode="full", max_pages=2, max_details=2),
        transport=httpx.MockTransport(handler),
        clock=Clock(args.epoch),
        run_id=args.run_id,
    )
    print(
        json.dumps({"summary": summary.model_dump(mode="json"), "requests": requests}),
        flush=True,
    )


if __name__ == "__main__":
    main()

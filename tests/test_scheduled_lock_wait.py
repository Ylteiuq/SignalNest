"""Finite scheduled-full lock recovery with actual SQLite/Parser and offline HTTP.

Real subprocess contention is exercised separately from fake monotonic budget tests.
Neither test category proves systemd/Windows recovery or power-loss behavior.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_crawling import HOME, Clock, html, listing, rows, settings

from signalnest.cli import main
from signalnest.config import RuntimeSettings, load_config
from signalnest.crawling import CrawlOptions, crawl_once
from signalnest.fetching import FetchLimits
from signalnest.instance_lock import WriterLockError, writer_lock
from signalnest.mail.background import run_scheduled_cycle
from signalnest.schema import documents, ingestion_runs, raw_responses

ROOT = Path(__file__).resolve().parents[1]


def opts(*, wait=2.0, seconds=3.0):
    return CrawlOptions(
        scan_mode="full",
        max_pages=2,
        max_details=0,
        lock_wait_seconds=wait,
        fetch_limits=FetchLimits(run_seconds=seconds, max_requests=4),
    )


class ReleaseClock(Clock):
    def __init__(self, release):
        super().__init__()
        self.release = release

    def sleep(self, seconds):
        super().sleep(seconds)
        if self.elapsed >= 1.0 and self.release is not None:
            self.release()
            self.release = None


def configuration(env, tmp_path, *, wait=2.0, seconds=3.0, mail=False):
    path = tmp_path / "scheduled.toml"
    text = f"""
[storage]
data_dir = {json.dumps(str(env.settings.data_dir))}
database = {json.dumps(str(env.settings.database))}
[source]
id = "whu-undergrad-student"
list_url = "{HOME}"
[http]
connect_timeout_seconds = 1.0
read_timeout_seconds = 1.0
request_interval_seconds = 0.01
user_agent = "SignalNest/offline-lock-test"
[runtime]
full_lock_wait_seconds = {wait}
[runtime.full]
max_pages = 2
max_details = 0
max_requests = 4
run_seconds = {seconds}
resource_seconds = 1.0
[runtime.regular]
max_pages = 1
max_details = 0
max_requests = 4
run_seconds = {seconds}
resource_seconds = 1.0
"""
    if mail:
        text += """
[smtp]
host = "smtp.example.org"
port = 587
[mail_runtime]
enabled = true
"""
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("wait", [-1, 601, float("inf"), float("nan"), True, "1"])
def test_config_and_options_reject_unbounded_or_coerced_waits(wait):
    with pytest.raises(ValidationError):
        RuntimeSettings(full_lock_wait_seconds=wait)
    with pytest.raises(ValidationError):
        CrawlOptions(lock_wait_seconds=wait)


def test_default_policy_is_bounded_and_manual_options_remain_immediate():
    assert RuntimeSettings().full_lock_wait_seconds == 180.0
    assert RuntimeSettings(full_lock_wait_seconds=0.0).full_lock_wait_seconds == 0.0
    assert CrawlOptions().lock_wait_seconds == 0.0


def test_wait_consumes_same_collection_budget_and_preserves_request_count(state_env):
    holder = writer_lock(state_env.settings.database)
    holder.__enter__()
    released = False

    def release():
        nonlocal released
        holder.__exit__(None, None, None)
        released = True

    clock, sent = ReleaseClock(release), []

    def handler(request):
        sent.append(clock.monotonic())
        assert released and str(request.url) == HOME
        return html(listing(total=1))

    try:
        result = crawl_once(
            settings(state_env),
            opts(wait=2.0, seconds=1.5),
            clock=clock,
            transport=httpx.MockTransport(handler),
        )
    finally:
        if not released:
            release()
    # 1 second waited, leaving only .5 seconds. The existing 1-second request
    # interval prevents the independent home recheck, rather than resetting time.
    assert sent == [1.0] and result.physical_requests == 1
    assert clock.elapsed == 1.0  # The fetcher rejects a wait exceeding the remaining .5 seconds.
    assert result.coverage == "limited" and not result.home_rechecked
    assert result.error_code == "run_time_limit"
    assert len(rows(state_env, ingestion_runs)) == 1
    assert len(rows(state_env, documents)) == len(rows(state_env, raw_responses)) == 1


@pytest.mark.parametrize(
    "wait,seconds,code,elapsed",
    [(0.5, 2.0, "writer_lock_timeout", 0.5), (2.0, 0.5, "writer_lock_budget_exhausted", 0.5)],
)
def test_timeout_has_no_engine_run_archive_or_http(
    state_env, monkeypatch, wait, seconds, code, elapsed
):
    from signalnest import crawling

    clock = Clock()
    files = sorted(state_env.settings.data_dir.rglob("*"))
    monkeypatch.setattr(
        crawling,
        "open_initialized_engine",
        lambda *args, **kwargs: pytest.fail("must not open the database before acquiring lock"),
    )
    with writer_lock(state_env.settings.database), pytest.raises(WriterLockError, match=code):
        crawl_once(
            settings(state_env),
            opts(wait=wait, seconds=seconds),
            clock=clock,
            transport=httpx.MockTransport(lambda request: pytest.fail("must not request")),
        )
    assert clock.elapsed == elapsed
    assert rows(state_env, ingestion_runs) == rows(state_env, raw_responses) == []
    assert sorted(state_env.settings.data_dir.rglob("*")) == files


def test_setup_time_cannot_reset_absolute_deadline_or_create_run(state_env, monkeypatch):
    from signalnest import crawling

    clock, actual = Clock(), crawling.open_initialized_engine

    def slow_open(database):
        clock.sleep(2.0)
        return actual(database)

    monkeypatch.setattr(crawling, "open_initialized_engine", slow_open)
    from signalnest.errors import IngestError

    with pytest.raises(IngestError, match="run_time_limit"):
        crawl_once(
            settings(state_env),
            opts(seconds=1.0),
            clock=clock,
            transport=httpx.MockTransport(lambda request: pytest.fail("setup exhausted budget")),
        )
    assert rows(state_env, ingestion_runs) == rows(state_env, raw_responses) == []


@pytest.mark.parametrize("mail", [False, True])
@pytest.mark.parametrize("mode", ["regular", "full"])
def test_cli_scheduled_modes_select_wait_and_preserve_summary_shape(
    state_env, tmp_path, monkeypatch, capsys, mail, mode
):
    from signalnest import crawling
    from signalnest.mail import background

    path = configuration(state_env, tmp_path, mail=mail)
    actual, observed = crawling.crawl_once, []

    def offline(configuration, options, *, run_id=None, **kwargs):
        observed.append(options)
        return actual(
            configuration,
            options,
            run_id=run_id,
            clock=Clock(),
            transport=httpx.MockTransport(lambda request: html(listing(total=1))),
        )

    monkeypatch.setattr(crawling, "crawl_once", offline)
    monkeypatch.setattr(background, "crawl_once", offline)
    monkeypatch.setattr(
        background, "run_mail_pass", lambda *args, **kwargs: {"needs_attention": False}
    )
    assert main(["scheduled-run", "--config", str(path), "--mode", mode]) == 0
    assert observed[0].lock_wait_seconds == (2.0 if mode == "full" else 0.0)
    result = json.loads(capsys.readouterr().out)
    assert ("collection" in result) is mail
    collection = result["collection"] if mail else result
    assert collection["coverage"] == ("complete" if mode == "full" else "limited")


def test_enabled_scheduled_full_budget_failure_does_not_continue_to_mail(
    state_env, tmp_path, monkeypatch
):
    from signalnest.mail import background

    path = configuration(state_env, tmp_path, wait=2.0, seconds=0.5, mail=True)
    clock = Clock()
    monkeypatch.setattr(
        background, "run_mail_pass", lambda *args, **kwargs: pytest.fail("no SMTP continuation")
    )
    with (
        writer_lock(state_env.settings.database),
        pytest.raises(WriterLockError, match="writer_lock_budget_exhausted"),
    ):
        run_scheduled_cycle(
            load_config(path),
            "full",
            clock=clock,
            transport=httpx.MockTransport(lambda request: pytest.fail("locked")),
        )
    assert rows(state_env, ingestion_runs) == []


def test_zero_wait_full_is_fail_fast_and_cli_reports_finite_error(state_env, tmp_path, capsys):
    path = configuration(state_env, tmp_path, wait=0.0)
    with writer_lock(state_env.settings.database):
        assert main(["scheduled-run", "--config", str(path), "--mode", "full"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "writer_lock_busy" in output.err
    assert rows(state_env, ingestion_runs) == []


@pytest.mark.parametrize("command", ["manual", "regular", "mail"])
def test_other_writing_commands_remain_immediate_even_with_full_wait_config(
    state_env, tmp_path, capsys, command
):
    path = configuration(state_env, tmp_path, wait=180.0, mail=command == "mail")
    args = {
        "manual": ["crawl-once", "--config", str(path), "--scan", "full"],
        "regular": ["scheduled-run", "--config", str(path), "--mode", "regular"],
        "mail": ["scheduled-mail", "--config", str(path)],
    }[command]
    with writer_lock(state_env.settings.database):
        assert main(args) == 1
    output = capsys.readouterr()
    assert output.out == "" and "writer_lock_busy" in output.err
    assert "writer_lock_wait_started" not in output.err
    assert rows(state_env, ingestion_runs) == rows(state_env, raw_responses) == []


WORKER = """
import pathlib, socket, sys
import httpx
from signalnest import crawling
from signalnest.cli import main
def forbidden(*args, **kwargs):
    raise AssertionError('subprocess must be offline')
socket.socket.connect = forbidden
socket.create_connection = forbidden
body = pathlib.Path(sys.argv[2]).read_bytes()
marker = pathlib.Path(sys.argv[3])
original = crawling.crawl_once
def handler(request):
    with marker.open('a') as stream:
        stream.write('request\\n')
    return httpx.Response(200, headers={'Content-Type': 'text/html'}, stream=httpx.ByteStream(body))
def offline(settings, options, *, run_id=None):
    return original(settings, options, run_id=run_id, transport=httpx.MockTransport(handler))
crawling.crawl_once = offline
raise SystemExit(main(['scheduled-run', '--config', sys.argv[1], '--mode', 'full']))
"""


@pytest.mark.skipif(os.name != "posix", reason="real POSIX writer-lock processes")
@pytest.mark.parametrize("release_holder", [True, False])
def test_real_scheduled_process_waits_before_any_run_or_request(
    state_env, tmp_path, release_holder
):
    path = configuration(state_env, tmp_path, wait=1.0, seconds=3.0)
    body = tmp_path / "list.html"
    body.write_bytes(listing(total=1))
    marker = tmp_path / "requests.txt"
    holder = writer_lock(state_env.settings.database)
    holder.__enter__()
    worker = subprocess.Popen(
        [sys.executable, "-c", WORKER, str(path), str(body), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    held = True
    try:
        first_log = json.loads(worker.stderr.readline())
        assert first_log["event"] == "writer_lock_wait_started"
        assert first_log["source_id"] == "whu-undergrad-student" and first_log["run_id"]
        assert not marker.exists()
        assert rows(state_env, ingestion_runs) == rows(state_env, raw_responses) == []
        if release_holder:
            holder.__exit__(None, None, None)
            held = False
        output, errors = worker.communicate(timeout=10)
        if release_holder:
            assert worker.returncode == 0, errors
            result = json.loads(output)
            assert result["coverage"] == "complete" and result["home_rechecked"]
            assert marker.read_text().splitlines() == ["request", "request"]
            assert len(rows(state_env, ingestion_runs)) == 1
            assert len(rows(state_env, raw_responses)) == 2
            assert '"stage": "acquired"' in errors
        else:
            assert worker.returncode == 1 and output == ""
            assert "writer_lock_timeout" in errors
            assert not marker.exists() and rows(state_env, ingestion_runs) == []
    finally:
        if worker.poll() is None:
            worker.terminate()
        worker.communicate(timeout=10)
        if held:
            holder.__exit__(None, None, None)


@pytest.mark.skipif(os.name != "posix", reason="actual POSIX process termination")
@pytest.mark.parametrize("stop", ["terminate", "kill"])
def test_terminating_real_waiter_preserves_holder_and_next_process_can_continue(
    state_env, tmp_path, stop
):
    path = configuration(state_env, tmp_path, wait=3.0, seconds=4.0)
    body = tmp_path / "list.html"
    body.write_bytes(listing(total=1))
    marker = tmp_path / "requests.txt"
    holder = writer_lock(state_env.settings.database)
    holder.__enter__()
    held = True
    command = [sys.executable, "-c", WORKER, str(path), str(body), str(marker)]
    waiter = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert json.loads(waiter.stderr.readline())["event"] == "writer_lock_wait_started"
        assert not marker.exists() and rows(state_env, ingestion_runs) == []
        getattr(waiter, stop)()
        waiter.communicate(timeout=10)
        assert waiter.returncode < 0
        assert not marker.exists() and rows(state_env, raw_responses) == []
        with (
            pytest.raises(WriterLockError, match="writer_lock_busy"),
            writer_lock(state_env.settings.database),
        ):
            pytest.fail("killing the waiter must not release another process's lock")
        holder.__exit__(None, None, None)
        held = False
        next_process = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert next_process.returncode == 0, next_process.stderr
        result = json.loads(next_process.stdout)
        assert result["coverage"] == "complete" and result["home_rechecked"]
        assert marker.read_text().splitlines() == ["request", "request"]
        assert len(rows(state_env, ingestion_runs)) == 1
    finally:
        if waiter.poll() is None:
            waiter.kill()
        waiter.communicate(timeout=10)
        if held:
            holder.__exit__(None, None, None)

"""Explicit EMS local preview does not enable storage, collection or sending."""

import builtins
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

from signalnest.cli import main
from signalnest.contracts import NoticeContent

ROOT = Path(__file__).resolve().parents[1]
EMS_FILE = (
    ROOT / "research/fixtures/teaching-assistant" / "ems-notice-250571-20261008T102823953399Z.html"
)
UC_FILE = ROOT / "research/fixtures/current-notice-detail.html"
URL = "https://ems.whu.edu.cn/info/1588/250571.htm"
UC_URL = "https://uc.whu.edu.cn/info/1517/128231.htm"


def _profile(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text(
        "# EXAMPLE ONLY: fictional engineering profile.\n"
        'institution = "whu"\nrole = "student"\nstudy_level = "master"\n'
        'interest_topics = ["teaching_assistant"]\n',
        encoding="utf-8",
    )
    return path


def _args(profile, *, parser="ems-notices", path=EMS_FILE, url=URL):
    arguments = [
        "decision-preview",
        "--profile",
        str(profile),
        "--file",
        str(path),
        "--url",
        url,
        "--at",
        "2024-09-20T09:00:00+08:00",
        "--next-digest-at",
        "2024-09-21T09:00:00+08:00",
    ]
    if parser is not None:
        arguments += ["--parser", parser]
    return arguments


@pytest.mark.parametrize(
    "at,next_digest,action,route,time_status",
    [
        (
            "2024-09-20T09:00:00+08:00",
            "2024-09-21T09:00:00+08:00",
            "PUSH_NOW",
            "immediate",
            "open",
        ),
        (
            "2026-10-08T19:00:00+08:00",
            "2026-10-09T09:00:00+08:00",
            "IGNORE",
            "none",
            "closed",
        ),
    ],
)
def test_real_ems_local_preview_with_explicit_clock(
    tmp_path, capsys, at, next_digest, action, route, time_status
):
    arguments = _args(_profile(tmp_path))
    arguments[arguments.index("--at") + 1] = at
    arguments[arguments.index("--next-digest-at") + 1] = next_digest
    before = set(tmp_path.iterdir())
    assert main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    output = json.loads(captured.out)
    assert output["preview_only"] is True
    assert "助教选聘通知" in output["facts"]["title"]
    assert output["facts"]["deadline_at"] == "2024-09-20T16:00:00+08:00"
    assert "body_text" not in output["facts"]
    assert output["decision"]["action"] == action
    assert output["decision"]["effective_route"] == route
    assert output["decision"]["time_status"] == time_status
    assert output["decision"]["eligibility"] == "unknown"
    assert output["decision"]["needs_review"] is True
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("parser", [None, "whu-student-notices"])
def test_ems_input_requires_its_explicit_parser(tmp_path, capsys, parser):
    assert main(_args(_profile(tmp_path), parser=parser)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsupported_identity" in captured.err
    assert "未生成决策" in captured.err


@pytest.mark.parametrize(
    "path,url",
    [
        (EMS_FILE, UC_URL),
        (UC_FILE, UC_URL),
        (EMS_FILE, "https://ems.whu.edu.cn/system/resource/code/auth/caslogin.jsp"),
    ],
)
def test_ems_parser_rejects_other_source_or_login_identity(tmp_path, capsys, path, url):
    assert main(_args(_profile(tmp_path), path=path, url=url)) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "unsupported_identity" in captured.err
    assert "<html" not in captured.err and url not in captured.err


def test_unknown_parser_is_a_clear_argument_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        main(_args(_profile(tmp_path), parser="not-supported"))
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--parser" in captured.err and "invalid choice" in captured.err
    assert "ems-notices" in captured.err and "whu-student-notices" in captured.err


@pytest.mark.parametrize("parser", [None, "whu-student-notices", "ems-notices"])
def test_normalized_json_does_not_accept_nondefault_html_parser(tmp_path, capsys, parser):
    path = tmp_path / "notice.json"
    path.write_text(
        NoticeContent(
            title="课程助教招聘通知",
            published_date=date(2024, 9, 20),
            body_html="<p>即日起招募课程助教，申请截止2024年9月20日16:00。</p>",
            body_text="即日起招募课程助教，申请截止2024年9月20日16:00。",
        ).model_dump_json(),
        encoding="utf-8",
    )
    arguments = _args(_profile(tmp_path), parser=parser, path=path)
    arguments[arguments.index("--file")] = "--notice-json"
    url_position = arguments.index("--url")
    del arguments[url_position : url_position + 2]
    assert main(arguments) == (2 if parser == "ems-notices" else 0)
    captured = capsys.readouterr()
    if parser == "ems-notices":
        assert captured.out == ""
        assert "--parser ems-notices requires --file" in captured.err
        assert "--notice-json is already normalized" in captured.err
    else:
        assert captured.err == ""
        assert json.loads(captured.out)["preview_only"] is True


def test_default_uc_preview_never_imports_ems_parser(tmp_path, monkeypatch, capsys):
    original_import = builtins.__import__

    def import_without_ems(name, *args, **kwargs):
        if name == "signalnest.ems_parsing":
            raise AssertionError("The optional local EMS parser must remain lazy")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_ems)
    assert main(_args(_profile(tmp_path), parser=None, path=UC_FILE, url=UC_URL)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "新生选课" in json.loads(captured.out)["facts"]["title"]


def test_help_and_ems_preview_do_not_touch_storage_lock_or_network(tmp_path, monkeypatch, capsys):
    from signalnest import instance_lock, storage

    def forbidden(*args, **kwargs):
        raise AssertionError("Local preview must not access storage, a lock or HTTP")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(storage, "open_initialized_engine", forbidden)
    monkeypatch.setattr(storage, "initialize_storage", forbidden)
    monkeypatch.setattr(instance_lock, "writer_lock", forbidden)
    monkeypatch.setattr("signalnest.cli.load_config", forbidden)
    before = set(tmp_path.iterdir())
    with pytest.raises(SystemExit) as caught:
        main(["decision-preview", "--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr()
    assert help_text.err == "" and "--parser" in help_text.out
    assert "ems-notices" in help_text.out
    assert set(tmp_path.iterdir()) == before
    arguments = _args(_profile(tmp_path))
    before = set(tmp_path.iterdir())
    assert main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out)["preview_only"] is True
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("help_only", [False, True])
def test_real_subprocess_ems_preview_and_help_are_local(tmp_path, help_only):
    arguments = ["decision-preview", "--help"] if help_only else _args(_profile(tmp_path))
    before = set(tmp_path.iterdir())
    code = """
import socket
import sqlite3
import sys
import httpx
def forbidden(*args, **kwargs):
    raise AssertionError('Local preview must not use a network or database')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden
httpx.Client = forbidden
from signalnest.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, *arguments],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    if help_only:
        assert "--parser" in result.stdout and "ems-notices" in result.stdout
    else:
        assert json.loads(result.stdout)["decision"]["action"] == "PUSH_NOW"
    assert set(tmp_path.iterdir()) == before

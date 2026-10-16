"""Explicit snapshot launcher credentials remain local and never enter output or argv."""

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from test_query_agent import AT
from test_query_agent import campus as campus

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy/ask_saved.py"
MODULE = runpy.run_path(str(ENTRY))
main = MODULE["main"]
read_key = MODULE["_read_private_key"]
CredentialFileError = MODULE["CredentialFileError"]
ENVIRONMENT_NAME = "SIGNALNEST_SAVED_TEST_KEY"


def configure(campus):
    with campus.query_file.open("a") as stream:
        stream.write(
            '\n[model]\nprovider="openai-compatible-chat-completions"\n'
            'base_url="http://127.0.0.1:8001/v1"\nmodel="offline-test"\n'
            f'api_key_env="{ENVIRONMENT_NAME}"\npricing_mode="unpriced_proxy"\n'
        )


def private_file(tmp_path, raw=b"offline-placeholder\n", mode=0o600):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700, exist_ok=True)
    key = parent / "key"
    key.write_bytes(raw)
    key.chmod(mode)
    return key


@pytest.mark.parametrize("mode", [0o400, 0o600])
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_explicit_owner_only_plaintext_file(mode, ending, tmp_path):
    key = private_file(tmp_path, b"offline-placeholder" + ending, mode)
    assert read_key(key) == "offline-placeholder"


@pytest.mark.parametrize(
    "raw",
    [b"", b"\n", b" leading", b"trailing ", b"line\nline", b"\x00", b"\xff", b"x" * 4097],
)
def test_invalid_secret_has_only_finite_error(raw, tmp_path):
    key = private_file(tmp_path, raw)
    with pytest.raises(CredentialFileError, match="^credential_file_invalid$"):
        read_key(key)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700, 0o000])
def test_non_owner_only_file_mode_is_rejected(mode, tmp_path):
    key = private_file(tmp_path, mode=mode)
    with pytest.raises(CredentialFileError, match="credential_file_"):
        read_key(key)


def test_symlink_file_directory_fifo_and_shared_parent_are_rejected(tmp_path):
    key = private_file(tmp_path)
    link = key.parent / "link"
    link.symlink_to(key)
    with pytest.raises(CredentialFileError):
        read_key(link)
    directory_link = tmp_path / "linked-directory"
    directory_link.symlink_to(key.parent, target_is_directory=True)
    with pytest.raises(CredentialFileError):
        read_key(directory_link / key.name)
    fifo = key.parent / "pipe"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(CredentialFileError, match="credential_file_unsafe"):
        read_key(fifo)
    key.parent.chmod(0o777)
    with pytest.raises(CredentialFileError, match="credential_parent_unsafe"):
        read_key(key)


def test_owner_mismatch_rejected_without_reading(tmp_path, monkeypatch):
    key = private_file(tmp_path)
    actual = MODULE["os"].fstat

    def foreign_file(descriptor):
        info = actual(descriptor)
        if not MODULE["stat"].S_ISREG(info.st_mode):
            return info
        values = list(info)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(MODULE["os"], "fstat", foreign_file)
    with pytest.raises(CredentialFileError, match="credential_file_unsafe"):
        read_key(key)


@pytest.mark.parametrize("prior", [None, "existing-placeholder"])
@pytest.mark.parametrize("raises", [False, True])
def test_secret_only_present_during_cli_and_original_environment_restored(
    campus, tmp_path, monkeypatch, capsys, prior, raises
):
    configure(campus)
    key = private_file(tmp_path)
    if prior is None:
        monkeypatch.delenv(ENVIRONMENT_NAME, raising=False)
    else:
        monkeypatch.setenv(ENVIRONMENT_NAME, prior)
    calls = []

    def cli(arguments):
        assert os.environ[ENVIRONMENT_NAME] == "offline-placeholder"
        assert "offline-placeholder" not in arguments
        calls.append(arguments)
        if raises:
            raise RuntimeError("program defect")
        return 1

    monkeypatch.setitem(main.__globals__, "signalnest_main", cli)
    arguments = [
        "--config",
        str(campus.query_file),
        "--question",
        "助教招聘",
        "--at",
        AT.isoformat(),
        "--json",
        "--api-key-file",
        str(key),
    ]
    if raises:
        with pytest.raises(RuntimeError, match="program defect"):
            main(arguments)
    else:
        assert main(arguments) == 1
    assert os.environ.get(ENVIRONMENT_NAME) == prior
    assert calls[0] == [
        "ask",
        "--config",
        str(campus.query_file),
        "--question",
        "助教招聘",
        "--json",
        "--at",
        AT.isoformat(),
    ]
    output = capsys.readouterr()
    assert "offline-placeholder" not in output.out + output.err


@pytest.mark.parametrize("mode", ["local", "check", "help"])
def test_local_check_and_help_do_not_read_explicit_key(campus, monkeypatch, mode):
    def forbidden(path):
        raise AssertionError("non-model command must not read credentials")

    monkeypatch.setitem(main.__globals__, "_read_private_key", forbidden)
    arguments = [
        "--config",
        str(campus.query_file),
        "--api-key-file",
        "/does-not-exist/private-key",
    ]
    if mode == "help":
        with pytest.raises(SystemExit) as exited:
            main(["--help"])
        assert exited.value.code == 0
    elif mode == "check":
        assert main(arguments + ["--check"]) == 0
    else:
        assert main(arguments + ["--question", "助教招聘", "--local", "--json"]) == 0


def test_existing_environment_is_used_without_discovering_files(campus, monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_NAME, "already-configured-placeholder")

    def forbidden(path):
        raise AssertionError("no file lookup")

    def cli(arguments):
        assert os.environ[ENVIRONMENT_NAME] == "already-configured-placeholder"
        return 0

    monkeypatch.setitem(main.__globals__, "_read_private_key", forbidden)
    monkeypatch.setitem(main.__globals__, "signalnest_main", cli)
    assert main(["--config", str(campus.query_file), "--question", "科研"]) == 0


def test_invalid_question_or_config_does_not_read_key(campus, monkeypatch, capsys):
    configure(campus)

    def forbidden(path):
        raise AssertionError("validation must happen before credential read")

    monkeypatch.setitem(main.__globals__, "_read_private_key", forbidden)
    assert (
        main(
            [
                "--config",
                str(campus.query_file),
                "--question",
                "科研",
                "--at",
                "not-a-date",
                "--api-key-file",
                "/unused",
            ]
        )
        == 2
    )
    assert "query_configuration_invalid" in capsys.readouterr().err


def test_real_cli_local_use_and_help_never_open_key(campus, tmp_path):
    missing_key = tmp_path / "absent.key"
    result = subprocess.run(
        [
            sys.executable,
            str(ENTRY),
            "--config",
            str(campus.query_file),
            "--question",
            "助教招聘",
            "--local",
            "--json",
            "--api-key-file",
            str(missing_key),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert b'"status": "local"' in result.stdout
    assert not missing_key.exists()

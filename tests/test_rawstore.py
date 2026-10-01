"""Real files; injected I/O failures, not process termination/power-loss experiments."""

import hashlib
import os

import pytest

from signalnest.rawstore import RawStore, RawStoreError


@pytest.fixture
def store(tmp_path):
    (tmp_path / "raw").mkdir()
    return RawStore(tmp_path)


def test_archive_reuses_verified_bytes_and_reads(store):
    content = b"original\x00bytes\xff"
    body = store.archive(content)
    assert body.path == f"raw/{hashlib.sha256(content).hexdigest()}.bin"
    assert store.archive(content) == body
    assert store.read(body.path, body.sha256) == content
    assert [p.name for p in (store.data_dir / "raw").iterdir()] == [f"{body.sha256}.bin"]


def test_existing_corrupt_body_is_never_overwritten(store):
    body = store.archive(b"original")
    path = store.data_dir / body.path
    path.write_bytes(b"damaged")
    for operation in [
        lambda: store.archive(b"original"),
        lambda: store.read(body.path, body.sha256),
    ]:
        with pytest.raises(RawStoreError, match="raw_digest_mismatch"):
            operation()
    assert path.read_bytes() == b"damaged"


@pytest.mark.parametrize("path", ["../escape", "/tmp/escape", "raw/../escape", "raw/wrong.bin"])
def test_path_escape_rejected(store, path):
    with pytest.raises(RawStoreError, match="raw_path_invalid"):
        store.read(path, "a" * 64)


def test_missing_body_and_invalid_digest(store):
    with pytest.raises(RawStoreError, match="raw_missing"):
        store.read("raw/" + "a" * 64 + ".bin", "a" * 64)
    with pytest.raises(RawStoreError, match="raw_path_invalid"):
        store.read("raw/wrong.bin", "../")


@pytest.mark.parametrize("component", ["data_dir", "ancestor", "raw", "body"])
def test_symlinks_are_rejected(tmp_path, component):
    actual = tmp_path / "actual"
    (actual / "data" / "raw").mkdir(parents=True)
    store = RawStore(actual / "data")
    body = store.archive(b"original")
    if component == "data_dir":
        link = tmp_path / "linked-data"
        link.symlink_to(actual / "data", target_is_directory=True)
        store = RawStore(link)
    elif component == "ancestor":
        link = tmp_path / "linked-ancestor"
        link.symlink_to(actual, target_is_directory=True)
        store = RawStore(link / "data")
    elif component == "raw":
        directory = actual / "data" / "raw"
        directory.rename(actual / "saved-raw")
        directory.symlink_to(actual / "saved-raw", target_is_directory=True)
    else:
        path = store.data_dir / body.path
        saved = actual / "saved-body"
        path.rename(saved)
        path.symlink_to(saved)
    with pytest.raises(RawStoreError, match="raw_io_or_unsafe_path"):
        store.read(body.path, body.sha256)
    with pytest.raises(RawStoreError, match="raw_io_or_unsafe_path"):
        store.archive(b"original")


def test_non_regular_body_rejected(store):
    digest = hashlib.sha256(b"original").hexdigest()
    os.mkfifo(store.data_dir / "raw" / f"{digest}.bin")
    with pytest.raises(RawStoreError, match="raw_not_regular"):
        store.archive(b"original")


def test_publication_is_complete_and_exclusive(store, monkeypatch):
    real_link = os.link
    content = b"complete HTML"

    def inspect_publish(source, destination, **kwargs):
        assert not (store.data_dir / "raw" / destination).exists()
        assert (store.data_dir / "raw" / source).read_bytes() == content
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", inspect_publish)
    store.archive(content)
    assert len(list((store.data_dir / "raw").iterdir())) == 1


def test_exclusive_publish_race_verifies_existing_file(store, monkeypatch):
    def competing_publish(source, destination, **kwargs):
        (store.data_dir / "raw" / destination).write_bytes(b"damaged competing file")
        raise FileExistsError()

    monkeypatch.setattr(os, "link", competing_publish)
    with pytest.raises(RawStoreError, match="raw_digest_mismatch"):
        store.archive(b"original")
    assert len(list((store.data_dir / "raw").iterdir())) == 1


def test_fsync_failure_before_publish_leaves_no_final_file(store, monkeypatch):
    def fail_sync(descriptor):
        raise OSError("injected file sync failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(RawStoreError, match="raw_io_or_unsafe_path"):
        store.archive(b"original")
    assert not list((store.data_dir / "raw").iterdir())

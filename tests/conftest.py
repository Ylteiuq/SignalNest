"""All in-process tests are offline; subprocess tests exercise local CLI commands only."""

import socket
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def state_env(tmp_path):
    from signalnest.config import StorageSettings
    from signalnest.rawstore import RawStore
    from signalnest.storage import initialize_storage, open_initialized_engine

    settings = StorageSettings(
        data_dir=str(tmp_path / "data"), database=str(tmp_path / "db.sqlite")
    )
    initialize_storage(settings)
    engine = open_initialized_engine(settings.database)
    yield SimpleNamespace(settings=settings, engine=engine, store=RawStore(settings.data_dir))
    engine.dispose()

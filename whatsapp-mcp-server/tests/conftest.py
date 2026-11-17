"""Shared fixtures: per-account store folders, an accounts file and a fake bridge."""

import json
import sqlite3
from urllib.parse import urlsplit

import pytest
import requests

import accounts

MESSAGES_SCHEMA = """
    CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP, last_read_time TIMESTAMP);
    CREATE TABLE messages (
        id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
        media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB,
        file_length INTEGER, quoted_message_id TEXT, PRIMARY KEY (id, chat_jid)
    );
"""


@pytest.fixture(autouse=True)
def fresh_account_registry(monkeypatch):
    """Build the registry from each test's own environment, never the developer's."""
    monkeypatch.delenv(accounts.ACCOUNTS_FILE_ENV, raising=False)
    accounts.reset()
    yield
    accounts.reset()


@pytest.fixture
def account_store(tmp_path):
    """Create a bridge store folder: messages.db, whatsapp.db with the device row, .bridge-token."""

    def make(name, own_number=None, token=None, messages=True):
        store = tmp_path / name
        store.mkdir()
        if messages:
            conn = sqlite3.connect(store / "messages.db")
            conn.executescript(MESSAGES_SCHEMA)
            conn.commit()
            conn.close()
        if own_number:
            conn = sqlite3.connect(store / "whatsapp.db")
            conn.execute("CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, lid TEXT)")
            conn.execute(
                "INSERT INTO whatsmeow_device (jid, lid) VALUES (?, ?)",
                (f"{own_number}:7@s.whatsapp.net", "123456789012345:7@lid"),
            )
            conn.commit()
            conn.close()
        if token:
            (store / ".bridge-token").write_text(f"{token}\n", encoding="utf-8")
        return store

    return make


@pytest.fixture
def accounts_file(tmp_path, monkeypatch):
    """Write an accounts file and point the server at it."""

    def write(entries, raw=None):
        path = tmp_path / "accounts.json"
        path.write_text(raw if raw is not None else json.dumps(entries), encoding="utf-8")
        monkeypatch.setenv(accounts.ACCOUNTS_FILE_ENV, str(path))
        accounts.reset()
        return path

    return write


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="OK"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"success": True, "message": "sent"}
        self.text = text

    def json(self):
        return self._payload


class FakeBridge:
    """Stands in for every bridge's REST API; the URL's port says which account was reached.

    `ports` maps a port to the JSON its bridge answers with; a port that isn't
    listed refuses the connection like a bridge that isn't running.
    """

    def __init__(self):
        self.ports: dict[int, FakeResponse] = {}
        self.calls: list[dict] = []

    def _answer(self, method, url, json=None, headers=None, **_kwargs):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers or {}})
        port = urlsplit(url).port
        if port not in self.ports:
            raise requests.ConnectionError(f"Connection refused: {url}")
        return self.ports[port]

    def get(self, url, **kwargs):
        return self._answer("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, **kwargs)


@pytest.fixture
def fake_bridge(monkeypatch):
    bridge = FakeBridge()
    monkeypatch.setattr(requests, "get", bridge.get)
    monkeypatch.setattr(requests, "post", bridge.post)
    return bridge


def use_default_account(monkeypatch, **settings):
    """Configure the implicit default account through its single-account settings."""
    for name, value in settings.items():
        monkeypatch.setenv(name, str(value))
    accounts.reset()


def first_account():
    return accounts.registry().first()

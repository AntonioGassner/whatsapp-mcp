"""One MCP server, several WhatsApp accounts: the accounts file and list_accounts."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import accounts
import main
from tests.conftest import FakeResponse

CONNECTED = {"state": "connected", "connected": True, "paired": True, "message": "WhatsApp is connected."}


def test_list_accounts_reports_each_account_and_survives_an_unreachable_bridge(
    account_store, accounts_file, fake_bridge
):
    personal = account_store("personal", own_number="391111111111", token="personal-token")
    work = account_store("work", own_number="392222222222", token="work-token")
    accounts_file(
        [
            {"name": "personal", "store_dir": str(personal), "api_url": "http://localhost:8081/api"},
            {"name": "work", "store_dir": str(work), "api_url": "http://localhost:8082/api"},
        ]
    )
    fake_bridge.ports[8081] = FakeResponse(payload={**CONNECTED, "qrCode": "secret"})

    result = main.list_accounts()

    assert [entry["name"] for entry in result] == ["personal", "work"]
    personal_entry, work_entry = result
    assert personal_entry["phone_number"] == "391111111111"
    assert personal_entry["store_available"] is True
    assert personal_entry["bridge"] == {
        "reachable": True,
        "state": "connected",
        "connected": True,
        "paired": True,
        "message": "WhatsApp is connected.",
    }
    assert work_entry["phone_number"] == "392222222222"
    assert work_entry["store_available"] is True
    assert work_entry["bridge"]["reachable"] is False
    assert work_entry["bridge"]["connected"] is False
    assert "localhost:8082" in work_entry["bridge"]["error"]


def _entry(name, store, port):
    return {"name": name, "store_dir": str(store), "api_url": f"http://localhost:{port}/api"}


def test_each_bridge_call_uses_its_own_url_and_store_token(account_store, accounts_file, fake_bridge, monkeypatch):
    # The global override is for the implicit default account only.
    monkeypatch.setenv("WHATSAPP_BRIDGE_TOKEN", "global-override")
    accounts_file(
        [
            _entry("personal", account_store("personal", token="personal-token"), 8081),
            _entry("work", account_store("work", token="work-token"), 8082),
        ]
    )

    main.list_accounts()

    reached = {call["url"]: call["headers"] for call in fake_bridge.calls}
    assert reached == {
        "http://localhost:8081/api/connection": {"Authorization": "Bearer personal-token"},
        "http://localhost:8082/api/connection": {"Authorization": "Bearer work-token"},
    }


def test_existing_tools_act_on_the_first_account_until_they_take_one(account_store, accounts_file, fake_bridge):
    accounts_file(
        [
            _entry("personal", account_store("personal", token="personal-token"), 8081),
            _entry("work", account_store("work", token="work-token"), 8082),
        ]
    )
    fake_bridge.ports[8081] = FakeResponse()

    assert main.send_message("12025551234", "hello")["success"] is True

    assert [(call["url"], call["headers"]) for call in fake_bridge.calls] == [
        ("http://localhost:8081/api/send", {"Authorization": "Bearer personal-token"})
    ]


def test_account_with_missing_store_is_reported_without_failing(tmp_path, account_store, accounts_file, fake_bridge):
    accounts_file(
        [
            _entry("gone", tmp_path / "never-started", 8083),
            _entry("work", account_store("work", own_number="392222222222"), 8082),
        ]
    )
    fake_bridge.ports[8082] = FakeResponse(payload=CONNECTED)

    gone, work = main.list_accounts()

    assert gone["name"] == "gone"
    assert gone["phone_number"] is None
    assert gone["store_available"] is False
    assert gone["bridge"]["reachable"] is False
    assert work["store_available"] is True
    assert work["bridge"]["connected"] is True


def test_bridge_that_rejects_the_token_is_reachable_but_not_connected(account_store, accounts_file, fake_bridge):
    accounts_file([_entry("work", account_store("work", token="stale"), 8082)])
    fake_bridge.ports[8082] = FakeResponse(status_code=401, text="Unauthorized")

    (work,) = main.list_accounts()

    assert work["bridge"]["reachable"] is True
    assert work["bridge"]["connected"] is False
    assert "401" in work["bridge"]["error"]


def test_without_accounts_file_one_default_account_uses_the_single_account_settings(
    account_store, fake_bridge, monkeypatch
):
    store = account_store("store", own_number="393333333333", token="file-token")
    monkeypatch.setenv("WHATSAPP_DB_PATH", str(store / "messages.db"))
    monkeypatch.setenv("WHATSMEOW_DB_PATH", str(store / "whatsapp.db"))
    monkeypatch.setenv("WHATSAPP_API_URL", "http://localhost:8084/api")
    monkeypatch.setenv("WHATSAPP_BRIDGE_TOKEN", "global-override")
    fake_bridge.ports[8084] = FakeResponse(payload=CONNECTED)

    (default,) = main.list_accounts()

    assert default["name"] == "default"
    assert default["phone_number"] == "393333333333"
    assert default["store_available"] is True
    assert default["bridge"]["connected"] is True
    assert fake_bridge.calls[0]["url"] == "http://localhost:8084/api/connection"
    assert fake_bridge.calls[0]["headers"] == {"Authorization": "Bearer global-override"}


def test_relative_store_dir_is_relative_to_the_accounts_file(account_store, accounts_file, fake_bridge):
    account_store("personal", own_number="391111111111")
    accounts_file([{"name": "personal", "store_dir": "personal", "api_url": "http://localhost:8081/api"}])

    (personal,) = main.list_accounts()

    assert personal["phone_number"] == "391111111111"
    assert personal["store_available"] is True


VALID = {"name": "work", "store_dir": "work", "api_url": "http://localhost:8082/api"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, "Cannot read accounts file", id="unreadable"),
        pytest.param("{not json", "not valid JSON", id="not-json"),
        pytest.param("[]", "lists no accounts", id="empty-list"),
        pytest.param('{"name": "work"}', "JSON list of accounts", id="not-a-list"),
        pytest.param('["work"]', r"entry 1 must be a JSON object", id="entry-not-object"),
        pytest.param(
            json.dumps([VALID, {**VALID, "store_dir": "other"}]),
            r"entry 2 \('work'\) repeats the name 'work' of entry 1",
            id="duplicate-name",
        ),
        pytest.param(
            json.dumps([{**VALID, "name": "Work Phone"}]),
            r"entry 1 \('Work Phone'\) has an invalid name",
            id="invalid-name",
        ),
        pytest.param(
            json.dumps([{"name": "work", "api_url": VALID["api_url"]}]),
            r"entry 1 \('work'\) is missing store_dir",
            id="missing-store-dir",
        ),
        pytest.param(
            json.dumps([{"name": "work", "store_dir": "work"}]),
            r"entry 1 \('work'\) is missing api_url",
            id="missing-api-url",
        ),
        pytest.param(
            json.dumps([{"store_dir": "work", "api_url": VALID["api_url"]}]),
            r"entry 1 is missing name",
            id="missing-name",
        ),
        pytest.param(json.dumps([{**VALID, "api_url": "localhost:8082"}]), r"invalid api_url", id="bad-api-url"),
        pytest.param(json.dumps([{**VALID, "token": "secret"}]), r"unknown field\(s\) token", id="unknown-field"),
    ],
)
def test_invalid_accounts_file_is_refused_with_a_message_naming_the_problem(
    tmp_path, accounts_file, monkeypatch, raw, expected
):
    if raw is None:
        monkeypatch.setenv(accounts.ACCOUNTS_FILE_ENV, str(tmp_path / "missing.json"))
        accounts.reset()
    else:
        accounts_file(None, raw=raw)

    with pytest.raises(accounts.AccountsFileError, match=expected):
        main.list_accounts()


def test_invalid_accounts_file_stops_server_startup(tmp_path):
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(json.dumps([VALID, VALID]), encoding="utf-8")
    env = {**os.environ, accounts.ACCOUNTS_FILE_ENV: str(accounts_path)}

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=Path(main.__file__).parent,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "repeats the name 'work'" in result.stderr


def test_read_from_an_account_without_a_store_names_the_account(tmp_path, accounts_file):
    accounts_file([_entry("gone", tmp_path / "never-started", 8083)])

    with pytest.raises(FileNotFoundError, match="account 'gone'"):
        main.list_chats()

    assert not (tmp_path / "never-started").exists()

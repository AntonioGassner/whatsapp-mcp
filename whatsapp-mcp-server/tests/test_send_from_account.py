"""New messages go out from the account the caller names; with several accounts there is no default."""

import os

import pytest

import main
import whatsapp
from tests.conftest import FakeResponse, use_default_account

PERSONAL_NUMBER = "391000000001"
WORK_NUMBER = "392000000002"
RECIPIENT = "12025551234"


def _entry(name, store, port):
    return {"name": name, "store_dir": str(store), "api_url": f"http://localhost:{port}/api"}


@pytest.fixture
def two_accounts(account_store, accounts_file, fake_bridge):
    accounts_file(
        [
            _entry("personal", account_store("personal", own_number=PERSONAL_NUMBER, token="personal-token"), 8081),
            _entry("work", account_store("work", own_number=WORK_NUMBER, token="work-token"), 8082),
        ]
    )
    fake_bridge.ports[8081] = FakeResponse()
    fake_bridge.ports[8082] = FakeResponse()
    return fake_bridge


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4")
    return str(path)


@pytest.fixture
def converted_audio(tmp_path, monkeypatch):
    """Fake FFmpeg: a non-.ogg input becomes this temporary .ogg file."""
    src = tmp_path / "voice.mp3"
    src.write_bytes(b"not really mp3")
    converted = tmp_path / "converted.ogg"

    def fake_convert(path):
        converted.write_bytes(b"ogg")
        return str(converted)

    monkeypatch.setattr(whatsapp.audio, "convert_to_opus_ogg_temp", fake_convert)
    return src, converted


@pytest.fixture(params=["send_message", "send_message_quoted", "send_file", "send_audio_message"])
def send(request, media, converted_audio):
    """Each tool that sends a new message, called with only its account varying."""
    src, _converted = converted_audio
    return {
        "send_message": lambda **kw: main.send_message(RECIPIENT, "hello", **kw),
        # No store holds m1, so a reply names its account like a new message.
        "send_message_quoted": lambda **kw: main.send_message(RECIPIENT, "hello", quoted_message_id="m1", **kw),
        "send_file": lambda **kw: main.send_file(RECIPIENT, media, "Q3 numbers", **kw),
        "send_audio_message": lambda **kw: main.send_audio_message(RECIPIENT, str(src), **kw),
    }[request.param]


@pytest.mark.parametrize("account", [None, ""])
def test_send_without_account_fails_before_any_bridge_call(two_accounts, send, account):
    with pytest.raises(ValueError, match="valid accounts: personal, work"):
        send(account=account)

    assert two_accounts.calls == []


def test_send_with_unknown_account_is_refused_with_the_valid_names(two_accounts, send):
    with pytest.raises(ValueError, match="Unknown account 'home'; valid accounts: personal, work"):
        send(account="home")

    assert two_accounts.calls == []


@pytest.mark.parametrize(
    ("account", "port", "token"), [("personal", 8081, "personal-token"), ("work", 8082, "work-token")]
)
def test_send_reaches_only_the_named_accounts_bridge_with_its_token(two_accounts, send, account, port, token):
    send(account=account)

    assert [(call["url"], call["headers"]) for call in two_accounts.calls] == [
        (f"http://localhost:{port}/api/send", {"Authorization": f"Bearer {token}"})
    ]


@pytest.mark.parametrize(("account", "number"), [("personal", PERSONAL_NUMBER), ("work", WORK_NUMBER)])
def test_send_result_names_the_account_and_its_own_number(two_accounts, send, account, number):
    assert send(account=account) == {
        "success": True,
        "message": "sent",
        "account": account,
        "account_phone_number": number,
    }


def test_failed_send_still_names_the_account(two_accounts, media):
    two_accounts.ports[8082] = FakeResponse(payload={"success": False, "message": "not on WhatsApp"})

    assert main.send_file(RECIPIENT, media, account="work") == {
        "success": False,
        "message": "not on WhatsApp",
        "account": "work",
        "account_phone_number": WORK_NUMBER,
    }


@pytest.mark.parametrize("status_code", [200, 500])
def test_audio_conversion_and_cleanup_are_unchanged_with_a_named_account(two_accounts, converted_audio, status_code):
    src, converted = converted_audio
    two_accounts.ports[8082] = FakeResponse(status_code=status_code)

    result = main.send_audio_message(RECIPIENT, str(src), account="work")

    assert result["success"] is (status_code == 200)
    assert [call["json"]["media_path"] for call in two_accounts.calls] == [str(converted)]
    assert not os.path.exists(converted)
    assert src.exists()


def test_audio_without_account_is_not_converted(two_accounts, converted_audio):
    src, converted = converted_audio

    with pytest.raises(ValueError):
        main.send_audio_message(RECIPIENT, str(src))

    assert not converted.exists()


def test_one_account_from_a_file_sends_without_naming_it(account_store, accounts_file, fake_bridge, media):
    accounts_file([_entry("personal", account_store("personal", own_number=PERSONAL_NUMBER, token="t"), 8081)])
    fake_bridge.ports[8081] = FakeResponse()

    assert main.send_message(RECIPIENT, "hello") == {"success": True, "message": "sent"}
    assert main.send_file(RECIPIENT, media, account="personal") == {"success": True, "message": "sent"}
    assert [call["url"] for call in fake_bridge.calls] == ["http://localhost:8081/api/send"] * 2


def test_default_account_sends_exactly_as_before(monkeypatch, fake_bridge, media, converted_audio):
    use_default_account(monkeypatch, WHATSAPP_API_URL="http://localhost:8080/api", WHATSAPP_BRIDGE_TOKEN="tok")
    fake_bridge.ports[8080] = FakeResponse()
    src, converted = converted_audio

    assert main.send_message(RECIPIENT, "hello") == {"success": True, "message": "sent"}
    assert main.send_file(RECIPIENT, media, "Q3 numbers") == {"success": True, "message": "sent"}
    assert main.send_audio_message(RECIPIENT, str(src)) == {"success": True, "message": "sent"}
    assert [(call["url"], call["headers"]) for call in fake_bridge.calls] == [
        ("http://localhost:8080/api/send", {"Authorization": "Bearer tok"})
    ] * 3
    assert not converted.exists()


def test_empty_recipient_is_refused_without_a_bridge_call(two_accounts):
    assert main.send_message("", "hello", account="work") == {
        "success": False,
        "message": "Recipient must be provided",
        "account": "work",
        "account_phone_number": WORK_NUMBER,
    }
    assert two_accounts.calls == []

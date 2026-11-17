"""Actions on an existing message go through the account that holds it."""

import sqlite3

import pytest

import main
import media_preview
import transcription
from tests.conftest import FakeResponse, use_default_account

PERSONAL_NUMBER = "391000000001"
WORK_NUMBER = "392000000002"
PERSONAL_JID = f"{PERSONAL_NUMBER}@s.whatsapp.net"
WORK_JID = f"{WORK_NUMBER}@s.whatsapp.net"
ALICE = "391111111111@s.whatsapp.net"
BOB = "392222222222@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
PORTS = {"personal": 8081, "work": 8082}
TOKENS = {"personal": "personal-token", "work": "work-token"}
NUMBERS = {"personal": PERSONAL_NUMBER, "work": WORK_NUMBER}


def _entry(name, store):
    return {"name": name, "store_dir": str(store), "api_url": f"http://localhost:{PORTS[name]}/api"}


def _fill(store, messages):
    """messages: (id, chat_jid, sender, media_type)."""
    conn = sqlite3.connect(store / "messages.db")
    conn.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type) "
        "VALUES (?, ?, ?, '', '2026-11-17 10:00:00+00:00', 0, ?)",
        messages,
    )
    conn.commit()
    conn.close()


def _content(store, message_id, chat_jid):
    conn = sqlite3.connect(store / "messages.db")
    try:
        return conn.execute(
            "SELECT content FROM messages WHERE id = ? AND chat_jid = ?", (message_id, chat_jid)
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def stores(account_store):
    """personal holds Alice's p1, work holds Bob's w1; both are in GROUP, so both hold g1.

    gp and gw reached GROUP while only personal, or only work, was linked.

    x1 went from personal to work: each store holds it, in its own chat with the other number.
    """
    personal = account_store("personal", own_number=PERSONAL_NUMBER, token=TOKENS["personal"])
    work = account_store("work", own_number=WORK_NUMBER, token=TOKENS["work"])
    _fill(
        personal,
        [
            ("p1", ALICE, ALICE, "audio"),
            ("g1", GROUP, ALICE, "audio"),
            ("gp", GROUP, ALICE, ""),
            ("x1", WORK_JID, PERSONAL_JID, ""),
        ],
    )
    _fill(
        work,
        [
            ("w1", BOB, BOB, "audio"),
            ("g1", GROUP, ALICE, "audio"),
            ("gw", GROUP, ALICE, ""),
            ("x1", PERSONAL_JID, PERSONAL_JID, ""),
        ],
    )
    return {"personal": personal, "work": work}


@pytest.fixture
def bridge(fake_bridge, tmp_path):
    """Both bridges answer every call with success; a download returns a voice note on disk."""
    voice = tmp_path / "voice.ogg"
    voice.write_bytes(b"ogg")
    for port in PORTS.values():
        fake_bridge.ports[port] = FakeResponse(
            payload={"success": True, "ok": True, "message": "done", "path": str(voice)}
        )
    return fake_bridge


@pytest.fixture
def two_accounts(stores, accounts_file, bridge):
    accounts_file([_entry("personal", stores["personal"]), _entry("work", stores["work"])])
    return bridge


@pytest.fixture
def transcriber(monkeypatch):
    """A transcription engine that hears the same words every time, and counts its runs."""
    runs = []

    def transcribe_file(path, work_dir, model, provider):
        runs.append(path)
        return "recognised words"

    monkeypatch.setattr(transcription, "provider_name", lambda: "whisper_cpp")
    monkeypatch.setattr(transcription, "configured_model", lambda _provider: "m.bin")
    monkeypatch.setattr(transcription, "transcribe_file", transcribe_file)
    # A message no store holds can't take its transcript; don't wait between retries.
    monkeypatch.setattr(transcription.time, "sleep", lambda _s: None)
    return runs


@pytest.fixture
def previewer(monkeypatch):
    monkeypatch.setattr(media_preview, "render_preview", lambda *_args, **_kwargs: (b"image-bytes", "png"))


# Each message-scoped tool, called with a message ID and its chat; the endpoint it reaches.
ACTIONS = {
    "send_reaction": (lambda mid, chat, **kw: main.send_reaction(chat, mid, "👍", **kw), "/react"),
    "mark_messages_read": (lambda mid, chat, **kw: main.mark_messages_read([mid], chat, ALICE, **kw), "/mark-read"),
    "send_message_quoted": (
        lambda mid, chat, **kw: main.send_message(chat, "ok", quoted_message_id=mid, **kw),
        "/send",
    ),
    "download_media": (lambda mid, chat, **kw: main.download_media(mid, chat, **kw), "/download"),
    "view_media": (lambda mid, chat, **kw: main.view_media(mid, chat, **kw), "/download"),
    "transcribe_audio": (lambda mid, chat, **kw: main.transcribe_audio(mid, chat, **kw), "/download"),
}


@pytest.fixture(params=list(ACTIONS))
def action(request, transcriber, previewer):
    return ACTIONS[request.param]


def _reached(bridge):
    return [(call["url"], call["headers"]) for call in bridge.calls]


def _at(account, endpoint):
    return (f"http://localhost:{PORTS[account]}/api{endpoint}", {"Authorization": f"Bearer {TOKENS[account]}"})


@pytest.mark.parametrize(("message_id", "chat", "holder"), [("p1", ALICE, "personal"), ("w1", BOB, "work")])
def test_message_held_by_one_account_goes_to_its_bridge_without_naming_it(
    two_accounts, action, message_id, chat, holder
):
    act, endpoint = action

    act(message_id, chat)

    assert _reached(two_accounts) == [_at(holder, endpoint)]


@pytest.mark.parametrize(("chat", "holder"), [(WORK_JID, "personal"), (PERSONAL_JID, "work")])
def test_message_between_my_numbers_is_held_by_the_side_whose_chat_is_named(two_accounts, chat, holder):
    main.send_reaction(chat, "x1", "👍")

    assert _reached(two_accounts) == [_at(holder, "/react")]


@pytest.mark.parametrize("account", [None, ""])
def test_message_held_by_both_accounts_needs_an_account(two_accounts, action, account):
    act, _endpoint = action

    with pytest.raises(ValueError, match="held by several accounts: personal, work"):
        act("g1", GROUP, account=account)

    assert two_accounts.calls == []


@pytest.mark.parametrize("account", ["personal", "work"])
def test_message_held_by_both_accounts_uses_the_named_one(two_accounts, action, account):
    act, endpoint = action

    act("g1", GROUP, account=account)

    assert _reached(two_accounts) == [_at(account, endpoint)]


def test_naming_an_account_that_does_not_hold_the_message_is_refused(two_accounts, action):
    act, _endpoint = action

    with pytest.raises(ValueError, match="Account 'work' does not hold message 'p1'.*held by: personal"):
        act("p1", ALICE, account="work")

    assert two_accounts.calls == []


def test_unknown_account_is_refused_with_the_valid_names(two_accounts, action):
    act, _endpoint = action

    with pytest.raises(ValueError, match="Unknown account 'home'; valid accounts: personal, work"):
        act("p1", ALICE, account="home")

    assert two_accounts.calls == []


def test_message_no_account_holds_needs_an_account(two_accounts, action):
    act, _endpoint = action

    with pytest.raises(ValueError, match="No account holds message 'zz'.*valid accounts: personal, work"):
        act("zz", ALICE)

    assert two_accounts.calls == []


@pytest.mark.parametrize("account", ["personal", "work"])
def test_message_no_account_holds_uses_the_named_account(two_accounts, action, account):
    act, endpoint = action

    act("zz", ALICE, account=account)

    assert _reached(two_accounts) == [_at(account, endpoint)]


def test_reply_to_a_phone_number_finds_the_quoted_message_in_that_chat(two_accounts):
    main.send_message(ALICE.split("@")[0], "ok", quoted_message_id="p1")

    assert _reached(two_accounts) == [_at("personal", "/send")]


def test_account_with_a_missing_store_is_skipped_when_locating(tmp_path, stores, accounts_file, bridge):
    accounts_file(
        [
            {"name": "gone", "store_dir": str(tmp_path / "never-started"), "api_url": "http://localhost:8083/api"},
            _entry("work", stores["work"]),
        ]
    )

    main.send_reaction(BOB, "w1", "👍")

    assert _reached(bridge) == [_at("work", "/react")]


def test_receipts_for_messages_held_by_different_accounts_need_an_account(two_accounts):
    # Each account could only mark its own message read; neither can be picked for both.
    with pytest.raises(ValueError, match="held by several accounts: personal, work"):
        main.mark_messages_read(["gp", "gw"], GROUP, ALICE)

    assert two_accounts.calls == []


@pytest.mark.parametrize(
    ("act", "message_id", "chat", "holder"),
    [
        (lambda mid, chat: main.send_reaction(chat, mid, "👍"), "p1", ALICE, "personal"),
        (lambda mid, chat: main.mark_messages_read([mid], chat), "w1", BOB, "work"),
        (lambda mid, chat: main.send_message(chat, "ok", quoted_message_id=mid), "w1", BOB, "work"),
    ],
)
def test_write_result_names_the_account_and_its_own_number(two_accounts, act, message_id, chat, holder):
    result = act(message_id, chat)

    assert result["account"] == holder
    assert result["account_phone_number"] == NUMBERS[holder]


def test_download_result_names_the_account(two_accounts, tmp_path):
    assert main.download_media("w1", BOB) == {
        "success": True,
        "message": "Media downloaded successfully",
        "file_path": str(tmp_path / "voice.ogg"),
        "account": "work",
    }


def test_transcript_is_stored_in_and_served_from_the_holding_account(two_accounts, stores, transcriber):
    first = main.transcribe_audio("w1", BOB)
    again = main.transcribe_audio("w1", BOB)

    assert first["success"] and first["account"] == "work"
    assert again == {**first, "message": "Transcript already stored"}
    assert _content(stores["work"], "w1", BOB) == first["transcript"]
    assert len(transcriber) == 1
    assert _reached(two_accounts) == [_at("work", "/download")]


def test_transcript_of_a_shared_message_stays_in_the_named_account(two_accounts, stores, transcriber):
    work = main.transcribe_audio("g1", GROUP, account="work")

    assert _content(stores["work"], "g1", GROUP) == work["transcript"]
    assert _content(stores["personal"], "g1", GROUP) == ""

    assert main.transcribe_audio("g1", GROUP, account="work")["message"] == "Transcript already stored"
    assert main.transcribe_audio("g1", GROUP, account="personal")["message"] == "Transcribed"
    assert len(transcriber) == 2
    assert _reached(two_accounts) == [_at("work", "/download"), _at("personal", "/download")]


def test_one_account_from_a_file_acts_without_locating(tmp_path, accounts_file, bridge, transcriber, previewer):
    # Its store was never created, so nothing can be located: the only account is used, as before.
    accounts_file([_entry("personal", tmp_path / "never-started")])

    for act, _endpoint in ACTIONS.values():
        act("zz", ALICE)

    assert [url for url, _headers in _reached(bridge)] == [
        f"http://localhost:8081/api{endpoint}" for _act, endpoint in ACTIONS.values()
    ]


def test_default_account_acts_exactly_as_before(tmp_path, monkeypatch, fake_bridge, transcriber, previewer):
    use_default_account(
        monkeypatch,
        WHATSAPP_API_URL="http://localhost:8080/api",
        WHATSAPP_BRIDGE_TOKEN="tok",
        WHATSAPP_DB_PATH=tmp_path / "missing" / "messages.db",
    )
    fake_bridge.ports[8080] = FakeResponse(payload={"success": True, "ok": True, "message": "done"})

    assert main.send_reaction(ALICE, "p1", "👍") == {"success": True, "message": "Reaction sent"}
    assert main.mark_messages_read(["p1"], ALICE) == {"success": True, "message": "done"}
    assert main.send_message(ALICE, "ok", quoted_message_id="p1") == {"success": True, "message": "done"}
    assert main.download_media("p1", ALICE) == {"success": False, "message": "Failed to download media"}
    assert main.view_media("p1", ALICE) == {"success": False, "message": "Failed to download media"}
    assert main.transcribe_audio("p1", ALICE)["success"] is False
    assert {(call["url"].rsplit("/", 1)[1], call["headers"]["Authorization"]) for call in fake_bridge.calls} == {
        ("react", "Bearer tok"),
        ("mark-read", "Bearer tok"),
        ("send", "Bearer tok"),
        ("download", "Bearer tok"),
    }

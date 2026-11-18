"""Lookup tools find an item in whichever account holds it unless one is named."""

import sqlite3

import pytest

import main

PERSONAL_NUMBER = "391000000001"
WORK_NUMBER = "392000000002"
PERSONAL_JID = f"{PERSONAL_NUMBER}@s.whatsapp.net"
WORK_JID = f"{WORK_NUMBER}@s.whatsapp.net"
ALICE = "391111111111@s.whatsapp.net"
BOB = "392222222222@s.whatsapp.net"
SAM = "393333333333@s.whatsapp.net"


def _entry(name, store, port):
    return {"name": name, "store_dir": str(store), "api_url": f"http://localhost:{port}/api"}


def _at(minute):
    return f"2026-11-17 10:{minute:02d}:00+00:00"


def _fill(store, chats, messages):
    """chats: (jid, name, last_message_time); messages: (id, chat_jid, sender, content, minute, is_from_me)."""
    conn = sqlite3.connect(store / "messages.db")
    conn.executemany("INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)", chats)
    conn.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (id_, chat, sender, content, _at(minute), from_me)
            for id_, chat, sender, content, minute, from_me in messages
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def two_accounts(account_store, accounts_file):
    """personal knows Alice, work knows Bob; both know Sam and each other.

    Sam was last active on work. Message x1, which personal sent to work, is in
    both stores under the same ID; work's reply x2 only reached work's store, so
    that chat was last active on work.
    """
    personal = account_store("personal", own_number=PERSONAL_NUMBER)
    work = account_store("work", own_number=WORK_NUMBER)
    _fill(
        personal,
        chats=[(ALICE, "Alice", _at(4)), (SAM, "Sam", _at(1)), (WORK_JID, "Work line", _at(6))],
        messages=[
            ("p1", ALICE, ALICE, "alice one", 2, 0),
            ("p2", ALICE, ALICE, "alice two", 4, 0),
            ("s1", SAM, SAM, "sam on personal", 1, 0),
            ("x1", WORK_JID, PERSONAL_JID, "between my numbers", 6, 1),
        ],
    )
    _fill(
        work,
        chats=[(BOB, "Bob", _at(3)), (SAM, "Sam (work)", _at(5)), (PERSONAL_JID, "Personal line", _at(9))],
        messages=[
            ("w1", BOB, BOB, "bob one", 3, 0),
            ("s2", SAM, SAM, "sam on work", 5, 0),
            ("x1", PERSONAL_JID, PERSONAL_JID, "between my numbers", 6, 0),
            ("x2", PERSONAL_JID, WORK_JID, "work reply", 9, 1),
        ],
    )
    accounts_file([_entry("personal", personal, 8081), _entry("work", work, 8082)])
    return personal, work


def test_get_chat_finds_a_chat_held_by_one_account(two_accounts):
    alice = main.get_chat(ALICE)
    bob = main.get_chat(BOB)

    assert (alice["jid"], alice["name"], alice["account"]) == (ALICE, "Alice", "personal")
    assert (bob["jid"], bob["name"], bob["account"]) == (BOB, "Bob", "work")
    assert "also_in_accounts" not in alice
    assert "also_in_accounts" not in bob


def test_get_chat_held_by_both_accounts_returns_the_most_recently_active(two_accounts):
    sam = main.get_chat(SAM)

    assert (sam["name"], sam["account"], sam["also_in_accounts"]) == ("Sam (work)", "work", ["personal"])
    assert sam["last_message"] == "sam on work"


def test_get_chat_with_an_account_looks_only_there(two_accounts):
    sam = main.get_chat(SAM, account="personal")

    assert (sam["name"], sam["account"]) == ("Sam", "personal")
    assert "also_in_accounts" not in sam
    assert main.get_chat(BOB, account="personal") is None


def test_get_direct_chat_by_contact_finds_the_account_that_has_it(two_accounts):
    bob = main.get_direct_chat_by_contact("392222222222")
    sam = main.get_direct_chat_by_contact("393333333333")
    sam_on_personal = main.get_direct_chat_by_contact("393333333333", account="personal")

    assert (bob["jid"], bob["account"]) == (BOB, "work")
    assert "also_in_accounts" not in bob
    assert (sam["name"], sam["account"], sam["also_in_accounts"]) == ("Sam (work)", "work", ["personal"])
    assert (sam_on_personal["name"], sam_on_personal["account"]) == ("Sam", "personal")
    assert main.get_direct_chat_by_contact("392222222222", account="personal") is None


def test_get_last_interaction_finds_the_account_that_has_it(two_accounts):
    alice = main.get_last_interaction(ALICE)
    sam = main.get_last_interaction(SAM)
    sam_on_personal = main.get_last_interaction(SAM, account="personal")

    assert (alice["id"], alice["account"]) == ("p2", "personal")
    assert "also_in_accounts" not in alice
    assert (sam["id"], sam["account"], sam["also_in_accounts"]) == ("s2", "work", ["personal"])
    assert (sam_on_personal["id"], sam_on_personal["account"]) == ("s1", "personal")
    assert main.get_last_interaction(BOB, account="personal") == {}


def test_get_message_context_finds_the_account_that_holds_the_message(two_accounts):
    context = main.get_message_context("p1", before=1, after=1)

    assert (context["message"]["id"], context["account"]) == ("p1", "personal")
    assert "also_in_accounts" not in context
    assert [(row["id"], row["account"]) for row in (context["message"], *context["after"])] == [
        ("p1", "personal"),
        ("p2", "personal"),
    ]
    assert context["before"] == []


def test_get_message_context_held_by_both_accounts_returns_the_most_recently_active_chat(two_accounts):
    # x1 went from personal to work, so both stores hold it under the same ID.
    context = main.get_message_context("x1", before=1, after=1)

    assert (context["account"], context["also_in_accounts"]) == ("work", ["personal"])
    assert (context["message"]["chat_jid"], context["message"]["account"]) == (PERSONAL_JID, "work")
    assert [(row["id"], row["account"]) for row in context["after"]] == [("x2", "work")]


def test_get_message_context_with_an_account_looks_only_there(two_accounts):
    context = main.get_message_context("x1", before=1, after=1, account="personal")

    assert (context["account"], context["message"]["chat_jid"], context["message"]["is_from_me"]) == (
        "personal",
        WORK_JID,
        True,
    )
    assert context["after"] == []
    assert "also_in_accounts" not in context
    with pytest.raises(ValueError, match="Message with ID p1 not found"):
        main.get_message_context("p1", account="work")


def test_get_contact_finds_the_account_that_knows_the_contact(two_accounts):
    alice = main.get_contact("391111111111")
    sam = main.get_contact("393333333333")
    sam_on_personal = main.get_contact("393333333333", account="personal")

    assert (alice["jid"], alice["name"], alice["resolved"], alice["account"]) == (ALICE, "Alice", True, "personal")
    assert "also_in_accounts" not in alice
    assert (sam["name"], sam["account"], sam["also_in_accounts"]) == ("Sam (work)", "work", ["personal"])
    assert (sam_on_personal["name"], sam_on_personal["account"]) == ("Sam", "personal")
    assert "also_in_accounts" not in sam_on_personal


LOOKUPS = {
    "get_chat": lambda jid, **kw: main.get_chat(jid, **kw),
    "get_direct_chat_by_contact": lambda jid, **kw: main.get_direct_chat_by_contact(jid.split("@")[0], **kw),
    "get_contact": lambda jid, **kw: main.get_contact(jid, **kw),
    "get_last_interaction": lambda jid, **kw: main.get_last_interaction(jid, **kw),
}


@pytest.mark.parametrize("lookup", [*LOOKUPS, "get_message_context"])
def test_unknown_account_is_refused_with_the_valid_names(two_accounts, lookup):
    read = LOOKUPS.get(lookup, lambda _jid, **kw: main.get_message_context("p1", **kw))

    with pytest.raises(ValueError, match="Unknown account 'home'; valid accounts: personal, work"):
        read(ALICE, account="home")


def test_an_item_no_account_holds_reads_as_not_found(two_accounts):
    nobody = "399999999999@s.whatsapp.net"

    assert main.get_chat(nobody) is None
    assert main.get_direct_chat_by_contact("399999999999") is None
    assert main.get_last_interaction(nobody) == {}
    assert main.get_contact("399999999999") == {
        "identifier": "399999999999",
        "jid": nobody,
        "phone_number": "399999999999",
        "lid": None,
        "name": "399999999999",
        "display_name": nobody,
        "is_lid": False,
        "resolved": False,
    }
    with pytest.raises(ValueError, match="Message with ID nope not found"):
        main.get_message_context("nope")


@pytest.mark.parametrize("lookup", [*LOOKUPS, "get_message_context"])
def test_lookups_skip_a_missing_store_but_naming_it_fails_loudly(tmp_path, account_store, accounts_file, lookup):
    work = account_store("work")
    _fill(work, chats=[(BOB, "Bob", _at(3))], messages=[("w1", BOB, BOB, "bob one", 3, 0)])
    accounts_file([_entry("gone", tmp_path / "never-started", 8083), _entry("work", work, 8082)])
    read = LOOKUPS.get(lookup, lambda _jid, **kw: main.get_message_context("w1", **kw))

    assert read(BOB)["account"] == "work"
    with pytest.raises(FileNotFoundError, match="account 'gone'"):
        read(BOB, account="gone")
    assert not (tmp_path / "never-started").exists()


@pytest.mark.parametrize("lookup", [*LOOKUPS, "get_message_context"])
def test_a_single_account_is_not_tagged(account_store, accounts_file, lookup):
    work = account_store("work")
    _fill(work, chats=[(BOB, "Bob", _at(3))], messages=[("w1", BOB, BOB, "bob one", 3, 0)])
    accounts_file([_entry("work", work, 8082)])
    read = LOOKUPS.get(lookup, lambda _jid, **kw: main.get_message_context("w1", **kw))

    for result in (read(BOB), read(BOB, account="work")):
        assert result
        assert "account" not in result
        assert "also_in_accounts" not in result
        for message in (result.get("message", {}), *result.get("before", []), *result.get("after", [])):
            assert "account" not in message

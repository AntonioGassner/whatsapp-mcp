"""List and search tools read every account unless one is named."""

import sqlite3

import pytest

import main

ALICE = "391111111111@s.whatsapp.net"
BOB = "392222222222@s.whatsapp.net"
SHARED = "393333333333@s.whatsapp.net"


def _entry(name, store, port):
    return {"name": name, "store_dir": str(store), "api_url": f"http://localhost:{port}/api"}


def _fill(store, chats, messages):
    conn = sqlite3.connect(store / "messages.db")
    conn.executemany("INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)", chats)
    conn.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, 0)",
        messages,
    )
    conn.commit()
    conn.close()


def _at(minute):
    return f"2026-11-17 10:{minute:02d}:00+00:00"


@pytest.fixture
def two_accounts(account_store, accounts_file):
    """personal and work, whose messages alternate in time: p1 w1 p2 w2 p3 w3."""
    personal = account_store("personal")
    work = account_store("work")
    _fill(
        personal,
        chats=[(ALICE, "Alice", _at(4)), (SHARED, "Sam", _at(1))],
        messages=[
            ("p1", ALICE, ALICE, "personal one", _at(0)),
            ("p2", ALICE, ALICE, "personal two invoice", _at(2)),
            ("p3", ALICE, ALICE, "personal three", _at(4)),
        ],
    )
    _fill(
        work,
        chats=[(BOB, "Bob", _at(3)), (SHARED, "Sam (work)", _at(5))],
        messages=[
            ("w1", BOB, BOB, "work one", _at(1)),
            ("w2", BOB, BOB, "work two invoice", _at(3)),
            ("w3", SHARED, SHARED, "work three", _at(5)),
        ],
    )
    accounts_file([_entry("personal", personal, 8081), _entry("work", work, 8082)])
    return personal, work


def _ids(rows):
    return [(row["id"], row["account"]) for row in rows]


def test_list_messages_without_account_interleaves_every_account_newest_first(two_accounts):
    rows = main.list_messages(include_context=False)

    assert _ids(rows) == [
        ("w3", "work"),
        ("p3", "personal"),
        ("w2", "work"),
        ("p2", "personal"),
        ("w1", "work"),
        ("p1", "personal"),
    ]


def test_list_messages_without_account_interleaves_every_account_oldest_first(two_accounts):
    rows = main.list_messages(include_context=False, sort_by="oldest")

    assert [row["id"] for row in rows] == ["p1", "w1", "p2", "w2", "p3", "w3"]
    assert [row["account"] for row in rows] == ["personal", "work"] * 3


@pytest.mark.parametrize("sort_by", ["newest", "oldest"])
@pytest.mark.parametrize("limit", [1, 2, 4, 5])
def test_paging_across_accounts_returns_every_message_exactly_once(two_accounts, sort_by, limit):
    everything = _ids(main.list_messages(include_context=False, sort_by=sort_by, limit=50))

    paged = []
    for page in range(7):
        paged += _ids(main.list_messages(include_context=False, sort_by=sort_by, limit=limit, page=page))

    assert paged == everything
    assert len(everything) == 6


def test_list_messages_context_comes_from_the_account_of_each_match(two_accounts):
    rows = main.list_messages(query="invoice")

    assert _ids(rows) == [
        ("w1", "work"),
        ("w2", "work"),
        ("p1", "personal"),
        ("p2", "personal"),
        ("p3", "personal"),
    ]


def test_list_messages_with_an_account_reads_only_that_account(two_accounts):
    rows = main.list_messages(include_context=False, account="personal", limit=2, page=1)

    assert _ids(rows) == [("p1", "personal")]


def test_list_chats_without_account_merges_by_last_activity_and_keeps_shared_chats_per_account(two_accounts):
    chats = main.list_chats()

    assert [(chat["jid"], chat["account"]) for chat in chats] == [
        (SHARED, "work"),
        (ALICE, "personal"),
        (BOB, "work"),
        (SHARED, "personal"),
    ]
    assert [chat["name"] for chat in chats] == ["Sam (work)", "Alice", "Bob", "Sam"]


def test_list_chats_pages_after_merging(two_accounts):
    pages = [main.list_chats(limit=3, page=page) for page in range(3)]

    assert [[chat["name"] for chat in page] for page in pages] == [["Sam (work)", "Alice", "Bob"], ["Sam"], []]


def test_list_chats_by_name_merges_alphabetically(two_accounts):
    chats = main.list_chats(sort_by="name")

    assert [chat["name"] for chat in chats] == ["Alice", "Bob", "Sam", "Sam (work)"]


def test_list_chats_with_an_account_reads_only_that_account(two_accounts):
    chats = main.list_chats(account="work")

    assert [(chat["name"], chat["account"]) for chat in chats] == [("Sam (work)", "work"), ("Bob", "work")]


def test_search_contacts_covers_every_account_unless_one_is_named(two_accounts):
    everywhere = main.search_contacts("Sam")
    in_work = main.search_contacts("Sam", account="work")

    assert [(contact["jid"], contact["name"], contact["account"]) for contact in everywhere] == [
        (SHARED, "Sam", "personal"),
        (SHARED, "Sam (work)", "work"),
    ]
    assert [(contact["name"], contact["account"]) for contact in in_work] == [("Sam (work)", "work")]


def test_get_contact_chats_covers_every_account_unless_one_is_named(two_accounts):
    everywhere = main.get_contact_chats(SHARED)
    in_personal = main.get_contact_chats(SHARED, account="personal")

    assert [(chat["name"], chat["account"]) for chat in everywhere] == [("Sam (work)", "work"), ("Sam", "personal")]
    assert [(chat["name"], chat["account"]) for chat in in_personal] == [("Sam", "personal")]


def test_an_empty_account_name_reads_every_account(two_accounts):
    # Clients often send "" for a string argument they meant to leave out.
    assert [chat["account"] for chat in main.list_chats(account="")] == ["work", "personal", "work", "personal"]


@pytest.mark.parametrize(
    "read",
    [
        lambda: main.list_messages(account="home"),
        lambda: main.list_chats(account="home"),
        lambda: main.search_contacts("Sam", account="home"),
        lambda: main.get_contact_chats(SHARED, account="home"),
    ],
    ids=["list_messages", "list_chats", "search_contacts", "get_contact_chats"],
)
def test_unknown_account_is_refused_with_the_valid_names(two_accounts, read):
    with pytest.raises(ValueError, match="Unknown account 'home'; valid accounts: personal, work"):
        read()


def test_fan_out_skips_a_missing_store_but_naming_it_fails_loudly(tmp_path, account_store, accounts_file):
    work = account_store("work")
    _fill(work, chats=[(BOB, "Bob", _at(3))], messages=[("w1", BOB, BOB, "work one", _at(1))])
    accounts_file([_entry("gone", tmp_path / "never-started", 8083), _entry("work", work, 8082)])

    assert _ids(main.list_messages()) == [("w1", "work")]
    assert [chat["account"] for chat in main.list_chats()] == ["work"]
    assert [contact["account"] for contact in main.search_contacts("Bob")] == ["work"]
    assert [chat["account"] for chat in main.get_contact_chats(BOB)] == ["work"]

    for read in (
        lambda: main.list_messages(account="gone"),
        lambda: main.list_chats(account="gone"),
        lambda: main.search_contacts("Bob", account="gone"),
        lambda: main.get_contact_chats(BOB, account="gone"),
    ):
        with pytest.raises(FileNotFoundError, match="account 'gone'"):
            read()
    assert not (tmp_path / "never-started").exists()


def test_fan_out_fails_loudly_when_no_account_has_a_store(tmp_path, accounts_file):
    accounts_file([_entry("gone", tmp_path / "gone", 8083), _entry("lost", tmp_path / "lost", 8084)])

    with pytest.raises(FileNotFoundError, match="account 'gone'"):
        main.list_chats()


def test_a_single_account_is_not_tagged(account_store, accounts_file):
    work = account_store("work")
    _fill(work, chats=[(BOB, "Bob", _at(3))], messages=[("w1", BOB, BOB, "work one", _at(1))])
    accounts_file([_entry("work", work, 8082)])

    for rows in (
        main.list_messages(),
        main.list_messages(account="work"),
        main.list_chats(),
        main.search_contacts("Bob"),
        main.get_contact_chats(BOB),
    ):
        assert rows
        assert all("account" not in row for row in rows)

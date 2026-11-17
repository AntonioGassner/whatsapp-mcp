import os
import signal
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, TypeVar

from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field

import accounts
import media_preview
import transcription
from accounts import Account
from mcp_config import resolve_host, resolve_port, resolve_transport
from parent_watchdog import install_stdio_parent_watchdog
from whatsapp import (
    Message,
    MessageContext,
    MessageNotFoundError,
    msg_to_dict,
)
from whatsapp import (
    connection_status as whatsapp_connection_status,
)
from whatsapp import (
    download_media as whatsapp_download_media,
)
from whatsapp import (
    find_messages as whatsapp_find_messages,
)
from whatsapp import (
    get_chat as whatsapp_get_chat,
)
from whatsapp import (
    get_contact_chats as whatsapp_get_contact_chats,
)
from whatsapp import (
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
)
from whatsapp import (
    get_last_interaction as whatsapp_get_last_interaction,
)
from whatsapp import (
    get_message_context as whatsapp_get_message_context,
)
from whatsapp import (
    get_sender_name as whatsapp_get_sender_name,
)
from whatsapp import (
    list_chats as whatsapp_list_chats,
)
from whatsapp import (
    list_messages as whatsapp_list_messages,
)
from whatsapp import (
    mark_messages_read as whatsapp_mark_messages_read,
)
from whatsapp import (
    own_number as whatsapp_own_number,
)
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
)
from whatsapp import (
    send_audio_message as whatsapp_audio_voice_message,
)
from whatsapp import (
    send_file as whatsapp_send_file,
)
from whatsapp import (
    send_message as whatsapp_send_message,
)
from whatsapp import (
    send_reaction as whatsapp_send_reaction,
)
from whatsapp import (
    with_context as whatsapp_with_context,
)

T = TypeVar("T")

# Initialize FastMCP server. Env-var handling is deferred to the __main__ block
# so importing this module never parses env vars or exits the process.
mcp = FastMCP("whatsapp")

PreviewDimension = Annotated[
    int,
    Field(
        strict=True,
        ge=media_preview.MIN_MAX_DIMENSION,
        le=media_preview.MAX_MAX_DIMENSION,
        description="Longest preview edge in pixels; must be an integer from 1 to 2048.",
    ),
]


AccountName = Annotated[
    str | None,
    Field(
        description=(
            "Name of the account (WhatsApp number) to read, as listed by list_accounts. "
            "Omit to read every account; with several accounts each result carries an `account` field."
        )
    ),
]


SendingAccount = Annotated[
    str | None,
    Field(
        description=(
            "Name of the account (WhatsApp number) to send from, as listed by list_accounts. "
            "Required when several accounts are configured: ask the user which account to send from, "
            "never pick one yourself. Optional with a single account."
        )
    ),
]


def _account() -> Account:
    # Until the tools learn which account to act on, they use the first one configured.
    return accounts.registry().first()


def _tagged(account: Account, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Say which account each row came from, once there is more than one to come from."""
    if not accounts.registry().several:
        return rows
    return [{**row, "account": account.name} for row in rows]


def _read_each(targets: tuple[Account, ...], read: Callable[[Account], list[T]]) -> list[tuple[Account, list[T]]]:
    """Run one read per account, skipping accounts whose store is missing.

    Only an account that was asked for by itself, or a read where no account had
    a store at all, fails loudly: one broken bridge must not hide the others.
    """
    results = []
    missing: FileNotFoundError | None = None
    for account in targets:
        try:
            results.append((account, read(account)))
        except FileNotFoundError as exc:
            if len(targets) == 1:
                raise
            missing = missing or exc
    if not results and missing:
        raise missing
    return results


def _look_up(
    targets: tuple[Account, ...], read: Callable[[Account], T | None], activity: Callable[[Account, T], Any]
) -> tuple[Account, T, list[str]] | None:
    """Find one item in the target accounts; read(account) returns it, or None where it isn't held.

    Returns the match from the account where activity(account, item) is most
    recent, and the names of the other accounts that also hold it; None when no
    account does. Ties keep the accounts' order. Missing stores are skipped as
    in _read_each.
    """

    def read_one(target: Account) -> list[T]:
        item = read(target)
        return [] if item is None else [item]

    found = [(target, items[0]) for target, items in _read_each(targets, read_one) if items]
    if not found:
        return None
    # A single match needs no activity, which may cost another read.
    target, item = found[0] if len(found) == 1 else max(found, key=lambda match: _instant(activity(*match)))
    return target, item, [other.name for other, _ in found if other is not target]


def _sent(account: Account, success: bool, message: str) -> dict[str, Any]:
    """A send's result, naming the account and number it went out from once there are several."""
    result: dict[str, Any] = {"success": success, "message": message}
    if accounts.registry().several:
        result["account"] = account.name
        result["account_phone_number"] = whatsapp_own_number(account)
    return result


def _located(account: Account, row: dict[str, Any], also_in: list[str]) -> dict[str, Any]:
    """Say which account a lookup's match came from, and which other accounts also hold it."""
    if not accounts.registry().several:
        return row
    row = {**row, "account": account.name}
    if also_in:
        row["also_in_accounts"] = also_in
    return row


def _merged_page(
    per_account: list[tuple[Account, list[T]]], key: Callable[[T], Any], reverse: bool, limit: int, page: int
) -> list[tuple[Account, T]]:
    """One page of every account's rows in a single order.

    Each account must have returned its first (page + 1) * limit rows in that
    order, so the page is exact: page N continues where page N-1 stopped.
    Ties keep the accounts' order, so paging is stable.
    """
    rows = [(account, row) for account, account_rows in per_account for row in account_rows]
    rows.sort(key=lambda item: key(item[1]), reverse=reverse)
    return rows[page * limit : (page + 1) * limit]


def _instant(value: datetime | str | None) -> tuple[bool, datetime]:
    """Sort key for a timestamp that may be missing; missing sorts before any time."""
    if value is None:
        return (False, datetime.min.replace(tzinfo=UTC))
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (True, value)


def _chats_page(
    targets: tuple[Account, ...],
    read: Callable[[Account, int, int], list[dict[str, Any]]],
    sort_by: str,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    """One page of chats from the target accounts; read(account, limit, page) reads one account."""
    if len(targets) == 1:
        return _tagged(targets[0], read(targets[0], limit, page))
    per_account = _read_each(targets, lambda target: read(target, (page + 1) * limit, 0))
    key, reverse = _chat_order(sort_by)
    return [{**chat, "account": target.name} for target, chat in _merged_page(per_account, key, reverse, limit, page)]


def _chat_order(sort_by: str) -> tuple[Callable[[dict[str, Any]], Any], bool]:
    """The merge key and direction matching whatsapp.list_chats' ORDER BY."""
    if sort_by == "last_active":
        return (lambda chat: _instant(chat["last_message_time"])), True
    # SQLite sorts NULL names first and strings by code point, as Python does.
    return (lambda chat: (chat["name"] is not None, chat["name"] or "")), False


@mcp.tool()
def list_accounts() -> list[dict[str, Any]]:
    """List the WhatsApp accounts (phone numbers) this server serves and whether each one works.

    Without an accounts file there is a single account named "default".

    Returns:
        One dictionary per account with its name, its own phone_number (null when
        its whatsmeow store can't be read), store_available (whether its messages
        database exists) and bridge: reachable, plus the bridge's reported state,
        connected and paired flags and message, or an error when it can't be asked.
        One broken account never hides the others.
    """
    return [
        {
            "name": account.name,
            "phone_number": whatsapp_own_number(account),
            "store_available": os.path.isfile(account.messages_db_path),
            "bridge": whatsapp_connection_status(account),
        }
        for account in accounts.registry().accounts
    ]


@mcp.tool()
def search_contacts(query: str, account: AccountName = None) -> list[dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number.

    Searches every account unless one is named. A person known on two accounts
    is listed once per account.

    Args:
        query: Search term to match against contact names or phone numbers
        account: Account to search (see list_accounts); omit to search every account
    """
    targets = accounts.registry().select(account)
    per_account = _read_each(targets, lambda target: whatsapp_search_contacts(target, query))
    return [row for target, contacts in per_account for row in _tagged(target, contacts)]


def _contact_in(
    account: Account, identifier: str, candidates: list[tuple[str, bool]]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """get_contact's answer from one account, with the chat it was found by (None if no chat matched)."""
    jid, is_lid = candidates[0]
    jid_user = jid.split("@", 1)[0]

    display_name: str | None = None
    resolved = False

    # Prefer chats table lookup via get_chat (works for both phone and LID contacts).
    chat = None
    for candidate_jid, candidate_is_lid in candidates:
        chat = whatsapp_get_chat(account, candidate_jid, include_last_message=False)
        if chat:
            jid = candidate_jid
            is_lid = candidate_is_lid
            jid_user = jid.split("@", 1)[0]
            break

    if chat and chat.get("name"):
        display_name = chat["name"]
        resolved = display_name not in (jid, jid_user)
    else:
        # Fallback: best-effort sender-name resolution (may use fuzzy LIKE lookup).
        display_name = whatsapp_get_sender_name(account, jid)
        resolved = display_name not in (jid, jid_user, identifier)

    contact = {
        "identifier": identifier,
        "jid": jid,
        "phone_number": jid_user if not is_lid else None,
        "lid": jid_user if is_lid else None,
        "name": display_name if resolved else jid_user,
        "display_name": display_name,
        "is_lid": is_lid,
        "resolved": resolved,
    }
    return contact, chat


@mcp.tool()
def get_contact(
    identifier: str | None = None,
    phone_number: str | None = None,
    phone: str | None = None,
    account: AccountName = None,
) -> dict[str, Any]:
    """Look up a WhatsApp contact by phone number, LID, or full JID.

    Automatically detects the identifier type and queries appropriately.
    Looks in every account unless one is named. When several accounts know the
    contact, the one whose chat with it was most recently active is returned and
    `also_in_accounts` names the others.

    Args:
        identifier: Phone number, LID, or full JID. Examples:
                    - "12025551234" (phone number)
                    - "35047067385985" (LID - numeric)
                    - "12025551234@s.whatsapp.net" (phone JID)
                    - "184125298348272@lid" (LID JID)
        phone_number: Backward-compatible alias for `identifier`.
        phone: Backward-compatible alias for `identifier` (matches README parameter name).
        account: Account to look in (see list_accounts); omit to look in every account

    Returns:
        Dictionary with jid, name, display_name, is_lid, and resolved status. A
        contact no account knows (no chat with it, no name for it) carries no
        `account` field.
    """
    if identifier is None:
        identifier = phone_number
    if identifier is None:
        identifier = phone
    if identifier is None:
        raise ValueError("Missing required argument: identifier (or phone_number / phone)")

    identifier = identifier.strip()
    if not identifier:
        raise ValueError("identifier must be non-empty")

    # Detect identifier type and normalize to JID.
    bare_numeric_digits: str | None = None
    if "@" in identifier:
        # Already a JID - use as-is
        jid = identifier
        is_lid = jid.endswith("@lid") or jid.split("@", 1)[-1] == "lid"
    else:
        digits = "".join(c for c in identifier if c.isdigit())
        if digits:
            # LIDs can overlap phone-number lengths, so bare numeric inputs try phone first.
            jid = f"{digits}@s.whatsapp.net"
            is_lid = False
            if identifier.isdigit():
                bare_numeric_digits = digits
        else:
            # Non-numeric and not a JID; try as-is.
            jid = identifier
            is_lid = False

    candidates: list[tuple[str, bool]] = [(jid, is_lid)]
    if bare_numeric_digits:
        candidates.append((f"{bare_numeric_digits}@lid", True))

    # An account knows the contact when it has a chat with it or can name it;
    # when none does, the first account's unresolved answer is returned.
    unknown: list[dict[str, Any]] = []

    def known_in(target: Account) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        contact, chat = _contact_in(target, identifier, candidates)
        if chat or contact["resolved"]:
            return contact, chat
        unknown.append(contact)
        return None

    match = _look_up(
        accounts.registry().select(account),
        known_in,
        lambda target, found: _last_active(target, found[1] or {}),
    )
    if match is None:
        return unknown[0]
    target, (contact, _chat), also_in = match
    return _located(target, contact, also_in)


@mcp.tool()
def list_messages(
    after: str | None = None,
    before: str | None = None,
    sender_phone_number: str | None = None,
    chat_jid: str | None = None,
    query: str | None = None,
    limit: int = 50,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    sort_by: str = "newest",
    account: AccountName = None,
) -> list[dict[str, Any]]:
    """Get WhatsApp messages matching specified criteria with optional context.

    Each message includes sender_display showing "Name (phone)" for easy identification.
    Searches every account unless one is named: matches from all accounts are merged
    into one list in the requested order and paged after merging. With several
    accounts each message, including context messages, carries an `account` field.

    Args:
        after: ISO-8601 date string (e.g., "2026-01-01" or "2026-01-01T09:00:00")
        before: ISO-8601 date string (e.g., "2026-01-09" or "2026-01-09T18:00:00")
        sender_phone_number: Phone number to filter by sender (e.g., "12025551234")
        chat_jid: Chat JID to filter by (e.g., "12025551234@s.whatsapp.net" or group JID)
        query: Search term to filter messages by content
        limit: Max messages to return (default 50, max 500)
        page: Page number for pagination (default 0)
        include_context: Include surrounding messages for context (default True)
        context_before: Messages to include before each match (default 1)
        context_after: Messages to include after each match (default 1)
        sort_by: "newest" (default, most recent first) or "oldest" (chronological)
        account: Account to search (see list_accounts); omit to search every account
    """
    # Cap limit at 500 to prevent excessive queries
    limit = max(1, min(limit, 500))
    page = max(0, page)
    filters = {
        "after": after,
        "before": before,
        "sender_phone_number": sender_phone_number,
        "chat_jid": chat_jid,
        "query": query,
        "sort_by": sort_by,
    }
    targets = accounts.registry().select(account)
    if len(targets) == 1:
        messages = whatsapp_list_messages(
            targets[0],
            limit=limit,
            page=page,
            include_context=include_context,
            context_before=context_before,
            context_after=context_after,
            **filters,
        )
        return _tagged(targets[0], messages)

    # Page the matches first, then add each match's context from its own account.
    per_account = _read_each(
        targets, lambda target: whatsapp_find_messages(target, limit=(page + 1) * limit, page=0, **filters)
    )
    matches = _merged_page(
        per_account,
        key=lambda message: _instant(message.timestamp),
        reverse=sort_by == "newest",
        limit=limit,
        page=page,
    )
    rows = whatsapp_with_context(matches, include_context, context_before, context_after)
    return [{**msg_to_dict(target, message), "account": target.name} for target, message in rows]


@mcp.tool()
def list_chats(
    query: str | None = None,
    limit: int = 50,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
    account: AccountName = None,
) -> list[dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.

    Lists every account's chats unless one is named, merged in the requested order
    and paged after merging. A chat that exists in two accounts is listed once per
    account; with several accounts each chat carries an `account` field.

    Args:
        query: Search term to filter chats by name or JID
        limit: Max chats to return (default 50, max 200)
        page: Page number for pagination (default 0)
        include_last_message: Include the last message in each chat (default True)
        sort_by: "last_active" (default, most recent first) or "name" (alphabetical)
        account: Account to list (see list_accounts); omit to list every account

    Returns:
        Chat dictionaries with jid, name, is_group, last_message_time, last_message,
        last_sender, last_is_from_me, last_read_time and unread. `last_read_time` is
        how far the chat has been read on any device (null if never reported); `unread`
        is true when the last message is inbound and newer than that marker, so chats
        already read on the phone are not reported as unread. Numeric fallback names
        are resolved from local contacts when possible, even while the bridge is offline.
    """
    # Cap limit at 200 to prevent excessive queries
    limit = max(1, min(limit, 200))
    page = max(0, page)
    return _chats_page(
        accounts.registry().select(account),
        lambda target, rows, offset_page: whatsapp_list_chats(
            target,
            query=query,
            limit=rows,
            page=offset_page,
            include_last_message=include_last_message,
            sort_by=sort_by,
        ),
        sort_by,
        limit,
        page,
    )


def _last_active(_account: Account, chat: dict[str, Any]) -> str | None:
    return chat.get("last_message_time")


@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True, account: AccountName = None) -> dict[str, Any]:
    """Get WhatsApp chat metadata by JID.

    Looks in every account unless one is named. When several accounts have the
    chat, the most recently active one is returned and `also_in_accounts` names
    the others.

    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
        account: Account to look in (see list_accounts); omit to look in every account

    Returns:
        Chat dictionary — same shape as list_chats, including last_read_time and unread —
        or null if no account has the chat.
    """
    match = _look_up(
        accounts.registry().select(account),
        lambda target: whatsapp_get_chat(target, chat_jid, include_last_message),
        _last_active,
    )
    return _located(*match) if match else None


@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str, account: AccountName = None) -> dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.

    Looks in every account unless one is named. When several accounts have a
    direct chat with the number, the most recently active one is returned and
    `also_in_accounts` names the others.

    Args:
        sender_phone_number: The phone number to search for
        account: Account to look in (see list_accounts); omit to look in every account
    """
    match = _look_up(
        accounts.registry().select(account),
        lambda target: whatsapp_get_direct_chat_by_contact(target, sender_phone_number),
        _last_active,
    )
    return _located(*match) if match else None


@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0, account: AccountName = None) -> list[dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.

    Covers every account unless one is named, merged by last activity and paged
    after merging. With several accounts each chat carries an `account` field.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        account: Account to search (see list_accounts); omit to search every account
    """
    limit = max(1, min(limit, 200))
    page = max(0, page)
    return _chats_page(
        accounts.registry().select(account),
        lambda target, rows, offset_page: whatsapp_get_contact_chats(target, jid, rows, offset_page),
        "last_active",
        limit,
        page,
    )


@mcp.tool()
def get_last_interaction(jid: str, account: AccountName = None) -> dict[str, Any]:
    """Get most recent WhatsApp message involving the contact.

    Looks in every account unless one is named. When several accounts have
    messages with the contact, the newest one is returned and `also_in_accounts`
    names the others.

    Args:
        jid: The JID of the contact to search for
        account: Account to look in (see list_accounts); omit to look in every account

    Returns:
        Message dictionary with id, timestamp, sender, content, etc. or empty dict if not found.
    """
    match = _look_up(
        accounts.registry().select(account),
        lambda target: whatsapp_get_last_interaction(target, jid),
        lambda _target, message: message["timestamp"],
    )
    return _located(*match) if match else {}


@mcp.tool()
def get_message_context(
    message_id: str, before: int = 5, after: int = 5, account: AccountName = None
) -> dict[str, Any]:
    """Get context around a specific WhatsApp message.

    Looks in every account unless one is named. A message between two of your
    accounts is held by both: the one whose chat was most recently active is
    returned and `also_in_accounts` names the others. With several accounts the
    result and each message carry an `account` field.

    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
        account: Account to look in (see list_accounts); omit to look in every account
    """

    def context_in(target: Account) -> MessageContext | None:
        try:
            return whatsapp_get_message_context(target, message_id, before, after)
        except MessageNotFoundError:
            return None

    match = _look_up(
        accounts.registry().select(account),
        context_in,
        lambda target, context: _last_active(
            target, whatsapp_get_chat(target, context.message.chat_jid, include_last_message=False) or {}
        ),
    )
    if match is None:
        raise MessageNotFoundError(f"Message with ID {message_id} not found")
    target, context, also_in = match

    def rows(messages: list[Message]) -> list[dict[str, Any]]:
        return _tagged(target, [msg_to_dict(target, message) for message in messages])

    return _located(
        target,
        {"message": rows([context.message])[0], "before": rows(context.before), "after": rows(context.after)},
        also_in,
    )


@mcp.tool()
def send_message(
    recipient: str,
    message: str,
    quoted_message_id: str = "",
    quoted_sender_jid: str = "",
    quoted_content: str = "",
    mentions: list[str] | None = None,
    account: SendingAccount = None,
) -> dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    With several accounts configured, `account` is required: ask the user which
    account (number) to send from. A reply names its account too, for now.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send
        quoted_message_id: ID of the message to reply to (optional). When set, the sent
                           message will appear as a quoted reply in WhatsApp.
        quoted_sender_jid: Full JID of the author of the quoted message. Required for
                           group replies so WhatsApp renders the correct attribution.
        quoted_content: Text content of the quoted message, used for the reply preview.
                        Only plain text is supported; media previews are not included.
        mentions: Users to @-mention, as phone numbers with country code but no + (e.g.
                  ["420601234567"]) or JIDs. For each entry the message text must contain
                  a matching "@<number>" token (e.g. "hi @420601234567"), otherwise the
                  mention won't render on recipients' devices. Only meaningful in groups.
        account: Account to send from (see list_accounts); required when several are configured

    Returns:
        A dictionary containing success status and a status message; with several
        accounts also `account` and `account_phone_number`, the number it went out from
    """
    sender = accounts.registry().sender(account)
    # Validate input
    if not recipient:
        return _sent(sender, False, "Recipient must be provided")

    # Call the whatsapp_send_message function with the unified recipient parameter
    success, status_message = whatsapp_send_message(
        sender, recipient, message, quoted_message_id, quoted_sender_jid, quoted_content, mentions
    )
    return _sent(sender, success, status_message)


@mcp.tool()
def send_reaction(
    recipient: str,
    message_id: str,
    emoji: str,
    from_me: bool = False,
    sender_jid: str = "",
) -> dict[str, Any]:
    """Send (or remove) a reaction to a WhatsApp message.

    Args:
        recipient: The chat JID the message belongs to (e.g., "12025551234@s.whatsapp.net"
                   or a group JID like "123456789@g.us")
        message_id: The ID of the message to react to
        emoji: The reaction emoji (e.g., "👍"). Pass an empty string to remove the reaction.
        from_me: Whether the original message was sent by the current user (default False)
        sender_jid: JID of the original message sender — required for group messages when
                    from_me is False so the bridge can build the correct WhatsApp key

    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_send_reaction(_account(), recipient, message_id, emoji, from_me, sender_jid)
    return {"success": success, "message": status_message}


@mcp.tool()
def mark_messages_read(
    message_ids: list[str],
    chat_jid: str,
    sender_jid: str = "",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Mark selected WhatsApp messages as read and send read receipts.

    This is an explicit external side effect. All message IDs must belong to the
    same chat and sender.

    Args:
        message_ids: IDs of the messages to mark as read
        chat_jid: JID of the chat containing the messages
        sender_jid: JID or bare phone number of the original sender; required for groups
        timestamp: Optional RFC 3339 read timestamp; defaults to the current time

    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_mark_messages_read(_account(), message_ids, chat_jid, sender_jid, timestamp)
    return {"success": success, "message": status_message}


@mcp.tool()
def send_file(recipient: str, media_path: str, caption: str = "", account: SendingAccount = None) -> dict[str, Any]:
    """Send a file (image, video, document) via WhatsApp, optionally with a caption.

    When `caption` is provided, the file and text arrive as a single
    attachment-with-caption message (one bubble in the WA UI), instead of
    needing a separate follow-up send_message call. For group chats use the JID.
    With several accounts configured, `account` is required: ask the user which
    account (number) to send from.

    Args:
        recipient: Either a phone number with country code (no + or symbols),
                 or a JID (e.g., "123456789@s.whatsapp.net" or "123456789@g.us")
        media_path: Absolute path to the media file (image, video, document)
        caption: Optional text rendered with the file as a caption. Omit for a
                 bare attachment.
        account: Account to send from (see list_accounts); required when several are configured

    Returns:
        A dictionary containing success status and a status message; with several
        accounts also `account` and `account_phone_number`, the number it went out from
    """
    sender = accounts.registry().sender(account)
    success, status_message = whatsapp_send_file(sender, recipient, media_path, caption)
    return _sent(sender, success, status_message)


@mcp.tool()
def send_audio_message(recipient: str, media_path: str, account: SendingAccount = None) -> dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.

    With several accounts configured, `account` is required: ask the user which
    account (number) to send from.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file)
        account: Account to send from (see list_accounts); required when several are configured

    Returns:
        A dictionary containing success status and a status message; with several
        accounts also `account` and `account_phone_number`, the number it went out from
    """
    sender = accounts.registry().sender(account)
    success, status_message = whatsapp_audio_voice_message(sender, recipient, media_path)
    return _sent(sender, success, status_message)


@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(_account(), message_id, chat_jid)

    if file_path:
        return {"success": True, "message": "Media downloaded successfully", "file_path": file_path}
    else:
        return {"success": False, "message": "Failed to download media"}


@mcp.tool()
def view_media(
    message_id: str,
    chat_jid: str,
    max_dimension: PreviewDimension = media_preview.DEFAULT_MAX_DIMENSION,
) -> Any:
    """View the media of a WhatsApp message as an image.

    download_media only returns a local file path, which a client without
    filesystem access cannot open. This returns the picture itself instead.
    Videos return their first frame, which is enough to tell what was sent.
    Both are downscaled so a single photo cannot flood the context. Voice notes
    are not images — use transcribe_audio or read the transcript from the
    message content with list_messages.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
        max_dimension: Longest edge of the returned image in pixels (default 1024)

    Returns:
        Image content on success, otherwise a dictionary explaining why not
    """
    try:
        media_preview.validate_max_dimension(max_dimension)
    except media_preview.PreviewError as exc:
        return {"success": False, "message": str(exc)}

    file_path = whatsapp_download_media(_account(), message_id, chat_jid)
    if not file_path:
        return {"success": False, "message": "Failed to download media"}

    with tempfile.TemporaryDirectory() as work_dir:
        try:
            data, image_format = media_preview.render_preview(file_path, max_dimension=max_dimension, work_dir=work_dir)
        except media_preview.PreviewError as exc:
            return {"success": False, "message": str(exc), "file_path": file_path}

    return Image(data=data, format=image_format)


@mcp.tool()
def transcribe_audio(message_id: str, chat_jid: str, force: bool = False) -> dict[str, Any]:
    """Transcribe a WhatsApp voice note and return its text.

    Runs whisper.cpp locally by default, or sends audio to the operator's
    configured OpenAI-compatible endpoint. The transcript is also written into
    the message's empty content field, so afterwards it is readable through list_messages by any
    client — including one with no filesystem access — without transcribing
    again.

    Call this for a voice note whose content field is still empty. Requires
    whisper.cpp, FFmpeg, and WHISPER_MODEL for the default provider; alternatively
    configure WHATSAPP_TRANSCRIPTION_PROVIDER=openai_compatible, URL and MODEL.

    Args:
        message_id: The ID of the message containing the voice note
        chat_jid: The JID of the chat containing the message
        force: Transcribe again even when a transcript is already stored

    Returns:
        A dictionary with success status and the transcript
    """
    account = _account()
    if not force:
        existing = transcription.stored_transcript(account.messages_db_path, message_id, chat_jid)
        if existing:
            return {"success": True, "message": "Transcript already stored", "transcript": existing}

    # WhatsApp expires media server-side after a few weeks, so the file has to
    # be on disk. This is a no-op when the bridge already downloaded it.
    file_path = whatsapp_download_media(account, message_id, chat_jid)
    if not file_path:
        return {
            "success": False,
            "message": (
                "Could not obtain the audio file. WhatsApp expires media after a while, "
                "so an old voice note may no longer be downloadable."
            ),
        }
    if not transcription.is_audio(file_path):
        return {
            "success": False,
            "message": "This message is not audio. Use download_media instead.",
            "file_path": file_path,
        }

    try:
        provider = transcription.provider_name()
        model = transcription.configured_model(provider)
        with tempfile.TemporaryDirectory() as work_dir:
            text = transcription.transcribe_file(file_path, work_dir, model=model, provider=provider)
    except transcription.TranscriptionError as exc:
        return {"success": False, "message": str(exc)}

    if not transcription.store_transcript(
        account.messages_db_path, message_id, chat_jid, text, model, provider=provider
    ):
        # The words are worth returning even when the row could not be updated,
        # but say so: without the row, list_messages will not show them.
        return {
            "success": True,
            "message": "Transcribed, but the transcript could not be stored on the message row",
            "transcript": text,
        }

    return {
        "success": True,
        "message": "Transcribed",
        "transcript": f"{transcription.label(model, provider)}{text}",
    }


def shutdown_handler(signum, frame):
    """Handle shutdown signals gracefully to prevent zombie processes."""
    sys.exit(0)


if __name__ == "__main__":
    # Capture before any await — os.getppid() is dynamic.
    parent_pid = os.getppid()
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Resolve the transport first: host/port are only used (and validated) for the
    # network transports, so a bad WHATSAPP_MCP_PORT can't break a stdio launch.
    # The localhost default keeps a remote server unreachable until explicitly opened up.
    # An unusable accounts file stops startup instead of silently dropping an account.
    try:
        accounts.registry()
        transport = resolve_transport(os.getenv("WHATSAPP_MCP_TRANSPORT"))
        if transport != "stdio":
            mcp.settings.host = resolve_host(os.getenv("WHATSAPP_MCP_HOST"))
            mcp.settings.port = resolve_port(os.getenv("WHATSAPP_MCP_PORT"))
            # stdout is reserved for the protocol on stdio; log startup to stderr.
            print(
                f"WhatsApp MCP server listening on {mcp.settings.host}:{mcp.settings.port} via {transport}",
                file=sys.stderr,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    if transport == "stdio":
        install_stdio_parent_watchdog("WHATSAPP_PARENT_WATCHDOG_S", parent_pid=parent_pid)
    mcp.run(transport=transport)

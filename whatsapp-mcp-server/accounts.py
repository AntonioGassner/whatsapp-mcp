"""Account registry: which WhatsApp numbers this server serves, and where each one lives.

An account is one WhatsApp number served by its own bridge, with its own store
folder (messages.db, whatsapp.db, .bridge-token) and its own REST URL. The
bridges stay single-account processes; only this server learns to talk to
several of them.

With WHATSAPP_ACCOUNTS_FILE set, the accounts come from that JSON file. Without
it, one implicit account named "default" is built from the single-account
settings (WHATSAPP_DB_PATH, WHATSMEOW_DB_PATH, WHATSAPP_API_URL,
WHATSAPP_BRIDGE_TOKEN), so existing setups keep working unchanged.
"""

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

ACCOUNTS_FILE_ENV = "WHATSAPP_ACCOUNTS_FILE"
DEFAULT_ACCOUNT_NAME = "default"

_DEFAULT_BRIDGE_STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store")
_NAME_PATTERN = re.compile(r"[a-z0-9-]+")
_ENTRY_FIELDS = ("name", "store_dir", "api_url")


class AccountsFileError(ValueError):
    """The accounts file cannot be used; the message names the entry and the problem."""


@dataclass(frozen=True)
class Account:
    name: str
    messages_db_path: str
    whatsmeow_db_path: str
    api_url: str
    # Only the implicit default account honours WHATSAPP_BRIDGE_TOKEN; accounts
    # from the file always read the token their own bridge wrote.
    token_override: str | None = None
    # What to tell the user when messages.db is missing.
    missing_store_hint: str = "Start the bridge once so it creates the store, or point WHATSAPP_DB_PATH at it."

    @property
    def token_path(self) -> str:
        # The bridge writes .bridge-token into its store folder, next to whatsapp.db.
        return os.path.join(os.path.dirname(self.whatsmeow_db_path), ".bridge-token")

    def bridge_token(self) -> str | None:
        if self.token_override:
            return self.token_override
        # Read on every call: the bridge may regenerate its token.
        try:
            with open(self.token_path, encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    def bridge_headers(self) -> dict[str, str]:
        token = self.bridge_token()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class Registry:
    accounts: tuple[Account, ...]

    def first(self) -> Account:
        return self.accounts[0]

    @property
    def several(self) -> bool:
        """Whether results must say which account they came from."""
        return len(self.accounts) > 1

    def get(self, name: str) -> Account:
        """The account with this name.

        Raises:
            ValueError: naming the valid accounts, so a typo isn't read as "nothing found".
        """
        for account in self.accounts:
            if account.name == name:
                return account
        valid = ", ".join(account.name for account in self.accounts)
        raise ValueError(f"Unknown account {name!r}; valid accounts: {valid}")

    def select(self, name: str | None) -> tuple[Account, ...]:
        """The accounts a read covers: the named one, or every account when no name is given.

        An empty name counts as no name: clients often send "" for an omitted string.
        """
        if not name:
            return self.accounts
        return (self.get(name),)

    def sender(self, name: str | None) -> Account:
        """The account a new message goes out from: the named one, or the only one.

        With several accounts no account is ever chosen by default: sending from
        the wrong number can't be taken back.

        Raises:
            ValueError: when several accounts are configured and none is named, or the
                name is unknown; both list the valid names.
        """
        if name:
            return self.get(name)
        if self.several:
            valid = ", ".join(account.name for account in self.accounts)
            raise ValueError(
                f"Several accounts are configured; ask the user which one to send from and pass it as account; "
                f"valid accounts: {valid}"
            )
        return self.first()

    def locate(self, holds: Callable[[Account], bool]) -> tuple[Account, ...]:
        """The message locator: the accounts whose store holds a message, in file order.

        holds(account) checks one account's messages store. An account whose
        store is missing (FileNotFoundError) can't hold anything and is left out.
        """
        found = []
        for account in self.accounts:
            try:
                if holds(account):
                    found.append(account)
            except FileNotFoundError:
                continue
        return tuple(found)

    def holder(
        self, name: str | None, holds: Callable[[Account, str], bool], message_ids: Sequence[str], where: str
    ) -> Account:
        """The account an action on existing messages goes through: the one that holds them.

        With one account nothing is looked up and that account acts, as it always
        did. With several, the message locator decides, message by message:
        messages held by one account use it; messages that several accounts hold
        (a message in a group two of the user's numbers are in) or that no account
        holds need the account named. Messages held by different accounts are
        refused: no single account can act on all of them.
        A named account is used only if it holds every message that some account holds.
        holds(account, message_id) checks one store; `where` names the chat in errors.

        Raises:
            ValueError: when the account can't be decided or the named one doesn't
                hold a message; each says which accounts can be named.
        """
        named = self.get(name) if name else None
        if not self.several:
            return named or self.first()
        located = {}
        for message_id in message_ids:
            held_by = self.locate(lambda account, message_id=message_id: holds(account, message_id))
            if held_by:
                located[message_id] = held_by
        if named:
            for message_id, held_by in located.items():
                if named not in held_by:
                    raise ValueError(
                        f"Account {named.name!r} does not hold message {message_id!r} {where}; "
                        f"it is held by: {_names(held_by)}"
                    )
            return named
        unheld = [message_id for message_id in message_ids if message_id not in located]
        if unheld or not located:
            raise ValueError(
                f"No account holds {_messages(unheld)} {where}; ask the user which account to act from "
                f"and pass it as account; valid accounts: {_names(self.accounts)}"
            )
        common = [account for account in self.accounts if all(account in held_by for held_by in located.values())]
        if len(common) == 1:
            return common[0]
        if common:
            what = _messages(list(located))
            raise ValueError(
                f"{what[0].upper()}{what[1:]} {where} {'are' if len(located) > 1 else 'is'} held by several "
                f"accounts: {_names(common)}; ask the user which one to act from and pass it as account"
            )
        spread = ", ".join(f"{message_id!r} by {_names(held_by)}" for message_id, held_by in located.items())
        raise ValueError(
            f"Messages {where} are held by different accounts: {spread}; "
            "act on each account's messages in a separate call"
        )


def _names(accounts: Sequence[Account]) -> str:
    return ", ".join(account.name for account in accounts)


def _messages(message_ids: Sequence[str]) -> str:
    """How errors name some messages: "message 'a'" or "messages 'a', 'b'"."""
    if not message_ids:
        return "the messages"
    plural = "s" if len(message_ids) > 1 else ""
    return f"message{plural} {', '.join(repr(message_id) for message_id in message_ids)}"


def _default_account(environ: Mapping[str, str]) -> Account:
    return Account(
        name=DEFAULT_ACCOUNT_NAME,
        messages_db_path=environ.get("WHATSAPP_DB_PATH", os.path.join(_DEFAULT_BRIDGE_STORE_DIR, "messages.db")),
        whatsmeow_db_path=environ.get("WHATSMEOW_DB_PATH", os.path.join(_DEFAULT_BRIDGE_STORE_DIR, "whatsapp.db")),
        api_url=environ.get("WHATSAPP_API_URL", "http://localhost:8080/api"),
        token_override=environ.get("WHATSAPP_BRIDGE_TOKEN", "").strip() or None,
    )


def _entry_label(index: int, entry: object) -> str:
    name = entry.get("name") if isinstance(entry, dict) else None
    return f"entry {index}" + (f" ({name!r})" if isinstance(name, str) and name else "")


def _account_from_entry(index: int, entry: object, base_dir: str, path: str) -> Account:
    label = _entry_label(index, entry)
    if not isinstance(entry, dict):
        raise AccountsFileError(f"Accounts file {path}: {label} must be a JSON object with {', '.join(_ENTRY_FIELDS)}")
    unknown = sorted(set(entry) - set(_ENTRY_FIELDS))
    if unknown:
        raise AccountsFileError(
            f"Accounts file {path}: {label} has unknown field(s) {', '.join(unknown)}; "
            f"allowed fields are {', '.join(_ENTRY_FIELDS)}"
        )
    for field in _ENTRY_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise AccountsFileError(f"Accounts file {path}: {label} is missing {field} (a non-empty string)")

    name = entry["name"]
    if not _NAME_PATTERN.fullmatch(name):
        raise AccountsFileError(
            f"Accounts file {path}: {label} has an invalid name; use only lowercase letters, digits and hyphens"
        )

    api_url = entry["api_url"].strip().rstrip("/")
    try:
        parsed = urlsplit(api_url)
        valid_url = parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise AccountsFileError(
            f"Accounts file {path}: {label} has an invalid api_url {api_url!r}; "
            "expected the bridge REST base URL, e.g. http://localhost:8081/api"
        )

    # A relative store_dir is relative to the accounts file, not to wherever the
    # MCP client happened to launch the server from.
    store_dir = os.path.normpath(os.path.join(base_dir, os.path.expanduser(entry["store_dir"].strip())))
    return Account(
        name=name,
        messages_db_path=os.path.join(store_dir, "messages.db"),
        whatsmeow_db_path=os.path.join(store_dir, "whatsapp.db"),
        api_url=api_url,
        missing_store_hint=(
            f"Start the bridge for account {name!r} once so it creates the store, or fix its store_dir in {path}."
        ),
    )


def _load_accounts_file(path: str) -> Registry:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise AccountsFileError(f"Cannot read accounts file {path}: {exc}") from None
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AccountsFileError(f"Accounts file {path} is not valid JSON: {exc}") from None
    if not isinstance(entries, list):
        raise AccountsFileError(f"Accounts file {path} must hold a JSON list of accounts")
    if not entries:
        raise AccountsFileError(f"Accounts file {path} lists no accounts; add at least one")

    base_dir = os.path.dirname(os.path.abspath(path))
    seen: dict[tuple[str, str], int] = {}
    loaded = []
    for index, entry in enumerate(entries, start=1):
        account = _account_from_entry(index, entry, base_dir, path)
        store_dir = os.path.dirname(account.messages_db_path)
        # A shared store or bridge would read one number's messages as another's,
        # or send one account's bridge token to another account's bridge.
        for field, value, key in (
            ("name", account.name, account.name),
            ("store_dir", store_dir, os.path.normcase(store_dir)),
            ("api_url", account.api_url, account.api_url),
        ):
            if (field, key) in seen:
                raise AccountsFileError(
                    f"Accounts file {path}: {_entry_label(index, entry)} repeats the {field} {value!r} "
                    f"of entry {seen[field, key]}"
                )
            seen[field, key] = index
        loaded.append(account)
    return Registry(accounts=tuple(loaded))


def load(environ: Mapping[str, str] | None = None) -> Registry:
    """Build the registry from the environment.

    Raises:
        AccountsFileError: when WHATSAPP_ACCOUNTS_FILE is set but unusable.
    """
    environ = os.environ if environ is None else environ
    path = environ.get(ACCOUNTS_FILE_ENV, "").strip()
    if not path:
        return Registry(accounts=(_default_account(environ),))
    return _load_accounts_file(os.path.expanduser(path))


_registry: Registry | None = None


def registry() -> Registry:
    """The registry for this process, loaded on first use.

    The accounts file is read once; changes apply on restart.
    """
    global _registry
    if _registry is None:
        _registry = load()
    return _registry


def reset() -> None:
    """Forget the loaded registry so the next use reads the environment again."""
    global _registry
    _registry = None

"""Durable idempotency claims for Chatwoot deliveries."""

from __future__ import annotations

import sqlite3
import threading
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from parser import normalize_message_id, normalize_subject


def message_id_key(message_id: str) -> str:
    """The lookup key for one RFC822 Message-ID."""
    normalized = normalize_message_id(message_id)
    return f"msgid:{normalized}" if normalized else ""


def participant_subject_key(subject: str, email: str) -> str:
    """The last-resort key: one thread subject as seen by one participant."""
    normalized_subject = normalize_subject(subject)
    normalized_email = str(email or "").strip().lower()
    if not normalized_subject or not normalized_email:
        return ""
    return f"thread:{normalized_subject}|{normalized_email}"


def inbound_thread_keys(message) -> list[tuple[str, bool]]:
    """Every key an inbound message proves points at its conversation.

    A Message-ID is globally unique, so the first conversation to claim one
    keeps it. A subject-and-participant key is a guess, so the newest
    conversation wins it — that is the thread a person replying now means.
    """
    keys: list[tuple[str, bool]] = []

    def add(key: str, unique: bool) -> None:
        if key and all(key != existing for existing, _ in keys):
            keys.append((key, unique))

    add(message_id_key(getattr(message, "rfc822_message_id", "")), True)
    add(message_id_key(getattr(message, "in_reply_to", "")), True)
    for reference in getattr(message, "references", None) or []:
        add(message_id_key(reference), True)

    subject = getattr(message, "subject", "")
    participants = [getattr(message, "from_email", "")]
    participants.extend(getattr(message, "to_emails", None) or [])
    for participant in participants:
        add(participant_subject_key(subject, participant), False)
    return keys


class DedupStore:
    """Make one durable claim per normalized inbound message key."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS message_claim (
                    claim_key TEXT PRIMARY KEY,
                    claimed_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS inbound_message (
                    account_id INTEGER NOT NULL,
                    conversation_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    normalized_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, message_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS thread_key (
                    thread_key TEXT PRIMARY KEY,
                    conversation_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL DEFAULT 0,
                    unique_key INTEGER NOT NULL DEFAULT 1,
                    observed_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def claim(self, key: str) -> bool:
        if not key:
            raise ValueError("claim key must not be empty")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO message_claim(claim_key, claimed_at) VALUES (?, ?)",
                (key, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount == 1

    def release(self, key: str) -> None:
        """Release an unsuccessful action so a fresh human command can retry it."""
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM message_claim WHERE claim_key = ?", (key,))

    def record_inbound(self, message) -> bool:
        """Persist one normalized inbound message; return false for a retry."""
        if message.message_id is None or message.conversation_id is None:
            raise ValueError("inbound message requires message and conversation IDs")
        # Outside the lock: the thread map is what a Sent reply resolves against,
        # and no call site should have to remember to build it separately.
        self.record_thread_keys(message)
        normalized = asdict(message)
        normalized.pop("raw", None)
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO inbound_message(
                    account_id, conversation_id, message_id, normalized_json, observed_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    message.account_id or 0,
                    message.conversation_id,
                    message.message_id,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def latest_inbound_id(self, conversation_id: int) -> int | None:
        """Return the newest inbound already observed for a conversation."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(message_id) FROM inbound_message WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def record_thread_keys(self, message) -> int:
        """Persist the thread map an inbound message proves; return keys written.

        The bot token cannot list or search conversations — Chatwoot denies bots
        the account-wide endpoints — so a reply that arrives outside Chatwoot can
        only be placed by a map the bridge built while it could still see the
        inbound side.
        """
        conversation_id = getattr(message, "conversation_id", None)
        if not conversation_id:
            return 0
        account_id = getattr(message, "account_id", None) or 0
        now = datetime.now(timezone.utc).isoformat()
        written = 0
        with self._lock, self._connect() as connection:
            for key, unique in inbound_thread_keys(message):
                if unique:
                    cursor = connection.execute(
                        """INSERT OR IGNORE INTO thread_key(
                            thread_key, conversation_id, account_id, unique_key, observed_at
                        ) VALUES (?, ?, ?, 1, ?)""",
                        (key, int(conversation_id), int(account_id), now),
                    )
                else:
                    cursor = connection.execute(
                        """INSERT INTO thread_key(
                            thread_key, conversation_id, account_id, unique_key, observed_at
                        ) VALUES (?, ?, ?, 0, ?)
                        ON CONFLICT(thread_key) DO UPDATE SET
                            conversation_id = excluded.conversation_id,
                            account_id = excluded.account_id,
                            observed_at = excluded.observed_at
                        WHERE thread_key.unique_key = 0
                          AND excluded.conversation_id > thread_key.conversation_id""",
                        (key, int(conversation_id), int(account_id), now),
                    )
                written += cursor.rowcount if cursor.rowcount > 0 else 0
        return written

    def resolve_thread(self, keys) -> int | None:
        """Return the conversation for the first key that is already mapped."""
        with self._lock, self._connect() as connection:
            for key in keys:
                if not key:
                    continue
                row = connection.execute(
                    "SELECT conversation_id FROM thread_key WHERE thread_key = ?",
                    (key,),
                ).fetchone()
                if row and row[0] is not None:
                    return int(row[0])
        return None

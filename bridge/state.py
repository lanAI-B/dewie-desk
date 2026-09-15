"""Durable idempotency claims for Chatwoot deliveries."""

from __future__ import annotations

import sqlite3
import threading
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


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

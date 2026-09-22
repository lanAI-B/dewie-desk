"""Durable idempotency claims for Chatwoot deliveries."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
import threading
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

OUTBOUND_FINAL_STATUSES = frozenset({"accepted", "rejected", "unknown"})


@dataclass(frozen=True)
class OutboundRecord:
    """Current state of one idempotency key for a customer-visible message."""

    idempotency_key: str
    conversation_id: int
    content_sha256: str
    actor: str
    source: str
    status: str
    attempts: int
    chatwoot_message_id: int | None
    http_status: int | None
    detail: str
    created_at: str
    updated_at: str


_OUTBOUND_COLUMNS = ", ".join(OutboundRecord.__dataclass_fields__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                """CREATE TABLE IF NOT EXISTS outbound_message (
                    idempotency_key TEXT PRIMARY KEY,
                    conversation_id INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'accepted', 'rejected', 'unknown')
                    ),
                    attempts INTEGER NOT NULL,
                    chatwoot_message_id INTEGER,
                    http_status INTEGER,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS outbound_attempt (
                    idempotency_key TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    chatwoot_message_id INTEGER,
                    http_status INTEGER,
                    detail TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY (idempotency_key, attempt)
                )"""
            )

    @contextmanager
    def _connect(self):
        """Commit on success, roll back on error, and always close.

        `with sqlite3.connect(...)` alone commits but never closes (review #16).
        """
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _transaction(self) -> sqlite3.Connection:
        """Open a connection holding SQLite's write lock until commit/rollback."""
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def _outbound_row(connection: sqlite3.Connection, key: str) -> OutboundRecord | None:
        row = connection.execute(
            f"SELECT {_OUTBOUND_COLUMNS} FROM outbound_message WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return OutboundRecord(*row) if row else None

    def begin_outbound(
        self,
        key: str,
        conversation_id: int,
        content_sha256: str,
        actor: str,
        source: str,
    ) -> tuple[str, OutboundRecord]:
        """Durably claim an outbound send before any external call.

        Returns ``("claimed", record)`` when the caller must now make exactly one
        Chatwoot attempt, ``("replay", record)`` when the key already has an
        accepted, unknown, or in-flight result, and ``("conflict", record)`` when
        the key was used for a different conversation or message. Only a
        definitive rejection may be claimed again, as a numbered new attempt.
        """
        if not key:
            raise ValueError("idempotency key must not be empty")
        now = _now()
        with self._lock:
            connection = self._transaction()
            try:
                existing = self._outbound_row(connection, key)
                if existing is None:
                    attempt = 1
                    connection.execute(
                        f"""INSERT INTO outbound_message({_OUTBOUND_COLUMNS})
                        VALUES (?, ?, ?, ?, ?, 'pending', 1, NULL, NULL, '', ?, ?)""",
                        (key, conversation_id, content_sha256, actor, source, now, now),
                    )
                elif (existing.conversation_id, existing.content_sha256) != (
                    conversation_id,
                    content_sha256,
                ):
                    connection.execute("ROLLBACK")
                    return "conflict", existing
                elif existing.status != "rejected":
                    connection.execute("ROLLBACK")
                    return "replay", existing
                else:
                    attempt = existing.attempts + 1
                    connection.execute(
                        """UPDATE outbound_message SET status = 'pending', attempts = ?,
                            actor = ?, source = ?, chatwoot_message_id = NULL,
                            http_status = NULL, detail = '', updated_at = ?
                        WHERE idempotency_key = ? AND status = 'rejected'""",
                        (attempt, actor, source, now, key),
                    )
                connection.execute(
                    """INSERT INTO outbound_attempt(
                        idempotency_key, attempt, actor, source, status, detail, started_at
                    ) VALUES (?, ?, ?, ?, 'pending', '', ?)""",
                    (key, attempt, actor, source, now),
                )
                record = self._outbound_row(connection, key)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return "claimed", record

    def finish_outbound(
        self,
        key: str,
        status: str,
        *,
        chatwoot_message_id: int | None = None,
        http_status: int | None = None,
        detail: str = "",
    ) -> OutboundRecord:
        """Record the one final outcome of a pending attempt."""
        if status not in OUTBOUND_FINAL_STATUSES:
            raise ValueError(f"invalid final outbound status: {status!r}")
        now = _now()
        with self._lock:
            connection = self._transaction()
            try:
                existing = self._outbound_row(connection, key)
                if existing is None or existing.status != "pending":
                    raise ValueError(f"outbound {key!r} has no pending attempt")
                values = (status, chatwoot_message_id, http_status, detail)
                connection.execute(
                    """UPDATE outbound_message SET status = ?, chatwoot_message_id = ?,
                        http_status = ?, detail = ?, updated_at = ?
                    WHERE idempotency_key = ?""",
                    (*values, now, key),
                )
                connection.execute(
                    """UPDATE outbound_attempt SET status = ?, chatwoot_message_id = ?,
                        http_status = ?, detail = ?, finished_at = ?
                    WHERE idempotency_key = ? AND attempt = ?""",
                    (*values, now, key, existing.attempts),
                )
                record = self._outbound_row(connection, key)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return record

    def get_outbound(self, key: str) -> OutboundRecord | None:
        with self._lock, self._connect() as connection:
            return self._outbound_row(connection, key)

    def outbound_attempts(self, key: str) -> list[dict]:
        """Return the append-only attempt history for one idempotency key."""
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM outbound_attempt WHERE idempotency_key = ? ORDER BY attempt",
                (key,),
            ).fetchall()
        return [dict(row) for row in rows]

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

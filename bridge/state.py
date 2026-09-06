"""Durable idempotency claims for Chatwoot deliveries."""

from __future__ import annotations

import sqlite3
import threading
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

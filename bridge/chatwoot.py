"""Thin Chatwoot API client — only the calls the bridge needs.

Deliberately tiny: post a private note, apply labels. Sending a real reply is a
human click in the Chatwoot UI, so there is intentionally no send() here.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("dewie-desk-bridge.chatwoot")


class ChatwootClient:
    def __init__(self, base_url: str, account_id: str, api_token: str, timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.account_id = account_id
        self.timeout = timeout
        self._h = {"api_access_token": api_token, "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v1/accounts/{self.account_id}{path}"

    def post_private_note(self, conversation_id: int, content: str) -> dict:
        """Post an internal note (private=true) — visible to agents, never the customer."""
        r = requests.post(
            self._url(f"/conversations/{conversation_id}/messages"),
            json={"content": content, "message_type": "outgoing", "private": True},
            headers=self._h, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def add_labels(self, conversation_id: int, labels: list[str]) -> dict:
        r = requests.post(
            self._url(f"/conversations/{conversation_id}/labels"),
            json={"labels": labels}, headers=self._h, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

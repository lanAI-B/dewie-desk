"""Minimal Chatwoot API client with no customer-facing send operation."""

from __future__ import annotations

from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class PostResult:
    ok: bool
    status_code: int | None = None
    message_id: int | None = None
    detail: str = ""


class ChatwootClient:
    def __init__(self, base_url: str, account_id: str, api_token: str, timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.account_id = account_id
        self.timeout = timeout
        self._headers = {
            "api_access_token": api_token,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v1/accounts/{self.account_id}{path}"

    def post_private_note(self, conversation_id: int, content: str) -> PostResult:
        """Post an internal note; ``private`` is deliberately not configurable."""
        if not conversation_id:
            return PostResult(False, detail="missing_conversation_id")
        try:
            response = requests.post(
                self._url(f"/conversations/{conversation_id}/messages"),
                json={"content": content, "message_type": "outgoing", "private": True},
                headers=self._headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            return PostResult(False, detail=f"{type(exc).__name__}: {exc}")
        if response.status_code // 100 != 2:
            return PostResult(
                False,
                status_code=response.status_code,
                detail=f"HTTP {response.status_code}: {response.text[:300]}",
            )
        try:
            message_id = response.json().get("id")
        except (ValueError, AttributeError):
            message_id = None
        return PostResult(True, response.status_code, message_id, "posted")

    def add_labels(self, conversation_id: int, labels: list[str]) -> dict:
        response = requests.post(
            self._url(f"/conversations/{conversation_id}/labels"),
            json={"labels": labels},
            headers=self._headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

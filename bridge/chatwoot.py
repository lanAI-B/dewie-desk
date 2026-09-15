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


@dataclass(frozen=True)
class ConversationResult:
    ok: bool
    messages: tuple[dict, ...] = ()
    meta: dict | None = None
    status_code: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class LabelResult:
    ok: bool
    labels: tuple[str, ...] = ()
    status_code: int | None = None
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

    def get_conversation(self, conversation_id: int) -> ConversationResult:
        """Fetch the current message page and metadata for one conversation."""
        if not conversation_id:
            return ConversationResult(False, detail="missing_conversation_id")
        try:
            response = requests.get(
                self._url(f"/conversations/{conversation_id}/messages"),
                headers=self._headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            return ConversationResult(False, detail=f"{type(exc).__name__}: {exc}")
        if response.status_code // 100 != 2:
            return ConversationResult(
                False,
                status_code=response.status_code,
                detail=f"HTTP {response.status_code}: {response.text[:300]}",
            )
        try:
            payload = response.json()
            messages = payload.get("payload") or []
            meta = payload.get("meta") or {}
            if not isinstance(messages, list) or not isinstance(meta, dict):
                raise ValueError("unexpected conversation response shape")
        except (ValueError, AttributeError) as exc:
            return ConversationResult(
                False,
                status_code=response.status_code,
                detail=f"invalid response: {exc}",
            )
        return ConversationResult(True, tuple(messages), meta, response.status_code, "fetched")

    def remove_label(self, conversation_id: int, label: str) -> LabelResult:
        """Remove one command label while preserving the conversation's other labels."""
        path = f"/conversations/{conversation_id}/labels"
        try:
            current = requests.get(self._url(path), headers=self._headers, timeout=self.timeout)
            if current.status_code // 100 != 2:
                return LabelResult(
                    False,
                    status_code=current.status_code,
                    detail=f"label read HTTP {current.status_code}: {current.text[:300]}",
                )
            labels = current.json().get("payload") or []
            if not isinstance(labels, list):
                raise ValueError("unexpected label response shape")
            remaining = [value for value in labels if value != label]
            if len(remaining) == len(labels):
                return LabelResult(True, tuple(labels), current.status_code, "already_removed")
            response = requests.post(
                self._url(path),
                json={"labels": remaining},
                headers=self._headers,
                timeout=self.timeout,
            )
        except (requests.RequestException, ValueError, AttributeError) as exc:
            return LabelResult(False, detail=f"{type(exc).__name__}: {exc}")
        if response.status_code // 100 != 2:
            return LabelResult(
                False,
                status_code=response.status_code,
                detail=f"label write HTTP {response.status_code}: {response.text[:300]}",
            )
        try:
            returned = response.json().get("payload") or remaining
        except (ValueError, AttributeError):
            returned = remaining
        return LabelResult(True, tuple(returned), response.status_code, "removed")

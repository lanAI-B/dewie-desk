"""Minimal Chatwoot API client.

Message visibility is fixed per method: ``post_private_note`` is always internal
and ``post_public_outgoing`` is always customer-visible. Neither accepts a flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import requests
from urllib3.exceptions import NewConnectionError

Outcome = Literal["accepted", "rejected", "unknown"]

# Statuses that prove Chatwoot refused the request without creating a message.
# 408 and every 5xx stay ambiguous: the server may have committed before failing.
_DEFINITIVE_REJECTIONS = frozenset({400, 401, 403, 404, 405, 409, 413, 415, 422, 429})

# Request errors raised before any bytes of the request can have reached Chatwoot.
_PRE_DISPATCH_ERRORS = (
    requests.ConnectTimeout,
    requests.exceptions.InvalidURL,
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
    requests.exceptions.InvalidHeader,
)


@dataclass(frozen=True)
class OutboundResult:
    """Outcome of one customer-visible message attempt.

    ``accepted`` means Chatwoot returned a created message ID. ``rejected`` means
    no message can exist. ``unknown`` means a message may exist and the attempt
    must be reconciled, never blindly resent.
    """

    outcome: Outcome
    status_code: int | None = None
    message_id: int | None = None
    detail: str = ""


def _connection_refused(exc: requests.RequestException) -> bool:
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, NewConnectionError)


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

    def post_public_outgoing(self, conversation_id: int, content: str) -> OutboundResult:
        """Post a customer-visible reply; ``private`` is deliberately not configurable."""
        if (
            isinstance(conversation_id, bool)
            or not isinstance(conversation_id, int)
            or conversation_id <= 0
        ):
            return OutboundResult("rejected", detail="invalid_conversation_id")
        if not isinstance(content, str) or not content.strip():
            return OutboundResult("rejected", detail="empty_content")
        try:
            response = requests.post(
                self._url(f"/conversations/{conversation_id}/messages"),
                json={"content": content, "message_type": "outgoing", "private": False},
                headers=self._headers,
                timeout=self.timeout,
            )
        # Details name only the error class or HTTP status: exception messages and
        # response bodies can echo the customer message, which is stored hash-only.
        except _PRE_DISPATCH_ERRORS as exc:
            return OutboundResult("rejected", detail=f"request_error:{type(exc).__name__}")
        except requests.ConnectionError as exc:
            if _connection_refused(exc):
                return OutboundResult("rejected", detail="request_error:ConnectionRefused")
            return OutboundResult("unknown", detail=f"request_error:{type(exc).__name__}")
        except requests.RequestException as exc:
            return OutboundResult("unknown", detail=f"request_error:{type(exc).__name__}")

        status = response.status_code
        if status // 100 != 2:
            outcome = "rejected" if status in _DEFINITIVE_REJECTIONS else "unknown"
            return OutboundResult(outcome, status, detail=f"http_{status}")
        try:
            message_id = response.json().get("id")
        except (ValueError, AttributeError):
            message_id = None
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            return OutboundResult("unknown", status, detail="accepted_without_message_id")
        return OutboundResult("accepted", status, message_id, "created")

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

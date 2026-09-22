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


class ChatwootError(Exception):
    """A Chatwoot call failed. The message is a bounded code, never response text."""


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

    # ── Conversation lookup/creation for internal senders ────────────────────
    # Read and create only; none of these posts a message. Errors raise
    # ChatwootError carrying a bounded code, never a response body.

    def _json(self, method: str, path: str, **kwargs):
        try:
            response = requests.request(method, self._url(path), headers=self._headers,
                                        timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ChatwootError(f"request_error:{type(exc).__name__}") from None
        if response.status_code // 100 != 2:
            raise ChatwootError(f"http_{response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ChatwootError("invalid_json") from None

    def find_contacts_by_email(self, email: str) -> list[dict]:
        """Contacts whose email equals ``email`` exactly (case-insensitive)."""
        payload = self._json("GET", "/contacts/search", params={"q": email}).get("payload") or []
        wanted = email.strip().casefold()
        return [c for c in payload
                if isinstance(c, dict) and str(c.get("email") or "").strip().casefold() == wanted]

    def contact_conversations(self, contact_id: int) -> list[dict]:
        payload = self._json("GET", f"/contacts/{int(contact_id)}/conversations").get("payload") or []
        return [c for c in payload if isinstance(c, dict)]

    def conversation_details(self, conversation_id: int) -> dict:
        return self._json("GET", f"/conversations/{int(conversation_id)}")

    def create_contact(self, inbox_id: int, email: str, name: str | None) -> dict:
        body = {"inbox_id": int(inbox_id), "email": email}
        if name:
            body["name"] = name
        data = self._json("POST", "/contacts", json=body)
        return (data.get("payload") or {}).get("contact") or data.get("payload") or data

    def contact_source_id(self, contact_id: int, inbox_id: int, email: str) -> str:
        """The contact's source id on this inbox, creating the contact-inbox link if absent."""
        contact = self._json("GET", f"/contacts/{int(contact_id)}").get("payload") or {}
        for link in contact.get("contact_inboxes") or []:
            inbox = link.get("inbox") or {}
            if inbox.get("id") == int(inbox_id) and link.get("source_id"):
                return str(link["source_id"])
        created = self._json("POST", f"/contacts/{int(contact_id)}/contact_inboxes",
                             json={"inbox_id": int(inbox_id), "source_id": email})
        source_id = created.get("source_id") or (created.get("payload") or {}).get("source_id")
        if not source_id:
            raise ChatwootError("contact_inbox_without_source_id")
        return str(source_id)

    def create_conversation(self, contact_id: int, inbox_id: int, source_id: str,
                            subject: str) -> int:
        """Open an empty conversation. No message is posted, so nothing is emailed."""
        data = self._json("POST", "/conversations", json={
            "source_id": source_id,
            "inbox_id": int(inbox_id),
            "contact_id": int(contact_id),
            "status": "open",
            "additional_attributes": {"mail_subject": subject},
        })
        conversation_id = data.get("id")
        if isinstance(conversation_id, bool) or not isinstance(conversation_id, int):
            raise ChatwootError("created_without_conversation_id")
        return conversation_id

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

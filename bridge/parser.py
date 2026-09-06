"""Defensive normalization of Chatwoot ``message_created`` webhooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedMessage:
    event: str
    account_id: int | None = None
    conversation_id: int | None = None
    inbox_id: int | None = None
    inbox_name: str | None = None
    message_id: int | None = None
    from_email: str = ""
    from_name: str = ""
    sender_type: str = ""
    subject: str = ""
    subject_source: str = ""
    body: str = ""
    message_type: str = ""
    content_type: str = ""
    is_private: bool = False
    channel: str = ""
    should_process: bool = False
    skip_reason: str = ""
    raw: dict = field(default_factory=dict, repr=False)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _sender_type(conversation: dict, sender: dict) -> str:
    direct = str(sender.get("type") or "").strip().lower()
    if direct:
        return direct
    messages = conversation.get("messages")
    if isinstance(messages, list) and messages:
        message = _dict(messages[-1])
        nested = _dict(message.get("sender"))
        found = nested.get("type") or message.get("sender_type")
        if found:
            return str(found).strip().lower()
    meta_sender = _dict(_dict(conversation.get("meta")).get("sender"))
    return str(meta_sender.get("type") or "").strip().lower()


def _subject(payload: dict, conversation: dict) -> tuple[str, str]:
    email = _dict(_dict(payload.get("content_attributes")).get("email"))
    top = _dict(payload.get("additional_attributes"))
    conversation_attributes = _dict(conversation.get("additional_attributes"))
    candidates = (
        (email.get("subject"), "content_attributes.email.subject"),
        (top.get("mail_subject"), "additional_attributes.mail_subject"),
        (conversation_attributes.get("mail_subject"),
         "conversation.additional_attributes.mail_subject"),
    )
    for value, source in candidates:
        cleaned = str(value or "").strip()
        if cleaned:
            return cleaned, source
    return "", ""


def parse_message_created(payload: dict) -> ParsedMessage:
    payload = _dict(payload)
    conversation = _dict(payload.get("conversation"))
    sender = _dict(payload.get("sender"))
    inbox = _dict(payload.get("inbox"))
    account = _dict(payload.get("account"))
    subject, subject_source = _subject(payload, conversation)

    parsed = ParsedMessage(
        event=str(payload.get("event") or "unknown"),
        account_id=account.get("id"),
        conversation_id=conversation.get("id") or payload.get("conversation_id"),
        inbox_id=inbox.get("id") or conversation.get("inbox_id"),
        inbox_name=inbox.get("name"),
        message_id=payload.get("id"),
        from_email=str(sender.get("email") or "").strip(),
        from_name=str(sender.get("name") or "").strip(),
        sender_type=_sender_type(conversation, sender),
        subject=subject,
        subject_source=subject_source,
        body=str(payload.get("content") or "").strip(),
        message_type=str(payload.get("message_type") or "").strip().lower(),
        content_type=str(payload.get("content_type") or "").strip().lower(),
        is_private=bool(payload.get("private")),
        channel=str(conversation.get("channel") or "").strip(),
        raw=payload,
    )
    parsed.should_process, parsed.skip_reason = _transport_gate(parsed)
    return parsed


def _transport_gate(message: ParsedMessage) -> tuple[bool, str]:
    if message.event != "message_created":
        return False, "not_message_created"
    if message.message_type != "incoming":
        return False, "not_incoming"
    if message.is_private:
        return False, "private_note"
    if message.sender_type and message.sender_type != "contact":
        return False, "not_contact"
    if not message.body:
        return False, "empty_content"
    if not message.from_email:
        return False, "missing_sender_email"
    if not message.conversation_id:
        return False, "missing_conversation_id"
    return True, ""

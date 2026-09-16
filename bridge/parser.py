"""Defensive normalization of Chatwoot ``message_created`` webhooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ParsedAttachment:
    """The transport fields needed to retrieve one Chatwoot attachment."""

    file_type: str = ""
    name: str = ""
    data_url: str = ""


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
    attachments: list[ParsedAttachment] = field(default_factory=list)
    rfc822_message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    to_emails: list[str] = field(default_factory=list)
    is_private: bool = False
    channel: str = ""
    should_process: bool = False
    skip_reason: str = ""
    raw: dict = field(default_factory=dict, repr=False)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _message_type(value: Any) -> str:
    if isinstance(value, int):
        return {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}.get(
            value, str(value)
        )
    return str(value or "").strip().lower()


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


def normalize_message_id(value: Any) -> str:
    """One canonical spelling for an RFC822 Message-ID.

    Chatwoot, Outlook and IMAP all disagree about the angle brackets and the
    surrounding whitespace, and a thread map keyed on the raw header would miss
    the same message written two ways. Case is folded too: the standard calls
    the local part case-sensitive, but no mail system in this pipeline varies
    it, and folding costs less than a missed rethread.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return text.strip("<>").strip().lower()


_REPLY_PREFIXES = ("re:", "re :", "fw:", "fwd:", "aw:", "antw:", "sv:", "vs:", "rv:")


def normalize_subject(value: Any) -> str:
    """Strip reply/forward prefixes so one thread has one subject key."""
    text = " ".join(str(value or "").split()).lower()
    changed = True
    while changed:
        changed = False
        for prefix in _REPLY_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
        if text.startswith("[") and "]" in text[:40]:
            candidate = text[text.index("]") + 1:].strip()
            if candidate:
                text = candidate
                changed = True
    return text


def message_id_list(value: Any) -> list[str]:
    """Normalize a References/In-Reply-To header given as a list or a string."""
    found: list[str] = []
    values = value if isinstance(value, list) else str(value or "").split()
    for item in values:
        normalized = normalize_message_id(item)
        if normalized and normalized not in found:
            found.append(normalized)
    return found


def _email_addresses(value: Any) -> list[str]:
    """Pull bare addresses out of a Chatwoot address list or header string."""
    found: list[str] = []
    values = value if isinstance(value, list) else str(value or "").split(",")
    for item in values:
        if isinstance(item, dict):
            item = item.get("email") or item.get("address") or ""
        text = str(item or "").strip()
        if "<" in text and ">" in text:
            text = text[text.rfind("<") + 1:text.rfind(">")]
        text = text.strip().strip('"').lower()
        if "@" in text and text not in found:
            found.append(text)
    return found


def _thread_headers(payload: dict) -> tuple[str, str, list[str], list[str]]:
    """Read the email threading headers Chatwoot keeps on the message."""
    email = _dict(_dict(payload.get("content_attributes")).get("email"))
    message_id = normalize_message_id(
        email.get("message_id") or email.get("message-id")
    )
    in_reply_to = message_id_list(
        email.get("in_reply_to") or email.get("in-reply-to")
    )
    references = message_id_list(email.get("references"))
    recipients = _email_addresses(email.get("to"))
    for address in _email_addresses(email.get("cc")):
        if address not in recipients:
            recipients.append(address)
    return (
        message_id,
        in_reply_to[0] if in_reply_to else "",
        references,
        recipients,
    )


def _attachments(payload: dict) -> list[ParsedAttachment]:
    found = []
    raw_attachments = payload.get("attachments")
    if not isinstance(raw_attachments, list):
        return found
    for value in raw_attachments:
        attachment = _dict(value)
        if not attachment:
            continue
        found.append(ParsedAttachment(
            file_type=str(
                attachment.get("file_type")
                or attachment.get("content_type")
                or ""
            ).strip().lower(),
            name=str(
                attachment.get("file_name")
                or attachment.get("name")
                or attachment.get("filename")
                or ""
            ).strip(),
            data_url=str(attachment.get("data_url") or "").strip(),
        ))
    return found


def parse_message_created(payload: dict) -> ParsedMessage:
    payload = _dict(payload)
    conversation = _dict(payload.get("conversation"))
    sender = _dict(payload.get("sender"))
    inbox = _dict(payload.get("inbox"))
    account = _dict(payload.get("account"))
    subject, subject_source = _subject(payload, conversation)
    rfc822_message_id, in_reply_to, references, to_emails = _thread_headers(payload)

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
        message_type=_message_type(payload.get("message_type")),
        content_type=str(payload.get("content_type") or "").strip().lower(),
        attachments=_attachments(payload),
        rfc822_message_id=rfc822_message_id,
        in_reply_to=in_reply_to,
        references=references,
        to_emails=to_emails,
        is_private=bool(payload.get("private")),
        channel=str(conversation.get("channel") or "").strip(),
        raw=payload,
    )
    parsed.should_process, parsed.skip_reason = _transport_gate(parsed)
    return parsed


def draft_label_added(payload: dict, label: str) -> int | None:
    """Return the conversation ID only for an explicit absent-to-present label change."""
    payload = _dict(payload)
    if payload.get("event") != "conversation_updated":
        return None
    for change in payload.get("changed_attributes") or []:
        values = _dict(_dict(change).get("label_list"))
        previous = values.get("previous_value")
        current = values.get("current_value")
        if not isinstance(previous, list) or not isinstance(current, list):
            continue
        if label not in previous and label in current:
            try:
                return int(payload.get("id"))
            except (TypeError, ValueError):
                return None
    return None


def newest_customer_message(
    messages: list[dict],
    *,
    conversation_id: int,
    account_id: int = 0,
    meta: dict | None = None,
    through_message_id: int | None = None,
) -> ParsedMessage | None:
    """Normalize the newest non-private incoming contact message from an API result."""
    meta = _dict(meta)
    contact = _dict(meta.get("contact"))
    if isinstance(contact.get("payload"), list) and contact["payload"]:
        contact = _dict(contact["payload"][0])
    conversation_attributes = _dict(meta.get("additional_attributes"))

    candidates = []
    for raw in messages if isinstance(messages, list) else []:
        value = _dict(raw)
        sender = _dict(value.get("sender"))
        sender_type = str(sender.get("type") or value.get("sender_type") or "").lower()
        if _message_type(value.get("message_type")) != "incoming":
            continue
        if bool(value.get("private")) or (sender_type and sender_type != "contact"):
            continue
        try:
            message_id = int(value.get("id"))
        except (TypeError, ValueError):
            continue
        if through_message_id is not None and message_id > through_message_id:
            continue
        candidates.append((message_id, value, sender))
    if not candidates:
        return None

    message_id, value, sender = max(candidates, key=lambda item: item[0])
    subject, subject_source = _subject(
        value,
        {"additional_attributes": conversation_attributes},
    )
    rfc822_message_id, in_reply_to, references, to_emails = _thread_headers(value)
    parsed = ParsedMessage(
        event="message_created",
        account_id=int(value.get("account_id") or account_id or 0),
        conversation_id=conversation_id,
        inbox_id=value.get("inbox_id"),
        message_id=message_id,
        from_email=str(sender.get("email") or contact.get("email") or "").strip(),
        from_name=str(sender.get("name") or contact.get("name") or "").strip(),
        sender_type=str(sender.get("type") or value.get("sender_type") or "contact").lower(),
        subject=subject,
        subject_source=subject_source,
        body=str(value.get("content") or value.get("processed_message_content") or "").strip(),
        message_type="incoming",
        content_type=str(value.get("content_type") or "").strip().lower(),
        attachments=_attachments(value),
        rfc822_message_id=rfc822_message_id,
        in_reply_to=in_reply_to,
        references=references,
        to_emails=to_emails,
        is_private=bool(value.get("private")),
        raw=value,
    )
    parsed.should_process, parsed.skip_reason = _transport_gate(parsed)
    return parsed if parsed.should_process else None


def _transport_gate(message: ParsedMessage) -> tuple[bool, str]:
    if message.event != "message_created":
        return False, "not_message_created"
    if message.message_type != "incoming":
        return False, "not_incoming"
    if message.is_private:
        return False, "private_note"
    if message.sender_type and message.sender_type != "contact":
        return False, "not_contact"
    if not message.body and not message.attachments:
        return False, "empty_content"
    if not message.from_email:
        return False, "missing_sender_email"
    if not message.conversation_id:
        return False, "missing_conversation_id"
    if message.message_id is None:
        return False, "missing_message_id"
    return True, ""

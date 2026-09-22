"""Find or open the Chatwoot conversation an internal sender should reply in.

Nobody on the team should have to look up a conversation id. A trusted caller
(first: the DewieOps refund notice) names the customer's email and a subject; the
bridge returns that contact's conversation WITH THAT SUBJECT in the configured
email inbox, or opens a new, empty one with it. A notice therefore gets its own
thread ("Refund Update: Order #N") instead of landing in whatever unrelated thread
the customer last wrote (Lana, 2026-09-22); running the same caller twice reuses
the thread rather than opening a duplicate.

Creating a conversation posts no message, so nothing is emailed here. The
customer-visible message still goes only through the idempotent outbound
transport (outbound.py). Every returned conversation is re-read and must belong
to exactly that email on exactly that inbox, so a caller can never be pointed at
someone else's thread.

Same bearer as the outbound transport. Disabled (503) until
``BRIDGE_OUTBOUND_INBOX_ID`` names the email inbox to use.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from chatwoot import ChatwootError

INBOX_ENV = "BRIDGE_OUTBOUND_INBOX_ID"

log = logging.getLogger("dewie-desk-bridge.conversations")

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    subject: Label
    name: Label | None = None
    actor: Label
    source: Label


def configured_inbox() -> int | None:
    raw = (os.environ.get(INBOX_ENV) or "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else None


def parse_request(payload: object) -> ResolveRequest:
    try:
        return ResolveRequest.model_validate(payload)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) or "body" for error in exc.errors()})
        raise ValueError("invalid_request: " + ", ".join(fields)) from None


def _activity(conversation: dict) -> tuple:
    return (
        conversation.get("last_activity_at") or conversation.get("timestamp") or 0,
        conversation.get("created_at") or 0,
        conversation.get("id") or 0,
    )


def _subject(conversation: dict) -> str:
    return " ".join(str((conversation.get("additional_attributes") or {}).get("mail_subject") or "").split())


def _belongs(details: dict, email: str, inbox_id: int) -> bool:
    sender = ((details.get("meta") or {}).get("sender") or {})
    return (details.get("inbox_id") == inbox_id
            and str(sender.get("email") or "").strip().casefold() == email.strip().casefold())


def resolve(client, request: ResolveRequest, inbox_id: int) -> tuple[int, dict]:
    email = request.email.strip()
    base = {"inbox_id": inbox_id}
    try:
        contacts = client.find_contacts_by_email(email)
        if len(contacts) > 1:
            return 409, {"status": "ambiguous_contact", **base}

        status = "found"
        conversation_id = None
        if contacts:
            contact_id = contacts[0]["id"]
            subject = " ".join(request.subject.split())
            mine = [c for c in client.contact_conversations(contact_id)
                    if c.get("inbox_id") == inbox_id and isinstance(c.get("id"), int)
                    and _subject(c) == subject]
            if mine:
                conversation_id = max(mine, key=_activity)["id"]
        else:
            contact_id = client.create_contact(inbox_id, email, request.name)["id"]

        if conversation_id is None:
            status = "created"
            source_id = client.contact_source_id(contact_id, inbox_id, email)
            conversation_id = client.create_conversation(
                contact_id, inbox_id, source_id, request.subject)

        if not _belongs(client.conversation_details(conversation_id), email, inbox_id):
            return 409, {"status": "mismatch", "conversation_id": conversation_id, **base}
    except ChatwootError as exc:
        return 502, {"status": "error", "detail": str(exc)[:80], **base}
    except (KeyError, TypeError, ValueError):
        return 502, {"status": "error", "detail": "unexpected_chatwoot_shape", **base}

    return 200, {"status": status, "conversation_id": conversation_id,
                 "contact_id": contact_id, "contact_email": email, **base}

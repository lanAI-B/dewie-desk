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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from chatwoot import ChatwootError

INBOX_ENV = "BRIDGE_OUTBOUND_INBOX_ID"
# Per-store inboxes: "abs:1,actex:3". A refund for an ABS order must go out from
# the ABS inbox — the reply-to is the address the customer will answer, and it
# has to be the one that store's mail is worked from. The single-inbox variable
# above remains for a one-store deployment and as the fallback for both.
INBOXES_ENV = "BRIDGE_OUTBOUND_INBOX_IDS"
STORES = ("abs", "actex")

log = logging.getLogger("dewie-desk-bridge.conversations")

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    subject: Label
    name: Label | None = None
    actor: Label
    source: Label
    # Which store's inbox this conversation belongs on. Required: guessing the
    # sending brand is how a customer gets a refund notice from the wrong shop.
    store: Literal["abs", "actex"]


def _inbox_id(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else None


def configured_inbox() -> int | None:
    """The single-inbox setting, used when no per-store map is configured."""
    return _inbox_id(os.environ.get(INBOX_ENV) or "")


def configured_inboxes() -> dict[str, int]:
    """store -> inbox id. Falls back to the single inbox for every store.

    Parsed strictly: one malformed pair yields an empty map and the endpoint
    refuses, rather than silently routing half the stores. Same reasoning as the
    old notifier allowlist — a config typo must not become a guess about where a
    customer's mail comes from.
    """
    raw = (os.environ.get(INBOXES_ENV) or "").strip()
    if not raw:
        single = configured_inbox()
        return {store: single for store in STORES} if single else {}
    mapping: dict[str, int] = {}
    for pair in raw.replace(";", ",").split(","):
        store, _, value = pair.partition(":")
        inbox = _inbox_id(value)
        if store.strip() not in STORES or inbox is None:
            return {}
        mapping[store.strip()] = inbox
    return mapping


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


EMAIL_CHANNEL = "Channel::Email"


def recipient_of(details: dict, inbox_id: int) -> str | None:
    """The address Chatwoot will email for this conversation, or None if it will not.

    None unless the conversation is on the configured inbox AND that inbox is an
    email channel: a widget/API inbox would accept the message and email nobody
    (review #5).
    """
    meta = details.get("meta") or {}
    if details.get("inbox_id") != inbox_id or meta.get("channel") != EMAIL_CHANNEL:
        return None
    email = str((meta.get("sender") or {}).get("email") or "").strip()
    return email or None


def recipient_on_any(details: dict, inbox_ids) -> str | None:
    """As `recipient_of`, but for a send, where any configured inbox is legitimate.

    Sending does not need to know the store. The property that keeps a customer
    from getting someone else's refund is "this conversation is on an inbox we
    send from, it is an email channel, and it belongs to the stated recipient" —
    store routing only decides which inbox a NEW conversation is opened on.
    Requiring the store here too would add a second way to fail and no safety.
    """
    for inbox_id in inbox_ids:
        found = recipient_of(details, inbox_id)
        if found is not None:
            return found
    return None


def _belongs(details: dict, email: str, inbox_id: int) -> bool:
    found = recipient_of(details, inbox_id)
    return found is not None and found.casefold() == email.strip().casefold()


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

        details = client.conversation_details(conversation_id)
        if not _belongs(details, email, inbox_id):
            return 409, {"status": "mismatch", "conversation_id": conversation_id, **base}
        # Report the address Chatwoot holds, not the caller's input (review #7).
        confirmed = recipient_of(details, inbox_id)
    except ChatwootError as exc:
        return 502, {"status": "error", "detail": str(exc)[:80], **base}
    except (KeyError, TypeError, ValueError):
        return 502, {"status": "error", "detail": "unexpected_chatwoot_shape", **base}

    return 200, {"status": status, "conversation_id": conversation_id,
                 "contact_id": contact_id, "contact_email": confirmed, **base}

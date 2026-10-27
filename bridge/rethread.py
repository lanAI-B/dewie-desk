"""Put a detached reply back on its thread.

Chatwoot joins an email to a conversation only when its In-Reply-To matches a
message that same inbox holds. So a customer answering mail we sent from
Outlook or webmail, or answering across our two inboxes, opens a new
conversation; one 10-27 customer's thread was split six ways.

Chatwoot has no merge API, so a thread is folded, not merged: the new
conversation is where the customer now is and where we reply from. It gets a
note naming the conversation it continues; the older one, if still open, gets
a note pointing forward and is resolved. The link is recorded, and the drafter
reads the whole chain as one thread (thread.fetch_chain).

How a parent is found, strongest first:
1. In-Reply-To / References name a message the bridge has mapped: an inbound
   email in either inbox, a reply sent from the desk, or one sent from
   Outlook/webmail that sent sync filed.
2. Fallback: a Re:/Fwd: subject from the same sender as an earlier
   conversation with that subject.

Only the first inbound message of a conversation is ever considered, and only
an older conversation can be a parent, so a chain always runs one way.

BRIDGE_RETHREAD: "off", "dry_run" (default: decide and log, change nothing),
or "on".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from parser import normalize_subject
from state import message_id_key, participant_subject_key

log = logging.getLogger("dewie-desk-bridge.rethread")

FOLD_NOTE_PREFIX = "**Thread continues**"
FORWARD_NOTE_PREFIX = "**Thread continued in**"
_REPLY_SUBJECT = ("re:", "fw:", "fwd:", "aw:")
INTERNAL_DOMAINS = {"actuarialbookstore.com", "actexlearning.com", "actexmadriver.com"}


def mode() -> str:
    value = os.environ.get("BRIDGE_RETHREAD", "dry_run").strip().lower()
    return value if value in {"off", "dry_run", "on"} else "dry_run"


@dataclass(frozen=True)
class Parent:
    conversation_id: int
    method: str  # "message_id" | "subject"


def find_parent(store, message) -> Parent | None:
    """The older conversation this first inbound message continues, if any.

    Call BEFORE the message's own thread keys are recorded: afterwards its
    subject key points at itself.
    """
    conversation_id = int(message.conversation_id or 0)
    if not conversation_id or store.latest_inbound_id(conversation_id) is not None:
        return None  # not the conversation's first email: Chatwoot already threaded it

    keys = [message_id_key(getattr(message, "in_reply_to", ""))]
    keys += [message_id_key(r) for r in reversed(getattr(message, "references", None) or [])]
    for key in keys:
        found = store.resolve_thread([key]) if key else None
        if found and found < conversation_id:
            return Parent(found, "message_id")

    subject = str(getattr(message, "subject", "") or "").strip().lower()
    if subject.startswith(_REPLY_SUBJECT) and normalize_subject(subject):
        key = participant_subject_key(subject, getattr(message, "from_email", ""))
        found = store.resolve_thread([key]) if key else None
        if found and found < conversation_id:
            return Parent(found, "subject")
    return None


def is_internal(email: str) -> bool:
    return str(email or "").strip().lower().rsplit("@", 1)[-1] in INTERNAL_DOMAINS


def fold(client, store, child: int, parent: Parent, from_email: str = "") -> str:
    """Link child to parent in Chatwoot and in the store; return the outcome."""
    if not store.link_conversations(child, parent.conversation_id, parent.method):
        return "already_linked"
    how = "the email it replies to" if parent.method == "message_id" else "subject and sender"
    if is_internal(from_email):
        # Our own mail landing in the other inbox (10-27 replay: service@ABS replies
        # to chat-box questions). Folding it would make our address the live
        # "customer" and resolve the real one, so link both ways and resolve nothing.
        client.post_private_note(
            child,
            f"{FOLD_NOTE_PREFIX} from #{parent.conversation_id} (matched by {how}). "
            f"This is our own mail ({from_email}) on that thread; the customer is there.",
        )
        client.post_private_note(
            parent.conversation_id,
            f"{FORWARD_NOTE_PREFIX} #{child}: our own mail from {from_email} on this thread "
            f"landed there as a new conversation.",
        )
        return "linked_internal"
    client.post_private_note(
        child,
        f"{FOLD_NOTE_PREFIX} from #{parent.conversation_id} (matched by {how}). "
        f"The earlier messages are there; Dewie reads both as one thread. Reply from here.",
    )
    try:
        details = client.conversation_details(parent.conversation_id)
    except Exception:
        details = {}
    if str((details or {}).get("status") or "").lower() in {"open", "pending", "snoozed"}:
        client.post_private_note(
            parent.conversation_id,
            f"{FORWARD_NOTE_PREFIX} #{child}: the customer's newest reply opened a new "
            f"conversation. Resolved here so the thread is worked in one place.",
        )
        client.resolve_conversation(parent.conversation_id)
        return "folded_parent_resolved"
    return "folded"


def handle(client, store, message, parent: Parent | None, increment=lambda name: None) -> str:
    """Background step after an inbound is recorded; never raises."""
    if parent is None:
        return "no_parent"
    current = mode()
    increment(f"rethread_{parent.method}")
    log.info("rethread %s conversation=%s parent=%s method=%s", current,
             message.conversation_id, parent.conversation_id, parent.method)
    if current != "on":
        increment(f"rethread_{current}")
        return current
    try:
        outcome = fold(client, store, int(message.conversation_id), parent,
                       getattr(message, "from_email", ""))
    except Exception:
        log.exception("rethread failed conversation=%s parent=%s",
                      message.conversation_id, parent.conversation_id)
        outcome = "failed"
    increment(f"rethread_{outcome}")
    return outcome

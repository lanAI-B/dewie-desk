"""The conversation as the drafter reads it: the whole thread, plus the team's notes.

A draft written from the newest message alone asks again for what the customer
already gave and repeats answers we already sent. So the drafter gets every
message, oldest first, labelled by who wrote it and who could see it.

A human note that says @dewie is the team steering the draft ("@dewie tell her
we'll make an exception and refund"). Those written after the customer's newest
message go to the drafter separately as TEAM NOTES, which outrank its own
judgment. Untagged notes are the team talking to each other: context in the
thread, never orders. Either kind is part of the draft action key, so adding a
note and re-applying the label gets a fresh draft.

Dewie's own notes (drafts, doubts, failures, captures) are left out: a model
that reads its last draft anchors on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sent_sync import strip_quoted_history

# The drafter caps the thread at 120k chars; trim below that here, oldest-middle
# first, so what is dropped is chosen rather than cut mid-message.
THREAD_BUDGET = 100_000
MAX_PAGES = 25  # 20 messages a page; a 500-message thread is far past any real one
DEWIE_NOTE_PREFIXES = ("**Dewie draft**", "**Dewie's doubts**", "**Dewie could not draft",
                       "**Dewie order capture**")
OUTLOOK_NOTE_PREFIX = "[sent by"


@dataclass(frozen=True)
class DraftThread:
    thread: str = ""
    team_notes: str = ""
    newest_note_id: int | None = None


def fetch_all_messages(client, conversation_id: int, first_page: tuple | list) -> list[dict]:
    """Page back from the newest page until Chatwoot runs out; best effort."""
    messages = {m.get("id"): m for m in first_page if isinstance(m, dict) and m.get("id")}
    for _ in range(MAX_PAGES):
        if not messages:
            break
        oldest = min(int(i) for i in messages)
        page = client.get_conversation(conversation_id, before=oldest)
        if not page.ok or not page.messages:
            break
        added = 0
        for m in page.messages:
            if isinstance(m, dict) and m.get("id") and m["id"] not in messages:
                messages[m["id"]] = m
                added += 1
        if not added:
            break
    return sorted(messages.values(), key=lambda m: int(m["id"]))


def _kind(m: dict) -> str | None:
    mt = m.get("message_type")
    mt = {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}.get(mt, str(mt or "").lower())
    content = str(m.get("content") or "").strip()
    if mt == "incoming" and not m.get("private"):
        return "customer"
    if mt != "outgoing":
        return None
    if not m.get("private"):
        return "sent"
    if content.startswith(DEWIE_NOTE_PREFIXES):
        return None
    if content.startswith(OUTLOOK_NOTE_PREFIX):
        return "sent_outlook"
    return "note"


# Gmail wraps a long attribution: "On Wed, Oct 14, 2026 at 12:50 PM ACTEX <" / "x@y> wrote:".
_WRAPPED_ATTRIBUTION = re.compile(r"^\s*on \S.{5,200}\n.{0,200}\bwrote:\s*$",
                                  re.IGNORECASE | re.MULTILINE)


def customer_text(content: str) -> str:
    """What the customer wrote this time, without the quoted chain below it.

    Every reply carries the whole thread again; in the thread that is the same
    text read ten times over, crowding out what is new.
    """
    text = strip_quoted_history(content)
    match = _WRAPPED_ATTRIBUTION.search(text)
    if match and text[:match.start()].strip():
        text = text[:match.start()].strip()
    return text


# "@dewie" typed as text, or a Chatwoot mention of an agent named Dewie.
_DEWIE_TAG = re.compile(r"\[@dewie\]\(mention://[^)]*\)|@dewie\b", re.IGNORECASE)


def addressed_to_dewie(content: str) -> bool:
    return bool(_DEWIE_TAG.search(content or ""))


def _sender(m: dict) -> str:
    sender = m.get("sender") if isinstance(m.get("sender"), dict) else {}
    return str(sender.get("name") or sender.get("email") or "").strip()


def _render(m: dict, kind: str) -> str:
    who = _sender(m)
    label = {
        "customer": f"Customer{f' ({who})' if who else ''}",
        "sent": f"Us, sent to the customer{f' by {who}' if who else ''}",
        "sent_outlook": "Us, sent to the customer from Outlook",
        "note": f"Internal note{f' from {who}' if who else ''} (team only; the customer never saw it)",
    }[kind]
    content = str(m.get("content") or "").strip()
    if kind == "customer":
        content = customer_text(content)
    if not content and m.get("attachments"):
        content = f"[{len(m['attachments'])} attachment(s)]"
    return f"[#{m.get('id')}] {label}:\n{content}"


def _fit(blocks: list[str], budget: int) -> str:
    """Keep the first message and as many of the newest as fit."""
    text = "\n\n".join(blocks)
    if len(text) <= budget or len(blocks) < 3:
        return text[-budget:]
    first, rest = blocks[0], blocks[1:]
    kept: list[str] = []
    room = budget - len(first) - 80
    for block in reversed(rest):
        if len(block) + 2 > room:
            break
        kept.insert(0, block)
        room -= len(block) + 2
    omitted = len(rest) - len(kept)
    gap = [f"[{omitted} earlier message(s) omitted for length]"] if omitted else []
    return "\n\n".join([first, *gap, *kept])


def build_draft_thread(messages: list[dict], customer_message_id: int,
                       through_message_id: int | None = None,
                       budget: int = THREAD_BUDGET) -> DraftThread:
    """Thread up to the customer's newest message; human notes after it are steering."""
    blocks, notes, note_ids = [], [], []
    for m in messages:
        try:
            mid = int(m.get("id"))
        except (TypeError, ValueError):
            continue
        if through_message_id is not None and mid > through_message_id:
            continue
        kind = _kind(m)
        if kind is None:
            continue
        content = str(m.get("content") or "").strip()
        steering = kind == "note" and mid > customer_message_id
        if steering:
            note_ids.append(mid)
        if steering and addressed_to_dewie(content):
            text = _DEWIE_TAG.sub("", content).strip(" ,:-")
            notes.append(f"{_sender(m) or 'Team'}: {text}")
        else:
            blocks.append(_render(m, kind))
    # A thread of just the newest message says nothing the body doesn't.
    thread = _fit(blocks, budget) if len(blocks) > 1 else ""
    return DraftThread(thread, "\n\n".join(notes), max(note_ids) if note_ids else None)

"""Replay externally authored Sent mail into Chatwoot as private notes.

Chatwoot's email channel fetches INBOX only. While CS keeps replying from
Outlook - which is the plan, not a transitional accident - every conversation in
Chatwoot is missing one whole side of itself, and a draft written from that view
contradicts what a person already said.

This module reads the Sent folder read-only, resolves each reply to the
conversation it belongs to using the thread map the bridge built from inbound
webhooks, and posts it as a PRIVATE note.

Two invariants, both safety-critical:

* Only ``post_private_note`` is ever called. A public outgoing message on a
  Chatwoot email inbox makes Chatwoot actually *send* it, so replaying history
  through the public path would re-email every customer.
* The mailbox is opened read-only and fetched with ``mark_seen=False``. This
  runs against a mailbox humans are working in.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import parsedate_to_datetime

from parser import message_id_list, normalize_message_id
from state import message_id_key, participant_subject_key

log = logging.getLogger("dewie-desk-bridge.sent-sync")

NOTE_BODY_LIMIT = 4000
_QUOTE_MARKERS = (
    re.compile(r"^\s*-+\s*original message\s*-+\s*$", re.IGNORECASE),
    re.compile(r"^\s*on .{5,200}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*from:\s.+@.+$", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),
)


@dataclass(frozen=True)
class SentReply:
    """One outgoing mail as it exists in the Sent folder."""

    message_id: str = ""
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    subject: str = ""
    from_email: str = ""
    from_name: str = ""
    to_emails: tuple[str, ...] = ()
    sent_at: datetime | None = None
    body: str = ""
    uid: str = ""


@dataclass
class SyncReport:
    """What one pass over the Sent folder did, and what it could not place."""

    counts: Counter = field(default_factory=Counter)
    unresolved: list[str] = field(default_factory=list)

    def bump(self, name: str, amount: int = 1) -> None:
        self.counts[name] += amount

    def as_dict(self) -> dict:
        return {"counts": dict(self.counts), "unresolved": list(self.unresolved)}


def _address(value) -> str:
    text = str(value or "").strip()
    if "<" in text and ">" in text:
        text = text[text.rfind("<") + 1:text.rfind(">")]
    return text.strip().strip('"').lower()


def _display_name(value) -> str:
    text = str(value or "").strip()
    if "<" in text:
        text = text[: text.index("<")]
    return text.strip().strip('"')


def _addresses(value) -> tuple[str, ...]:
    found: list[str] = []
    for item in str(value or "").split(","):
        address = _address(item)
        if "@" in address and address not in found:
            found.append(address)
    return tuple(found)


def strip_quoted_history(body: str) -> str:
    """Drop the quoted thread below a reply, keeping what the agent wrote.

    Conservative on purpose: if trimming would leave nothing, the full text is
    kept. A note with too much history is noise; a note missing the reply is the
    bug this module exists to fix.
    """
    lines = str(body or "").splitlines()
    for index, line in enumerate(lines):
        if any(marker.match(line) for marker in _QUOTE_MARKERS):
            trimmed = "\n".join(lines[:index]).strip()
            return trimmed if trimmed else str(body or "").strip()
    return str(body or "").strip()


def _body_text(message) -> str:
    """Prefer the plain-text part; fall back to a crude HTML strip."""
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        if disposition.startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
        if part.get_content_subtype() == "html":
            html.append(text)
        else:
            plain.append(text)
    if plain:
        return "\n".join(plain)
    stripped = re.sub(r"<[^>]+>", " ", "\n".join(html))
    return re.sub(r"[ \t]{2,}", " ", stripped)


def parse_sent_email(raw: bytes, uid: str = "") -> SentReply:
    """Normalize one RFC822 message from the Sent folder."""
    message = message_from_bytes(raw)
    try:
        sent_at = parsedate_to_datetime(message.get("Date"))
    except (TypeError, ValueError):
        sent_at = None
    references = tuple(message_id_list(message.get("References")))
    in_reply_to = message_id_list(message.get("In-Reply-To"))
    return SentReply(
        message_id=normalize_message_id(message.get("Message-ID")),
        in_reply_to=in_reply_to[0] if in_reply_to else "",
        references=references,
        subject=str(message.get("Subject") or "").strip(),
        from_email=_address(message.get("From")),
        from_name=_display_name(message.get("From")),
        to_emails=_addresses(message.get("To")) + _addresses(message.get("Cc")),
        sent_at=sent_at,
        body=_body_text(message),
        uid=str(uid or ""),
    )


def from_mail_message(message) -> SentReply:
    """Normalize an ``imap_tools`` message, preferring its raw RFC822 source."""
    raw = getattr(message, "obj", None)
    if raw is not None and hasattr(raw, "as_bytes"):
        return parse_sent_email(raw.as_bytes(), uid=getattr(message, "uid", "") or "")
    headers = getattr(message, "headers", None) or {}
    return SentReply(
        message_id=normalize_message_id((headers.get("message-id") or ("",))[0]),
        in_reply_to=normalize_message_id((headers.get("in-reply-to") or ("",))[0]),
        references=tuple(message_id_list(" ".join(headers.get("references") or ()))),
        subject=str(getattr(message, "subject", "") or ""),
        from_email=_address(getattr(message, "from_", "")),
        to_emails=tuple(_address(value) for value in getattr(message, "to", ()) or ()),
        sent_at=getattr(message, "date", None),
        body=str(getattr(message, "text", "") or ""),
        uid=str(getattr(message, "uid", "") or ""),
    )


def resolution_keys(reply: SentReply) -> list[tuple[str, str]]:
    """Ordered (key, method) candidates for placing one sent reply.

    In-Reply-To is the only identifier the sender actually asserts about the
    thread, so it leads. References follow, newest first. Subject plus recipient
    is a guess, and is counted separately for exactly that reason.
    """
    candidates: list[tuple[str, str]] = []

    def add(key: str, method: str) -> None:
        if key and all(key != existing for existing, _ in candidates):
            candidates.append((key, method))

    add(message_id_key(reply.in_reply_to), "in_reply_to")
    for reference in reversed(reply.references):
        add(message_id_key(reference), "references")
    for recipient in reply.to_emails:
        add(participant_subject_key(reply.subject, recipient), "subject_recipient")
    return candidates


def resolve_conversation(store, reply: SentReply) -> tuple[int | None, str]:
    """Return the conversation this reply belongs to, and how it was found."""
    for key, method in resolution_keys(reply):
        conversation_id = store.resolve_thread([key])
        if conversation_id:
            return conversation_id, method
    return None, "unresolved"


def note_claim_key(reply: SentReply) -> str:
    """One durable claim per sent message, so a reply is never posted twice."""
    if reply.message_id:
        return f"sent-note:msgid:{reply.message_id}"
    if reply.uid:
        return f"sent-note:uid:{reply.uid}"
    return ""


def format_note(reply: SentReply) -> str:
    """Render the private note, attributed to the human who sent it."""
    agent = reply.from_name or reply.from_email or "unknown sender"
    parts = ["sent by", agent, "via Outlook"]
    if reply.sent_at is not None:
        parts.append(reply.sent_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    body = strip_quoted_history(reply.body)
    if len(body) > NOTE_BODY_LIMIT:
        body = body[:NOTE_BODY_LIMIT].rstrip() + "\n\n[truncated]"
    return f"[{' '.join(parts)}]\n\n{body}".strip()


def _enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def sent_sync_status() -> dict:
    """Configuration evidence for /health. No secret value is reported."""
    return {
        "enabled": _enabled("SENT_SYNC_ENABLED", False),
        "folder": os.environ.get("SENT_SYNC_FOLDER", "Sent"),
        "host": os.environ.get("SENT_SYNC_IMAP_HOST", "secure.emailsrvr.com"),
        "credentials_configured": bool(
            (os.environ.get("SENT_SYNC_USERNAME") or "").strip()
            and (os.environ.get("SENT_SYNC_PASSWORD") or "")
        ),
    }


def sync_replies(
    replies,
    *,
    store,
    client,
    dry_run: bool = True,
    report: SyncReport | None = None,
) -> SyncReport:
    """Place and post already-fetched sent replies. Transport-free, so testable."""
    report = report or SyncReport()
    for reply in replies:
        report.bump("sent_seen")
        claim_key = note_claim_key(reply)
        if not claim_key:
            report.bump("unidentifiable")
            report.unresolved.append(f"{reply.subject[:60]} (no Message-ID or UID)")
            continue
        conversation_id, method = resolve_conversation(store, reply)
        if conversation_id is None:
            report.bump("unresolved")
            report.unresolved.append(
                f"{reply.subject[:60]} -> {', '.join(reply.to_emails)}"
            )
            continue
        report.bump(f"resolved_{method}")
        if not store.claim(claim_key):
            report.bump("duplicate_sent")
            continue
        if dry_run:
            report.bump("notes_avoided_dry_run")
            store.release(claim_key)
            continue
        posted = client.post_private_note(int(conversation_id), format_note(reply))
        if posted.ok:
            report.bump("private_notes_posted")
        else:
            report.bump("note_post_failed")
            store.release(claim_key)
            log.error(
                "sent note failed conversation=%s status=%s detail=%s",
                conversation_id,
                posted.status_code,
                posted.detail,
            )
    return report


def fetch_sent_replies(
    *,
    host: str,
    username: str,
    password: str,
    folder: str,
    lookback_days: int,
    limit: int = 200,
):
    """Read the Sent folder read-only. Nothing here writes to the mailbox."""
    from imap_tools import AND, MailBox

    since = date.today() - timedelta(days=max(lookback_days, 0))
    replies = []
    with MailBox(host).login(username, password, folder) as mailbox:
        for message in mailbox.fetch(AND(date_gte=since), mark_seen=False, limit=limit):
            replies.append(from_mail_message(message))
    return replies


def run_sent_sync(*, store, client, dry_run: bool | None = None) -> SyncReport:
    """One configured pass. Missing credentials is a reported state, not a crash."""
    report = SyncReport()
    if not _enabled("SENT_SYNC_ENABLED", False):
        report.bump("disabled")
        return report
    host = os.environ.get("SENT_SYNC_IMAP_HOST", "secure.emailsrvr.com").strip()
    username = (os.environ.get("SENT_SYNC_USERNAME") or "").strip()
    password = os.environ.get("SENT_SYNC_PASSWORD") or ""
    folder = os.environ.get("SENT_SYNC_FOLDER", "Sent").strip() or "Sent"
    try:
        lookback = int(os.environ.get("SENT_SYNC_LOOKBACK_DAYS", "14") or 14)
    except ValueError:
        lookback = 14
    if not username or not password:
        report.bump("missing_credentials")
        log.warning("sent sync has no mailbox credentials; nothing fetched")
        return report
    try:
        replies = fetch_sent_replies(
            host=host,
            username=username,
            password=password,
            folder=folder,
            lookback_days=lookback,
        )
    except Exception as exc:
        report.bump("fetch_failed")
        log.error("sent folder fetch failed host=%s error=%s", host, type(exc).__name__)
        return report
    effective_dry_run = _enabled("BRIDGE_DRY_RUN", True) if dry_run is None else dry_run
    return sync_replies(
        replies, store=store, client=client, dry_run=effective_dry_run, report=report
    )


def main(argv=None) -> int:
    """Attended entry point: one pass, printed as a report.

    Deliberately a command rather than an HTTP route. The bridge has no operator
    authentication - only Chatwoot's webhook HMAC - so an endpoint that reads a
    mailbox and writes notes would be a new unauthenticated surface.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Sync the Sent folder into Chatwoot.")
    parser.add_argument(
        "--post",
        action="store_true",
        help="post private notes; without it the pass is a dry run",
    )
    arguments = parser.parse_args(argv)

    from main import chatwoot_client, dedup_store

    report = run_sent_sync(
        store=dedup_store(),
        client=chatwoot_client(),
        dry_run=not arguments.post,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - attended entry point
    raise SystemExit(main())

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
  runs against a mailbox humans are working in. Nothing is moved, flagged or
  deleted.
* A message carrying ``X-Dewie-Desk-Copy`` is skipped. The bridge appends that
  header to its Sent-folder copy of a reply that was sent FROM Chatwoot
  (feature/desk-sent-copy); posting it back would duplicate a message
  Chatwoot already shows, as a note, on every such reply.

Mailboxes and credentials are the ones ``sent_copy`` already uses
(``SENT_COPY_INBOXES`` and ``SENT_COPY_<LABEL>_*``); each is read on its own, so
one misconfigured or unreachable mailbox never stops the others.

The bridge runs a pass every ``SENT_SYNC_INTERVAL_SECONDS`` when
``SENT_SYNC_ENABLED`` is true (see ``SentSyncRunner``). ``python sent_sync.py``
is the attended equivalent and is a dry run unless ``--post`` is given.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

import sent_copy
from parser import message_id_list, normalize_message_id
from state import message_id_key, participant_subject_key

log = logging.getLogger("dewie-desk-bridge.sent-sync")

NOTE_BODY_LIMIT = 4000
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_LOOKBACK_DAYS = 3
IMAP_TIMEOUT_SECONDS = 60
# Set by the bridge on copies of Chatwoot-sent replies it files in Sent. The
# value is the Chatwoot message id; its presence alone means "already in Chatwoot".
DESK_COPY_HEADER = "X-Dewie-Desk-Copy"
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
    desk_copy: str = ""  # X-Dewie-Desk-Copy value: this mail came FROM Chatwoot


@dataclass
class SyncReport:
    """What one pass over the Sent folder did, and what it could not place."""

    counts: Counter = field(default_factory=Counter)
    unresolved: list[str] = field(default_factory=list)
    problem: str = ""  # why this mailbox was not (fully) read; never a secret

    def bump(self, name: str, amount: int = 1) -> None:
        self.counts[name] += amount

    def as_dict(self) -> dict:
        value = {"counts": dict(self.counts), "unresolved": list(self.unresolved)}
        if self.problem:
            value["problem"] = self.problem
        return value


@dataclass
class PassReport:
    """One pass over every configured mailbox, reported per mailbox."""

    mailboxes: dict[str, SyncReport] = field(default_factory=dict)
    problem: str = ""  # the mailbox list itself could not be read

    def totals(self) -> Counter:
        total: Counter = Counter()
        for report in self.mailboxes.values():
            total.update(report.counts)
        return total

    def as_dict(self) -> dict:
        value = {
            "mailboxes": {label: report.as_dict() for label, report in self.mailboxes.items()},
            "totals": dict(self.totals()),
        }
        if self.problem:
            value["problem"] = self.problem
        return value


def decode_header_text(value) -> str:
    """Decode RFC 2047 encoded words (``=?utf-8?Q?...?=``) into plain text."""
    if value is None:
        return ""
    text = str(value)
    try:
        text = str(make_header(decode_header(text)))
    except Exception:  # a malformed encoded word stays as it was sent
        pass
    return " ".join(text.split())


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


_HTML_INVISIBLE = re.compile(
    r"<!--.*?-->|<(head|style|script)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_HTML_BREAK = re.compile(r"<br\s*/?>|</p\s*>|</div\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


def html_to_text(markup: str) -> str:
    """Readable text from an HTML-only body: line breaks kept, entities decoded."""
    text = _HTML_INVISIBLE.sub("", str(markup or ""))
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_TAG.sub(" ", text)
    # Unescape only after the tags are gone, so an escaped "&lt;b&gt;" stays text.
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _body_text(message) -> str:
    """Prefer the plain-text part; fall back to the HTML part as text."""
    plain: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        # Only text parts are a body. An inline image (a signature logo, a pasted
        # screenshot) is not an attachment by disposition and must not be read as one.
        if part.get_content_maintype() != "text":
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
            html_parts.append(text)
        else:
            plain.append(text)
    if plain:
        return "\n".join(plain)
    return html_to_text("\n".join(html_parts))


def parse_sent_email(raw: bytes, uid: str = "") -> SentReply:
    """Normalize one RFC822 message from the Sent folder."""
    message = message_from_bytes(raw)
    try:
        sent_at = parsedate_to_datetime(message.get("Date"))
    except (TypeError, ValueError):
        sent_at = None
    references = tuple(message_id_list(message.get("References")))
    in_reply_to = message_id_list(message.get("In-Reply-To"))
    sender = decode_header_text(message.get("From"))
    return SentReply(
        message_id=normalize_message_id(message.get("Message-ID")),
        in_reply_to=in_reply_to[0] if in_reply_to else "",
        references=references,
        # Decoded, so subject + recipient matching compares what Chatwoot shows.
        subject=decode_header_text(message.get("Subject")),
        from_email=_address(sender),
        from_name=_display_name(sender),
        to_emails=_addresses(decode_header_text(message.get("To")))
        + _addresses(decode_header_text(message.get("Cc"))),
        sent_at=sent_at,
        body=_body_text(message),
        uid=str(uid or ""),
        desk_copy=str(message.get(DESK_COPY_HEADER) or "").strip(),
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
        subject=decode_header_text(getattr(message, "subject", "") or ""),
        from_email=_address(getattr(message, "from_", "")),
        to_emails=tuple(_address(value) for value in getattr(message, "to", ()) or ()),
        sent_at=getattr(message, "date", None),
        body=str(getattr(message, "text", "") or ""),
        uid=str(getattr(message, "uid", "") or ""),
        desk_copy=str((headers.get(DESK_COPY_HEADER.lower()) or ("",))[0] or "").strip(),
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
        if reply.desk_copy:
            # Loop guard: the bridge filed this copy of a Chatwoot-sent reply.
            report.bump("desk_copy_skipped")
            continue
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
    port: int = sent_copy.DEFAULT_IMAP_PORT,
    limit: int = 200,
):
    """Read the Sent folder read-only. Nothing here writes to the mailbox."""
    from imap_tools import AND, MailBox

    since = date.today() - timedelta(days=max(lookback_days, 0))
    replies = []
    # login(initial_folder=...) selects the folder read-WRITE. This mailbox is
    # one people are working in, so select it read-only ourselves and let
    # mark_seen=False keep the unread flags as the human left them. The timeout
    # keeps a hung server from wedging the periodic pass forever.
    with MailBox(host, port, timeout=IMAP_TIMEOUT_SECONDS).login(
        username, password, initial_folder=None
    ) as mailbox:
        mailbox.folder.set(folder, readonly=True)
        for message in mailbox.fetch(AND(date_gte=since), mark_seen=False, limit=limit):
            replies.append(from_mail_message(message))
    return replies


# ── Configuration ─────────────────────────────────────────────────────────────


def _enabled(name: str, default: bool, environ=None) -> bool:
    raw = (environ if environ is not None else os.environ).get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int, environ=None) -> int:
    raw = (environ if environ is not None else os.environ).get(name)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def sync_enabled(environ=None) -> bool:
    """The periodic loop runs only when this is explicitly true."""
    return _enabled("SENT_SYNC_ENABLED", False, environ)


def interval_seconds(environ=None) -> int:
    return _positive_int("SENT_SYNC_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS, environ)


def configured_lookback_days(environ=None) -> int:
    return _positive_int("SENT_SYNC_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS, environ)


def configured_mailboxes(environ=None):
    """Every mailbox named in SENT_COPY_INBOXES, as (label, config, problem).

    Credentials and folders come from sent_copy's SENT_COPY_<LABEL>_* settings,
    through ``sent_copy.mailbox_for_inbox``. A label mapped from two inboxes is
    one mailbox and is read once. Returns ``([], problem)`` when the map itself
    is missing or malformed.
    """
    env = environ if environ is not None else os.environ
    mapping = sent_copy.parse_inbox_map(env.get("SENT_COPY_INBOXES", ""))
    if not mapping:
        return [], "inbox_map_not_configured"
    found = []
    seen: set[str] = set()
    for inbox_id, label in sorted(mapping.items()):
        if label in seen:
            continue
        seen.add(label)
        config, problem = sent_copy.mailbox_for_inbox(inbox_id, env)
        found.append((label, config, problem))
    return found, ""


def sent_sync_status(runner: "SentSyncRunner | None" = None, environ=None) -> dict:
    """Configuration and last-run evidence for /health. No secret value is reported."""
    mailboxes, problem = configured_mailboxes(environ)
    value = {
        "enabled": sync_enabled(environ),
        "interval_seconds": interval_seconds(environ),
        "lookback_days": configured_lookback_days(environ),
        "mailboxes": {
            label: {
                "ready": config is not None,
                "problem": problem_ or None,
                "folder": config.folder if config is not None else None,
            }
            for label, config, problem_ in mailboxes
        },
        "problem": problem or None,
    }
    value.update((runner or SentSyncRunner(lambda: PassReport())).snapshot())
    return value


# ── One pass ──────────────────────────────────────────────────────────────────


def sync_mailbox(
    config,
    *,
    store,
    client,
    dry_run: bool,
    lookback_days: int,
    fetch=fetch_sent_replies,
) -> SyncReport:
    """Fetch one mailbox's Sent folder and place its replies."""
    report = SyncReport()
    try:
        replies = fetch(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            folder=config.folder,
            lookback_days=lookback_days,
        )
    except Exception as exc:
        # A type name only: an IMAP error can echo what the server was sent.
        report.bump("fetch_failed")
        report.problem = f"fetch_failed:{type(exc).__name__}"
        log.error(
            "sent folder fetch failed mailbox=%s host=%s error=%s",
            config.label,
            config.host,
            type(exc).__name__,
        )
        return report
    return sync_replies(replies, store=store, client=client, dry_run=dry_run, report=report)


def run_sent_sync(
    *,
    store,
    client,
    dry_run: bool | None = None,
    lookback_days: int | None = None,
    mailbox: str | None = None,
    fetch=fetch_sent_replies,
    environ=None,
) -> PassReport:
    """One pass over every configured mailbox (or only ``mailbox``).

    A mailbox whose configuration has a problem, or whose fetch fails, is
    reported and skipped; the others still sync. ``dry_run=None`` follows
    BRIDGE_DRY_RUN (default true).
    """
    env = environ if environ is not None else os.environ
    report = PassReport()
    mailboxes, problem = configured_mailboxes(env)
    if problem:
        report.problem = problem
        log.warning("sent sync has no mailboxes: %s", problem)
        return report
    if mailbox is not None:
        wanted = mailbox.strip().lower()
        mailboxes = [entry for entry in mailboxes if entry[0] == wanted]
        if not mailboxes:
            report.problem = f"mailbox_not_configured:{wanted}"
            return report
    effective_dry_run = _enabled("BRIDGE_DRY_RUN", True, env) if dry_run is None else dry_run
    days = lookback_days if lookback_days is not None else configured_lookback_days(env)
    for label, config, config_problem in mailboxes:
        if config is None:
            skipped = SyncReport(problem=config_problem)
            skipped.bump("mailbox_skipped")
            report.mailboxes[label] = skipped
            log.warning("sent sync skipped mailbox=%s problem=%s", label, config_problem)
            continue
        report.mailboxes[label] = sync_mailbox(
            config,
            store=store,
            client=client,
            dry_run=effective_dry_run,
            lookback_days=days,
            fetch=fetch,
        )
    return report


# ── The periodic loop ─────────────────────────────────────────────────────────


class SentSyncRunner:
    """Runs passes one at a time and keeps what /health reports about them.

    ``run_pass`` is blocking (IMAP); the loop calls it in a worker thread. The
    lock is the overlap guard: a pass that is still running makes the next one a
    counted skip, never a second concurrent pass.
    """

    def __init__(self, run_pass, *, clock=None):
        self._run_pass = run_pass
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.passes = 0
        self.failures = 0
        self.overlaps_skipped = 0
        self.last_run_at: str | None = None
        self.last_counts: dict | None = None
        self.last_error: str | None = None
        self.loop_running = False

    def run_once(self) -> bool:
        """One guarded pass. Returns False when a pass was already running."""
        if not self._lock.acquire(blocking=False):
            with self._state_lock:
                self.overlaps_skipped += 1
            log.warning("sent sync pass skipped: the previous pass is still running")
            return False
        try:
            started = self._clock().isoformat()
            try:
                report = self._run_pass()
            except Exception as exc:
                log.exception("sent sync pass failed")
                with self._state_lock:
                    self.passes += 1
                    self.failures += 1
                    self.last_run_at = started
                    self.last_error = type(exc).__name__
                return True
            counts = {
                label: {"counts": dict(mailbox.counts), "problem": mailbox.problem or None}
                for label, mailbox in report.mailboxes.items()
            }
            with self._state_lock:
                self.passes += 1
                self.last_run_at = started
                self.last_counts = counts
                self.last_error = report.problem or None
            log.info("sent sync pass done totals=%s", dict(report.totals()))
            return True
        finally:
            self._lock.release()

    async def run_forever(self, interval: float, stop: asyncio.Event) -> None:
        """Run a pass now, then every ``interval`` seconds, until ``stop`` is set."""
        self.loop_running = True
        try:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(self.run_once)
                except Exception:  # run_once already contains the pass; belt and braces
                    log.exception("sent sync loop iteration failed")
                    with self._state_lock:
                        self.failures += 1
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.loop_running = False

    def snapshot(self) -> dict:
        with self._state_lock:
            return {
                "loop_running": self.loop_running,
                "passes": self.passes,
                "failures": self.failures,
                "overlaps_skipped": self.overlaps_skipped,
                "last_run_at": self.last_run_at,
                "last_counts": self.last_counts,
                "last_error": self.last_error,
            }


def start_loop(runner: SentSyncRunner, environ=None):
    """Start the background loop if, and only if, SENT_SYNC_ENABLED is true.

    Returns ``(task, stop_event)``, or ``(None, None)`` when disabled. Must be
    called from inside the running event loop (the app's lifespan).
    """
    if not sync_enabled(environ):
        log.info("sent sync loop not started: SENT_SYNC_ENABLED is not true")
        return None, None
    stop = asyncio.Event()
    interval = interval_seconds(environ)
    task = asyncio.create_task(runner.run_forever(interval, stop), name="sent-sync-loop")
    log.info("sent sync loop started interval=%ss", interval)
    return task, stop


async def stop_loop(task, stop) -> None:
    if task is None:
        return
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


# ── Attended entry point ──────────────────────────────────────────────────────


def format_report(report: PassReport) -> str:
    """Human-readable, one block per mailbox."""
    lines = []
    if report.problem:
        lines.append(f"problem: {report.problem}")
    for label, mailbox in report.mailboxes.items():
        lines.append(f"== {label} ==")
        if mailbox.problem:
            lines.append(f"  problem: {mailbox.problem}")
        for name, count in sorted(mailbox.counts.items()):
            lines.append(f"  {name}: {count}")
        for item in mailbox.unresolved:
            lines.append(f"  unresolved: {item}")
    if report.mailboxes:
        lines.append("== totals ==")
        for name, count in sorted(report.totals().items()):
            lines.append(f"  {name}: {count}")
    return "\n".join(lines)


def main(argv=None, *, store=None, client=None, fetch=fetch_sent_replies) -> int:
    """Attended entry point: one pass, printed as a report per mailbox.

    Deliberately a command rather than an HTTP route. The bridge has no operator
    authentication - only Chatwoot's webhook HMAC - so an endpoint that reads a
    mailbox and writes notes would be a new unauthenticated surface. It does not
    need SENT_SYNC_ENABLED, which only gates the bridge's periodic loop.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Sync the Sent folder into Chatwoot.")
    parser.add_argument(
        "--post",
        action="store_true",
        help="post private notes; without it the pass is a dry run",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        metavar="N",
        help=f"days of Sent mail to read (default SENT_SYNC_LOOKBACK_DAYS or {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--mailbox",
        default=None,
        metavar="LABEL",
        help="only this SENT_COPY_INBOXES label (default: every mailbox)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    arguments = parser.parse_args(argv)
    if arguments.lookback_days is not None and arguments.lookback_days < 0:
        parser.error("--lookback-days must be 0 or more")

    if store is None or client is None:
        from main import chatwoot_client, dedup_store

        store = store or dedup_store()
        client = client or chatwoot_client()

    report = run_sent_sync(
        store=store,
        client=client,
        dry_run=not arguments.post,
        lookback_days=arguments.lookback_days,
        mailbox=arguments.mailbox,
        fetch=fetch,
    )
    print("mode: " + ("POST private notes" if arguments.post else "dry run (use --post to post)"))
    if arguments.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return 1 if report.problem else 0


if __name__ == "__main__":  # pragma: no cover - attended entry point
    raise SystemExit(main())

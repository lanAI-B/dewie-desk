"""Give Outlook (and Dewie's memory) the replies that were sent from Chatwoot.

The team works the mailbox in Outlook. A reply sent from Chatwoot goes out over
Chatwoot's SMTP and never lands in the mailbox's Sent folder, so the people in
Outlook cannot see it and ask Lana what was said. This module is the opposite
direction of ``sent_sync`` (on feature/desk-sent-continuity), which reads Sent
and posts Outlook replies into Chatwoot as private notes.

Trigger: a Chatwoot webhook for an OUTGOING, PUBLIC message on an EMAIL inbox
that carries a ``source_id``. Chatwoot v4.16.1 creates the message first (the
``message_created`` webhook has no source_id), sends it in SendReplyJob, and
only after ``deliver_now`` succeeds runs ``message.update(source_id:
reply_mail.message_id)`` - which fires ``message_updated``. A source_id is
therefore the proof the mail actually left, and it IS the Message-ID the
customer received. A failed send never gets one.

Safety invariants:

* IMAP APPEND only. The connection is wrapped so this module can reach LOGIN,
  APPEND and LOGOUT and nothing else; there is no SMTP import anywhere here.
  Appending a message to a folder never sends it.
* Every appended copy carries ``X-Dewie-Desk-Copy: <chatwoot message id>`` so
  the Sent-folder reader skips it instead of posting it back into Chatwoot.
* One durable claim per Chatwoot message id (the bridge's SQLite claim table),
  so a webhook retry, a later status update, or a restart cannot append twice.
  A failed append releases the claim for the next delivery of the event.
"""

from __future__ import annotations

import imaplib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr

log = logging.getLogger("dewie-desk-bridge.sent-copy")

COPY_HEADER = "X-Dewie-Desk-Copy"
SENT_EVENTS = frozenset({"message_created", "message_updated"})
EMAIL_CHANNEL = "Channel::Email"
DEFAULT_IMAP_HOST = "secure.emailsrvr.com"
DEFAULT_IMAP_PORT = 993
# Chatwoot's fallback subject when a conversation has no mail_subject
# (conversations.reply.email_subject in config/locales/en.yml, v4.16.1).
FALLBACK_SUBJECT = "New messages on this conversation"


def _enabled(name: str, environ=None) -> bool:
    raw = (environ if environ is not None else os.environ).get(name)
    return raw is not None and raw.strip().lower() in {"1", "true", "yes", "on"}


def copy_enabled(environ=None) -> bool:
    """#6333: append Chatwoot-sent replies to the mailbox Sent folder."""
    return _enabled("BRIDGE_SENT_COPY_ENABLED", environ)


def memory_enabled(environ=None) -> bool:
    """#6334: record Chatwoot-sent replies in DewieOps conv_memory."""
    return _enabled("BRIDGE_CONV_MEMORY_ENABLED", environ)


def any_enabled(environ=None) -> bool:
    return copy_enabled(environ) or memory_enabled(environ)


# ── Screening the webhook ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SentMessage:
    """One Chatwoot reply that provably went to the customer."""

    account_id: int
    conversation_id: int
    message_id: int
    inbox_id: int | None
    inbox_name: str
    rfc822_message_id: str  # without angle brackets, exactly as Chatwoot stored it
    content: str
    created_at: datetime
    agent_name: str
    to_emails: tuple[str, ...]
    cc_emails: tuple[str, ...] = ()
    bcc_emails: tuple[str, ...] = ()
    mail_subject: str = ""
    display_id: int | None = None
    attachment_names: tuple[str, ...] = ()

    @property
    def customer_email(self) -> str:
        return self.to_emails[0] if self.to_emails else ""


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_type(value) -> str:
    if isinstance(value, int):
        return {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}.get(value, str(value))
    return str(value or "").strip().lower()


def _emails(value) -> tuple[str, ...]:
    """Chatwoot stores cc/to as an array; tolerate a comma string too."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    found: list[str] = []
    for item in items:
        address = str(item or "").strip().strip("<>").lower()
        if "@" in address and address not in found and not any(c in address for c in "\r\n"):
            found.append(address)
    return tuple(found)


def normalize_message_id(value) -> str:
    text = str(value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    if not text or any(c.isspace() for c in text) or "<" in text or ">" in text:
        return ""
    return text


def _created_at(value) -> datetime:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def screen_sent(payload) -> tuple[SentMessage | None, str]:
    """Return the sent reply this webhook proves, or why it is not one."""
    payload = _dict(payload)
    if payload.get("event") not in SENT_EVENTS:
        return None, "not_sent_event"
    if _message_type(payload.get("message_type")) != "outgoing":
        return None, "not_outgoing"
    if bool(payload.get("private")):
        return None, "private_note"
    conversation = _dict(payload.get("conversation"))
    channel = str(conversation.get("channel") or "").strip()
    if channel and channel != EMAIL_CHANNEL:
        return None, "not_email_channel"
    rfc822_id = normalize_message_id(payload.get("source_id"))
    if not rfc822_id:
        # Created but not (yet) delivered, or the send failed.
        return None, "not_sent_yet"
    message_id = _int(payload.get("id"))
    conversation_id = _int(conversation.get("id") or payload.get("conversation_id"))
    if not message_id or not conversation_id:
        return None, "missing_ids"

    attributes = _dict(payload.get("content_attributes"))
    meta = _dict(conversation.get("meta"))
    contact = _dict(meta.get("sender"))
    # Mirror ConversationReplyMailer#to_emails: explicit to_emails, else the contact.
    to_emails = _emails(attributes.get("to_emails")) or _emails([contact.get("email")])
    if not to_emails:
        to_emails = _emails([_dict(conversation.get("contact_inbox")).get("source_id")])
    if not to_emails:
        return None, "missing_recipient"

    attachments = tuple(
        str(_dict(item).get("file_name") or _dict(item).get("name") or "attachment").strip()
        for item in payload.get("attachments") or []
        if isinstance(item, dict)
    )
    content = str(payload.get("content") or "").strip()
    if not content and not attachments:
        return None, "empty_content"

    sender = _dict(payload.get("sender"))
    inbox = _dict(payload.get("inbox"))
    account = _dict(payload.get("account"))
    return SentMessage(
        account_id=_int(account.get("id")) or 0,
        conversation_id=conversation_id,
        message_id=message_id,
        inbox_id=_int(inbox.get("id") or conversation.get("inbox_id")),
        inbox_name=str(inbox.get("name") or "").strip(),
        rfc822_message_id=rfc822_id,
        content=content,
        created_at=_created_at(payload.get("created_at")),
        agent_name=str(sender.get("name") or sender.get("available_name") or "").strip(),
        to_emails=to_emails,
        cc_emails=_emails(attributes.get("cc_emails")),
        bcc_emails=_emails(attributes.get("bcc_emails")),
        mail_subject=str(
            _dict(conversation.get("additional_attributes")).get("mail_subject") or ""
        ).strip(),
        display_id=conversation_id,
        attachment_names=attachments,
    ), ""


# ── Threading: rebuild the headers Chatwoot's mailer used ─────────────────────


@dataclass(frozen=True)
class Threading:
    subject: str
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    source: str = "none"  # "conversation" when rebuilt from the replied-to message


def _subject(sent: SentMessage, chat_count: int | None) -> str:
    """ConversationReplyMailer#mail_subject: 'Re: ' once the thread has >1 message."""
    if not sent.mail_subject:
        return f"[#{sent.display_id}] {FALLBACK_SUBJECT}"
    if chat_count is not None and chat_count <= 1:
        return sent.mail_subject
    return f"Re: {sent.mail_subject}"


def default_threading(sent: SentMessage) -> Threading:
    return Threading(subject=_subject(sent, None))


def rebuild_threading(sent: SentMessage, messages) -> Threading:
    """Same rule as the v4.16.1 mailer: In-Reply-To is the newest INCOMING
    message's email.message_id; References is that message's References plus it.

    ``messages`` is the conversation page from GET .../messages. Only messages
    older than this reply count - that is what the mailer saw when it sent.
    """
    chat = []
    incoming = []
    for raw in messages or ():
        item = _dict(raw)
        found = _int(item.get("id"))
        if found is None or found > sent.message_id:
            continue
        kind = _message_type(item.get("message_type"))
        if kind in {"incoming", "outgoing"}:
            chat.append(found)
        if kind == "incoming":
            incoming.append((found, item))
    in_reply_to = ""
    references: list[str] = []
    if incoming:
        _, replied = max(incoming, key=lambda pair: pair[0])
        email = _dict(_dict(replied.get("content_attributes")).get("email"))
        in_reply_to = normalize_message_id(email.get("message_id"))
        raw_refs = email.get("references")
        for ref in raw_refs if isinstance(raw_refs, list) else str(raw_refs or "").split():
            cleaned = normalize_message_id(ref)
            if cleaned and cleaned not in references:
                references.append(cleaned)
    if in_reply_to and in_reply_to not in references:
        references.append(in_reply_to)
    return Threading(
        subject=_subject(sent, len(chat) if chat else None),
        in_reply_to=in_reply_to,
        references=tuple(references),
        source="conversation" if in_reply_to else "none",
    )


# ── The RFC822 copy ───────────────────────────────────────────────────────────


def _header_safe(value: str) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def build_copy(sent: SentMessage, threading: Threading, from_address: str) -> bytes:
    """Render the copy Outlook will file under Sent. Never handed to a sender."""
    message = EmailMessage()
    display = _header_safe(sent.agent_name)
    if display and sent.inbox_name:
        display = f"{display} from {_header_safe(sent.inbox_name)}"
    message["From"] = formataddr((display, from_address)) if display else from_address
    message["To"] = ", ".join(sent.to_emails)
    if sent.cc_emails:
        message["Cc"] = ", ".join(sent.cc_emails)
    if sent.bcc_emails:
        message["Bcc"] = ", ".join(sent.bcc_emails)
    message["Subject"] = _header_safe(threading.subject)
    message["Date"] = format_datetime(sent.created_at)
    message["Message-ID"] = f"<{sent.rfc822_message_id}>"
    if threading.in_reply_to:
        message["In-Reply-To"] = f"<{threading.in_reply_to}>"
    if threading.references:
        message["References"] = " ".join(f"<{ref}>" for ref in threading.references)
    message[COPY_HEADER] = str(sent.message_id)
    body = sent.content
    if sent.attachment_names:
        listed = ", ".join(_header_safe(name) for name in sent.attachment_names)
        body = f"{body}\n\n[Sent from Chatwoot with attachments not copied here: {listed}]".strip()
    message.set_content(body + "\n", charset="utf-8")
    return message.as_bytes(policy=SMTP)


# ── Which mailbox a Chatwoot inbox belongs to ─────────────────────────────────


@dataclass(frozen=True)
class MailboxConfig:
    label: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    folder: str
    from_address: str


def parse_inbox_map(raw: str) -> dict[int, str]:
    """``"1:abs,3:actex"`` -> {1: "abs", 3: "actex"}. Strict: one bad pair = {}.

    Same reasoning as BRIDGE_OUTBOUND_INBOX_IDS: a config typo must not become a
    guess about which mailbox a customer's reply is filed in.
    """
    found: dict[int, str] = {}
    for pair in str(raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        inbox, sep, label = pair.partition(":")
        inbox_id = _int(inbox.strip())
        label = label.strip().lower()
        if not sep or inbox_id is None or not label.isidentifier() or inbox_id in found:
            return {}
        found[inbox_id] = label
    return found


def mailbox_for_inbox(inbox_id: int | None, environ=None) -> tuple[MailboxConfig | None, str]:
    env = environ if environ is not None else os.environ
    mapping = parse_inbox_map(env.get("SENT_COPY_INBOXES", ""))
    if not mapping:
        return None, "inbox_map_not_configured"
    label = mapping.get(inbox_id) if inbox_id is not None else None
    if label is None:
        return None, "inbox_not_mapped"
    prefix = f"SENT_COPY_{label.upper()}_"
    username = (env.get(prefix + "USERNAME") or "").strip()
    password = env.get(prefix + "PASSWORD") or ""
    folder = (env.get(prefix + "FOLDER") or "").strip()
    if not username or not password:
        return None, "mailbox_credentials_missing"
    if not folder:
        # No default on purpose: Rackspace calls it INBOX.Sent, M365 "Sent Items".
        # APPEND never creates a folder, but guessing wrong would fail silently-ish.
        return None, "mailbox_folder_missing"
    port = _int(env.get(prefix + "IMAP_PORT")) or DEFAULT_IMAP_PORT
    return MailboxConfig(
        label=label,
        host=(env.get(prefix + "IMAP_HOST") or DEFAULT_IMAP_HOST).strip(),
        port=port,
        username=username,
        password=password,
        folder=folder,
        from_address=(env.get(prefix + "FROM") or username).strip(),
    ), ""


def status(environ=None) -> dict:
    """Configuration evidence for /health. Never reports a secret value."""
    env = environ if environ is not None else os.environ
    mapping = parse_inbox_map(env.get("SENT_COPY_INBOXES", ""))
    mailboxes = {}
    for inbox_id, label in sorted(mapping.items()):
        config, problem = mailbox_for_inbox(inbox_id, env)
        mailboxes[label] = {"inbox_id": inbox_id, "ready": config is not None,
                            "problem": problem or None}
    return {
        "copy_enabled": copy_enabled(env),
        "memory_enabled": memory_enabled(env),
        "mailboxes": mailboxes,
    }


# ── IMAP: APPEND and nothing else ─────────────────────────────────────────────


class AppendOnlyMailbox:
    """The whole IMAP surface this module may use. Anything else is unreachable."""

    def __init__(self, connection):
        self._connection = connection

    def login(self, username: str, password: str):
        return self._connection.login(username, password)

    def append(self, folder: str, flags: str, date_time: str, message: bytes):
        return self._connection.append(folder, flags, date_time, message)

    def logout(self):
        return self._connection.logout()


def default_connect(host: str, port: int):
    return imaplib.IMAP4_SSL(host, port, timeout=30)


def _quote_folder(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def append_copy(config: MailboxConfig, raw: bytes, when: datetime, *, connect=default_connect):
    """APPEND one message to the Sent folder flagged \\Seen. Returns (ok, detail)."""
    try:
        mailbox = AppendOnlyMailbox(connect(config.host, config.port))
    except Exception as exc:
        return False, f"connect_error:{type(exc).__name__}"
    try:
        mailbox.login(config.username, config.password)
        status_, data = mailbox.append(
            _quote_folder(config.folder),
            r"(\Seen)",
            imaplib.Time2Internaldate(when),
            raw,
        )
        if status_ != "OK":
            return False, f"append_{str(status_).lower()}"
        return True, "appended"
    except Exception as exc:  # the detail is a type name: never a server echo of creds
        return False, f"append_error:{type(exc).__name__}"
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


# ── One sent reply, both destinations ─────────────────────────────────────────


def copy_claim_key(sent: SentMessage) -> str:
    return f"sent-copy:{sent.account_id}:{sent.message_id}"


def memory_claim_key(sent: SentMessage) -> str:
    return f"conv-memory:{sent.account_id}:{sent.message_id}"


def copy_to_sent_folder(sent: SentMessage, *, store, client, connect=default_connect,
                        environ=None) -> str:
    """#6333. Returns a short outcome code for metrics and logs."""
    config, problem = mailbox_for_inbox(sent.inbox_id, environ)
    if config is None:
        return problem
    key = copy_claim_key(sent)
    if not store.claim(key):
        return "duplicate"
    threading = default_threading(sent)
    try:
        conversation = client.get_conversation(sent.conversation_id)
        if conversation.ok:
            threading = rebuild_threading(sent, conversation.messages)
    except Exception as exc:
        log.warning("sent copy threading lookup failed conversation=%s error=%s",
                    sent.conversation_id, type(exc).__name__)
    raw = build_copy(sent, threading, config.from_address)
    ok, detail = append_copy(config, raw, sent.created_at, connect=connect)
    if not ok:
        store.release(key)
        log.error("sent copy failed conversation=%s message=%s mailbox=%s detail=%s",
                  sent.conversation_id, sent.message_id, config.label, detail)
        return "append_failed"
    log.info("sent copy appended conversation=%s message=%s mailbox=%s threading=%s",
             sent.conversation_id, sent.message_id, config.label, threading.source)
    return "appended" if threading.source == "conversation" else "appended_unthreaded"


def record_in_memory(sent: SentMessage, *, store, writer) -> str:
    """#6334. Returns a short outcome code for metrics and logs."""
    key = memory_claim_key(sent)
    if not store.claim(key):
        return "duplicate"
    try:
        inserted = writer.write(sent, subject=default_threading(sent).subject)
    except Exception as exc:
        store.release(key)
        log.error("conv_memory write failed conversation=%s message=%s error=%s",
                  sent.conversation_id, sent.message_id, type(exc).__name__)
        return "write_failed"
    return "written" if inserted else "already_present"


def process_sent(sent: SentMessage, *, store, client, writer_factory, connect=default_connect,
                 environ=None) -> dict:
    """Run whichever destinations are enabled. Each fails independently."""
    outcome = {}
    if copy_enabled(environ):
        outcome["sent_copy"] = copy_to_sent_folder(
            sent, store=store, client=client, connect=connect, environ=environ)
    if memory_enabled(environ):
        outcome["conv_memory"] = record_in_memory(sent, store=store, writer=writer_factory())
    return outcome

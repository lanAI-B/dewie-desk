"""Sent-folder continuity: place an externally authored reply, post it once."""

from dataclasses import dataclass

import pytest

from parser import parse_message_created
from state import DedupStore
import sent_sync


@dataclass
class FakePost:
    ok: bool = True
    status_code: int = 200
    message_id: int = 91
    detail: str = "posted"


class FakeClient:
    """Records every call so a test can prove nothing but a private note happens."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def post_private_note(self, conversation_id, content):
        self.calls.append(("post_private_note", conversation_id, content))
        return FakePost(ok=self.ok, detail="posted" if self.ok else "HTTP 500")

    def __getattr__(self, name):  # pragma: no cover - only hit by a wrong call
        raise AssertionError(f"sent sync must not call {name}")


def inbound_payload(**updates):
    value = {
        "event": "message_created",
        "id": 42,
        "message_type": "incoming",
        "private": False,
        "content": "My access code will not work.",
        "content_attributes": {
            "email": {
                "subject": "Access question",
                "message_id": "<inbound-1@mail.example>",
                "to": ["support@actexlearning.com"],
            }
        },
        "sender": {"email": "person@example.com", "type": "contact"},
        "conversation": {"id": 7, "channel": "Channel::Email"},
        "inbox": {"id": 3, "name": "Support"},
        "account": {"id": 1},
    }
    value.update(updates)
    return value


def sent_bytes(
    message_id="<sent-1@outlook.example>",
    in_reply_to="<inbound-1@mail.example>",
    references=None,
    subject="Re: Access question",
    to="person@example.com",
    body="Your code is reset, please try again.",
):
    headers = [
        "From: Paula Reyes <paula@actexlearning.com>",
        f"To: {to}",
        f"Subject: {subject}",
        "Date: Tue, 15 Sep 2026 14:05:00 -0400",
        f"Message-ID: {message_id}",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    if references:
        headers.append(f"References: {references}")
    headers.append("Content-Type: text/plain; charset=utf-8")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


@pytest.fixture
def store(tmp_path):
    return DedupStore(tmp_path / "bridge.sqlite3")


def test_inbound_webhook_builds_the_thread_map(store):
    message = parse_message_created(inbound_payload())

    assert message.rfc822_message_id == "inbound-1@mail.example"
    assert message.to_emails == ["support@actexlearning.com"]
    assert store.record_inbound(message)
    assert store.resolve_thread(["msgid:inbound-1@mail.example"]) == 7
    assert store.resolve_thread(["thread:access question|person@example.com"]) == 7


def test_reply_resolves_by_in_reply_to_and_posts_one_private_note(store):
    store.record_inbound(parse_message_created(inbound_payload()))
    reply = sent_sync.parse_sent_email(sent_bytes())
    client = FakeClient()

    report = sent_sync.sync_replies([reply], store=store, client=client, dry_run=False)

    assert report.counts["resolved_in_reply_to"] == 1
    assert report.counts["private_notes_posted"] == 1
    assert len(client.calls) == 1
    call, conversation_id, content = client.calls[0]
    assert call == "post_private_note"
    assert conversation_id == 7
    assert content.startswith("[sent by Paula Reyes via Outlook 2026-09-15 18:05 UTC]")
    assert "Your code is reset" in content


def test_the_same_sent_message_is_never_posted_twice(store):
    store.record_inbound(parse_message_created(inbound_payload()))
    reply = sent_sync.parse_sent_email(sent_bytes())
    client = FakeClient()

    sent_sync.sync_replies([reply], store=store, client=client, dry_run=False)
    second = sent_sync.sync_replies([reply], store=store, client=client, dry_run=False)

    assert second.counts["duplicate_sent"] == 1
    assert second.counts["private_notes_posted"] == 0
    assert len(client.calls) == 1


def test_dedup_survives_a_restart_and_a_reparsed_copy(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    DedupStore(path).record_inbound(parse_message_created(inbound_payload()))
    client = FakeClient()

    sent_sync.sync_replies(
        [sent_sync.parse_sent_email(sent_bytes())],
        store=DedupStore(path),
        client=client,
        dry_run=False,
    )
    after_restart = sent_sync.sync_replies(
        [sent_sync.parse_sent_email(sent_bytes(message_id="<SENT-1@Outlook.Example>"))],
        store=DedupStore(path),
        client=client,
        dry_run=False,
    )

    assert after_restart.counts["duplicate_sent"] == 1
    assert len(client.calls) == 1


def test_references_place_a_reply_that_lost_in_reply_to(store):
    store.record_inbound(parse_message_created(inbound_payload()))
    reply = sent_sync.parse_sent_email(sent_bytes(
        in_reply_to="",
        references="<older@mail.example> <inbound-1@mail.example>",
    ))
    client = FakeClient()

    report = sent_sync.sync_replies([reply], store=store, client=client, dry_run=False)

    assert report.counts["resolved_references"] == 1
    assert client.calls[0][1] == 7


def test_subject_and_recipient_are_the_last_resort(store):
    store.record_inbound(parse_message_created(inbound_payload()))
    reply = sent_sync.parse_sent_email(sent_bytes(in_reply_to="", references=""))
    client = FakeClient()

    report = sent_sync.sync_replies([reply], store=store, client=client, dry_run=False)

    assert report.counts["resolved_subject_recipient"] == 1
    assert client.calls[0][1] == 7


def test_an_unplaceable_reply_is_reported_not_guessed(store):
    store.record_inbound(parse_message_created(inbound_payload()))
    reply = sent_sync.parse_sent_email(sent_bytes(
        in_reply_to="<unknown@mail.example>",
        subject="Re: something else entirely",
        to="stranger@example.com",
    ))
    client = FakeClient()

    report = sent_sync.sync_replies([reply], store=store, client=client, dry_run=False)

    assert report.counts["unresolved"] == 1
    assert report.unresolved == ["Re: something else entirely -> stranger@example.com"]
    assert client.calls == []


def test_dry_run_posts_nothing_and_stays_retryable(store):
    store.record_inbound(parse_message_created(inbound_payload()))
    reply = sent_sync.parse_sent_email(sent_bytes())
    dry_client = FakeClient()
    live_client = FakeClient()

    dry = sent_sync.sync_replies([reply], store=store, client=dry_client, dry_run=True)
    wet = sent_sync.sync_replies([reply], store=store, client=live_client, dry_run=False)

    assert dry.counts["notes_avoided_dry_run"] == 1
    assert dry_client.calls == []
    assert wet.counts["private_notes_posted"] == 1
    assert len(live_client.calls) == 1


def test_a_failed_post_is_released_for_the_next_pass(store):
    store.record_inbound(parse_message_created(inbound_payload()))
    reply = sent_sync.parse_sent_email(sent_bytes())
    failing = FakeClient(ok=False)

    first = sent_sync.sync_replies([reply], store=store, client=failing, dry_run=False)
    recovered = sent_sync.sync_replies(
        [reply], store=store, client=FakeClient(), dry_run=False
    )

    assert first.counts["note_post_failed"] == 1
    assert recovered.counts["private_notes_posted"] == 1


def test_quoted_history_is_trimmed_but_never_to_nothing():
    reply = sent_sync.parse_sent_email(sent_bytes(
        body="Refund is processed.\n\nOn Mon, Sep 14 2026, person wrote:\n> where is it?",
    ))
    assert sent_sync.format_note(reply).endswith("Refund is processed.")

    quoted_only = sent_sync.parse_sent_email(sent_bytes(
        body="-----Original Message-----\n> where is it?",
    ))
    assert "where is it?" in sent_sync.format_note(quoted_only)


def test_a_long_reply_is_truncated_rather_than_dropped():
    reply = sent_sync.parse_sent_email(sent_bytes(body="x" * (sent_sync.NOTE_BODY_LIMIT + 50)))
    note = sent_sync.format_note(reply)

    assert note.endswith("[truncated]")
    assert len(note) < sent_sync.NOTE_BODY_LIMIT + 200


def test_html_only_reply_still_yields_readable_text():
    raw = (
        "From: Paula <paula@actexlearning.com>\r\n"
        "To: person@example.com\r\n"
        "Subject: Re: Access question\r\n"
        "Message-ID: <html-1@outlook.example>\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><body><p>Access restored.</p></body></html>"
    ).encode("utf-8")

    assert "Access restored." in sent_sync.parse_sent_email(raw).body


def test_disabled_is_the_default_and_makes_no_transport_call(monkeypatch, store):
    monkeypatch.delenv("SENT_SYNC_ENABLED", raising=False)
    client = FakeClient()

    report = sent_sync.run_sent_sync(store=store, client=client)

    assert report.counts["disabled"] == 1
    assert client.calls == []


def test_enabled_without_credentials_reports_instead_of_crashing(monkeypatch, store):
    monkeypatch.setenv("SENT_SYNC_ENABLED", "true")
    monkeypatch.setenv("SENT_SYNC_USERNAME", "")
    monkeypatch.setenv("SENT_SYNC_PASSWORD", "")

    report = sent_sync.run_sent_sync(store=store, client=FakeClient())

    assert report.counts["missing_credentials"] == 1


def test_fetch_is_read_only_and_bounded(monkeypatch):
    captured = {}

    class FakeFolderManager:
        def set(self, folder, readonly=False):
            captured.update(folder=folder, readonly=readonly)

    class FakeMailbox:
        def __init__(self, host):
            captured["host"] = host
            self.folder = FakeFolderManager()

        def login(self, username, password, initial_folder="INBOX"):
            captured.update(
                username=username, password=password, initial_folder=initial_folder
            )
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch(self, criteria, **kwargs):
            captured["fetch_kwargs"] = kwargs
            captured["criteria"] = criteria
            return []

    import imap_tools

    monkeypatch.setattr(imap_tools, "MailBox", FakeMailbox)

    sent_sync.fetch_sent_replies(
        host="secure.emailsrvr.com",
        username="support@actexlearning.com",
        password="secret",
        folder="Sent",
        lookback_days=14,
    )

    assert captured["initial_folder"] is None
    assert captured["folder"] == "Sent"
    assert captured["readonly"] is True
    assert captured["fetch_kwargs"]["mark_seen"] is False
    assert captured["fetch_kwargs"]["limit"] == 200


def test_a_message_without_raw_source_still_normalizes(store):
    """imap_tools exposes .obj, but a message that lacks it must not crash a pass."""

    class HeaderOnly:
        uid = "77"
        subject = "Re: Access question"
        from_ = "Paula Reyes <paula@actexlearning.com>"
        to = ("person@example.com",)
        date = None
        text = "Handled by phone."
        headers = {
            "message-id": ("<header-only@outlook.example>",),
            "in-reply-to": ("<inbound-1@mail.example>",),
        }

    reply = sent_sync.from_mail_message(HeaderOnly())

    assert reply.message_id == "header-only@outlook.example"
    assert reply.in_reply_to == "inbound-1@mail.example"
    assert reply.to_emails == ("person@example.com",)
    assert sent_sync.note_claim_key(reply) == "sent-note:msgid:header-only@outlook.example"

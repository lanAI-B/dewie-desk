"""Chatwoot-sent replies -> mailbox Sent folder (#6333) and conv_memory (#6334).

Nothing here reaches a real mailbox, Chatwoot, or database: the IMAP client
raises on anything but LOGIN/SELECT/APPEND (and LOGOUT to hang up), the
Chatwoot client raises on anything but get_conversation, and conv_memory is an
in-memory SQLite table built from the DewieOps schema.
"""

import email
import json
from datetime import datetime, timezone
from email import policy

import pytest
import sqlalchemy
from fastapi.testclient import TestClient

import main
import sent_copy
import webhook_auth
from chatwoot import ConversationResult
from conv_memory_sync import ConvMemoryWriter, session_id_for
from state import DedupStore

SECRET = "correct-horse-battery-staple"


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeIMAP:
    """Records calls. Anything but LOGIN/SELECT/APPEND/LOGOUT is a test failure."""

    ALLOWED = {"login", "select", "append", "logout"}

    def __init__(self, append_status="OK"):
        self.calls = []
        self.appended = []
        self.append_status = append_status

    def __getattr__(self, name):
        raise AssertionError(f"IMAP call not allowed: {name}")

    def login(self, username, password):
        self.calls.append(("login", username))
        return "OK", [b"logged in"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder))
        return "OK", [b"1"]

    def append(self, folder, flags, date_time, message):
        self.calls.append(("append", folder))
        self.appended.append({"folder": folder, "flags": flags, "date": date_time,
                              "raw": message})
        return self.append_status, [b"done"]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]


class Connector:
    def __init__(self, imap=None, fail=False):
        self.imap = imap or FakeIMAP()
        self.fail = fail
        self.opened = []

    def __call__(self, host, port):
        self.opened.append((host, port))
        if self.fail:
            raise ConnectionRefusedError("no route")
        return self.imap


class FakeChatwoot:
    """Only get_conversation exists. Posting anything would be a test failure."""

    def __init__(self, messages=(), ok=True):
        self.messages = tuple(messages)
        self.ok = ok
        self.fetched = []

    def __getattr__(self, name):
        raise AssertionError(f"Chatwoot call not allowed: {name}")

    def get_conversation(self, conversation_id):
        self.fetched.append(conversation_id)
        if not self.ok:
            return ConversationResult(False, status_code=500, detail="HTTP 500")
        return ConversationResult(True, self.messages, {}, 200, "fetched")


class ListWriter:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def write(self, sent, subject=""):
        if self.fail:
            raise RuntimeError("db down")
        self.rows.append((sent.message_id, subject))
        return True


@pytest.fixture(autouse=True)
def sent_copy_env(monkeypatch):
    for name in ("BRIDGE_SENT_COPY_ENABLED", "BRIDGE_CONV_MEMORY_ENABLED", "SENT_COPY_INBOXES"):
        monkeypatch.delenv(name, raising=False)
    for label in ("ABS", "ACTEX"):
        for suffix in ("IMAP_HOST", "IMAP_PORT", "USERNAME", "PASSWORD", "FOLDER", "FROM"):
            monkeypatch.delenv(f"SENT_COPY_{label}_{suffix}", raising=False)


def env(**overrides):
    value = {
        "BRIDGE_SENT_COPY_ENABLED": "true",
        "BRIDGE_CONV_MEMORY_ENABLED": "true",
        "SENT_COPY_INBOXES": "1:abs,3:actex",
        "SENT_COPY_ABS_USERNAME": "orders@example.com",
        "SENT_COPY_ABS_PASSWORD": SECRET,
        "SENT_COPY_ABS_FOLDER": "INBOX.Sent",
    }
    value.update(overrides)
    return {k: v for k, v in value.items() if v is not None}


def sent_payload(**updates):
    """A v4.16.1 Message#webhook_data after SendOnEmailService set source_id."""
    value = {
        "event": "message_updated",
        "id": 501,
        "message_type": "outgoing",
        "private": False,
        "content": "Your refund was issued today.",
        "content_type": "text",
        "content_attributes": {},
        "created_at": "2026-09-23T14:05:11.123Z",
        "source_id": "conversation/abc-uuid/messages/501@example.com",
        "sender": {"id": 4, "name": "Paula", "email": "paula@example.com", "type": "user"},
        "inbox": {"id": 1, "name": "Actuarial Bookstore"},
        "account": {"id": 1, "name": "Bookstore"},
        "conversation": {
            "id": 58,
            "inbox_id": 1,
            "channel": "Channel::Email",
            "additional_attributes": {"mail_subject": "Refund for order 1234"},
            "meta": {"sender": {"email": "Customer@Example.org", "type": "contact"}},
            "contact_inbox": {"source_id": "customer@example.org"},
        },
    }
    value.update(updates)
    return value


def thread_messages():
    return [
        {"id": 480, "message_type": 0, "content_attributes": {"email": {
            "message_id": "first@customer.example",
        }}},
        {"id": 490, "message_type": 1, "private": False},
        {"id": 495, "message_type": 0, "content_attributes": {"email": {
            "message_id": "second@customer.example",
            "references": ["first@customer.example", "<reply-1@example.com>"],
        }}},
        {"id": 499, "message_type": 1, "private": True},
        {"id": 501, "message_type": 1},
        # Newer than the reply: the mailer never saw it.
        {"id": 520, "message_type": 0, "content_attributes": {"email": {
            "message_id": "later@customer.example",
        }}},
    ]


def screened(**updates):
    sent, reason = sent_copy.screen_sent(sent_payload(**updates))
    assert sent is not None, reason
    return sent


# ── Screening ─────────────────────────────────────────────────────────────────


def test_delivered_outgoing_reply_is_screened_in():
    sent = screened()
    assert sent.message_id == 501
    assert sent.conversation_id == 58
    assert sent.inbox_id == 1
    assert sent.rfc822_message_id == "conversation/abc-uuid/messages/501@example.com"
    assert sent.to_emails == ("customer@example.org",)
    assert sent.customer_email == "customer@example.org"
    assert sent.created_at == datetime(2026, 9, 23, 14, 5, 11, 123000, tzinfo=timezone.utc)


@pytest.mark.parametrize("updates, reason", [
    ({"source_id": None, "event": "message_created"}, "not_sent_yet"),
    ({"source_id": ""}, "not_sent_yet"),
    ({"private": True}, "private_note"),
    ({"message_type": "incoming"}, "not_outgoing"),
    ({"message_type": 0}, "not_outgoing"),
    ({"message_type": "template"}, "not_outgoing"),
    ({"message_type": "activity"}, "not_outgoing"),
    ({"event": "conversation_updated"}, "not_sent_event"),
    ({"content": "", "attachments": []}, "empty_content"),
    ({"source_id": "has space@x"}, "not_sent_yet"),
])
def test_everything_that_did_not_reach_the_customer_is_screened_out(updates, reason):
    sent, found = sent_copy.screen_sent(sent_payload(**updates))
    assert sent is None
    assert found == reason


def test_non_email_channel_is_screened_out():
    payload = sent_payload()
    payload["conversation"]["channel"] = "Channel::WebWidget"
    assert sent_copy.screen_sent(payload) == (None, "not_email_channel")


def test_explicit_to_and_cc_win_over_the_contact():
    sent = screened(content_attributes={
        "to_emails": ["Other@Example.org"],
        "cc_emails": "a@example.org, b@example.org",
        "bcc_emails": ["audit@example.com"],
    })
    assert sent.to_emails == ("other@example.org",)
    assert sent.cc_emails == ("a@example.org", "b@example.org")
    assert sent.bcc_emails == ("audit@example.com",)


def test_angle_bracketed_source_id_is_normalized():
    assert screened(source_id="<abc@example.com>").rfc822_message_id == "abc@example.com"


# ── Threading and the RFC822 copy ─────────────────────────────────────────────


def test_threading_follows_the_mailer_rule():
    threading = sent_copy.rebuild_threading(screened(), thread_messages())
    assert threading.in_reply_to == "second@customer.example"
    assert threading.references == (
        "first@customer.example", "reply-1@example.com", "second@customer.example")
    assert threading.subject == "Re: Refund for order 1234"
    assert threading.source == "conversation"


def test_first_message_of_a_new_thread_keeps_its_subject():
    threading = sent_copy.rebuild_threading(screened(), [{"id": 501, "message_type": 1}])
    assert threading.subject == "Refund for order 1234"
    assert threading.in_reply_to == ""


def test_missing_mail_subject_uses_chatwoots_fallback():
    payload = sent_payload()
    payload["conversation"]["additional_attributes"] = {}
    sent, _ = sent_copy.screen_sent(payload)
    assert sent_copy.default_threading(sent).subject == "[#58] New messages on this conversation"


def test_copy_carries_chatwoots_ids_and_the_loop_guard_header():
    sent = screened(content_attributes={"cc_emails": ["cc@example.org"]})
    raw = sent_copy.build_copy(
        sent, sent_copy.rebuild_threading(sent, thread_messages()), "orders@example.com")
    assert b"\r\n" in raw
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert parsed["Message-ID"] == "<conversation/abc-uuid/messages/501@example.com>"
    assert parsed["In-Reply-To"] == "<second@customer.example>"
    assert parsed["References"].split() == [
        "<first@customer.example>", "<reply-1@example.com>", "<second@customer.example>"]
    assert parsed["X-Dewie-Desk-Copy"] == "501"
    assert parsed["To"] == "customer@example.org"
    assert parsed["Cc"] == "cc@example.org"
    assert parsed["Subject"] == "Re: Refund for order 1234"
    assert "orders@example.com" in parsed["From"]
    assert "Paula" in parsed["From"]
    assert parsed.get_content().strip() == "Your refund was issued today."


def test_header_injection_in_chatwoot_fields_is_flattened():
    payload = sent_payload(sender={"name": "Eve\r\nBcc: victim@example.org"})
    payload["conversation"]["additional_attributes"]["mail_subject"] = "Hi\r\nBcc: x@y.z"
    sent, _ = sent_copy.screen_sent(payload)
    raw = sent_copy.build_copy(sent, sent_copy.default_threading(sent), "orders@example.com")
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert parsed["Bcc"] is None
    assert "\n" not in parsed["Subject"]


def test_attachments_are_named_not_copied():
    sent = screened(content="", attachments=[{"file_name": "receipt.pdf"}])
    raw = sent_copy.build_copy(sent, sent_copy.default_threading(sent), "orders@example.com")
    assert b"receipt.pdf" in raw


# ── Mailbox configuration ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["1:abs,x:actex", "1:abs,1:actex", "1abs", "1:a-b"])
def test_malformed_inbox_map_refuses_every_inbox(raw):
    assert sent_copy.parse_inbox_map(raw) == {}


def test_mailbox_resolution():
    config, problem = sent_copy.mailbox_for_inbox(1, env())
    assert problem == ""
    assert (config.host, config.port, config.folder) == ("secure.emailsrvr.com", 993, "INBOX.Sent")
    assert SECRET not in repr(config)
    assert sent_copy.mailbox_for_inbox(3, env())[1] == "mailbox_credentials_missing"
    assert sent_copy.mailbox_for_inbox(9, env())[1] == "inbox_not_mapped"
    assert sent_copy.mailbox_for_inbox(1, env(SENT_COPY_ABS_FOLDER=None))[1] == (
        "mailbox_folder_missing")
    assert sent_copy.mailbox_for_inbox(1, env(SENT_COPY_INBOXES=None))[1] == (
        "inbox_map_not_configured")


def test_status_reports_readiness_without_secrets():
    report = sent_copy.status(env())
    assert report["copy_enabled"] and report["memory_enabled"]
    assert report["mailboxes"]["abs"]["ready"] is True
    assert report["mailboxes"]["actex"]["problem"] == "mailbox_credentials_missing"
    assert SECRET not in json.dumps(report)


def test_flags_default_off():
    assert sent_copy.copy_enabled({}) is False
    assert sent_copy.memory_enabled({}) is False


# ── APPEND ────────────────────────────────────────────────────────────────────


def test_append_is_the_only_mailbox_write_and_is_flagged_seen(tmp_path):
    connector = Connector()
    outcome = sent_copy.copy_to_sent_folder(
        screened(), store=DedupStore(tmp_path / "s.sqlite3"),
        client=FakeChatwoot(thread_messages()), connect=connector, environ=env())
    assert outcome == "appended"
    assert connector.opened == [("secure.emailsrvr.com", 993)]
    assert [call[0] for call in connector.imap.calls] == ["login", "append", "logout"]
    appended = connector.imap.appended[0]
    assert appended["folder"] == '"INBOX.Sent"'
    assert appended["flags"] == r"(\Seen)"
    assert appended["date"].startswith('"23-Sep-2026')
    assert b"X-Dewie-Desk-Copy: 501" in appended["raw"]


def test_append_only_wrapper_exposes_nothing_else():
    wrapper = sent_copy.AppendOnlyMailbox(FakeIMAP())
    for forbidden in ("select", "store", "expunge", "uid", "copy", "create", "delete", "send"):
        assert not hasattr(wrapper, forbidden)


def test_fake_imap_refuses_unlisted_commands():
    with pytest.raises(AssertionError):
        FakeIMAP().store("1", "+FLAGS", r"(\Deleted)")


def test_retry_restart_and_status_updates_do_not_append_twice(tmp_path):
    path = tmp_path / "state.sqlite3"
    connector = Connector()
    first = sent_copy.copy_to_sent_folder(
        screened(), store=DedupStore(path), client=FakeChatwoot(thread_messages()),
        connect=connector, environ=env())
    # Same message again: a webhook retry, a later delivered/read status update,
    # and a fresh process reading the same state file.
    again = sent_copy.copy_to_sent_folder(
        screened(), store=DedupStore(path), client=FakeChatwoot(thread_messages()),
        connect=connector, environ=env())
    assert (first, again) == ("appended", "duplicate")
    assert len(connector.imap.appended) == 1


def test_failed_append_releases_the_claim(tmp_path):
    store = DedupStore(tmp_path / "state.sqlite3")
    failing = Connector(FakeIMAP(append_status="NO"))
    assert sent_copy.copy_to_sent_folder(
        screened(), store=store, client=FakeChatwoot(), connect=failing,
        environ=env()) == "append_failed"
    assert failing.imap.calls[-1] == ("logout",)
    refused = Connector(fail=True)
    assert sent_copy.copy_to_sent_folder(
        screened(), store=store, client=FakeChatwoot(), connect=refused,
        environ=env()) == "append_failed"
    working = Connector()
    assert sent_copy.copy_to_sent_folder(
        screened(), store=store, client=FakeChatwoot(), connect=working,
        environ=env()) == "appended_unthreaded"
    assert len(working.imap.appended) == 1


def test_threading_lookup_failure_still_files_the_copy(tmp_path):
    connector = Connector()
    outcome = sent_copy.copy_to_sent_folder(
        screened(), store=DedupStore(tmp_path / "s.sqlite3"),
        client=FakeChatwoot(ok=False), connect=connector, environ=env())
    assert outcome == "appended_unthreaded"
    raw = connector.imap.appended[0]["raw"]
    assert b"In-Reply-To" not in raw
    assert b"Message-ID: <conversation/abc-uuid/messages/501@example.com>" in raw


def test_unmapped_inbox_touches_no_mailbox_and_claims_nothing(tmp_path):
    store = DedupStore(tmp_path / "s.sqlite3")
    connector = Connector()
    payload = sent_payload(inbox={"id": 3, "name": "ACTEX"})
    sent, _ = sent_copy.screen_sent(payload)
    assert sent_copy.copy_to_sent_folder(
        sent, store=store, client=FakeChatwoot(), connect=connector,
        environ=env()) == "mailbox_credentials_missing"
    assert connector.opened == []
    assert store.claim(sent_copy.copy_claim_key(sent)) is True


# ── conv_memory ───────────────────────────────────────────────────────────────


@pytest.fixture
def memory_engine():
    from dewie_brain.db.schema import conv_memory

    engine = sqlalchemy.create_engine("sqlite://")
    conv_memory.create(engine)
    return engine


def test_conv_memory_row_mirrors_the_sent_sync_shape(memory_engine):
    from dewie_brain.db.schema import conv_memory

    writer = ConvMemoryWriter(memory_engine)
    sent = screened()
    assert writer.write(sent, subject="Re: Refund for order 1234") is True
    assert writer.write(sent, subject="Re: Refund for order 1234") is False
    with memory_engine.connect() as conn:
        rows = conn.execute(sqlalchemy.select(conv_memory)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == session_id_for(sent) == "chatwoot:1:501"
    assert len(row["session_id"]) <= 64
    assert row["role"] == "assistant"
    assert row["agent_id"] == "chatwoot"
    assert row["customer_id"] == "customer@example.org"
    assert row["content"] == "Your refund was issued today."
    assert row["topic"] == "Re: Refund for order 1234"
    assert row["turn_index"] == 0
    assert row["created_at"].replace(tzinfo=timezone.utc) == sent.created_at


def test_memory_claim_dedups_and_releases_on_failure(tmp_path):
    store = DedupStore(tmp_path / "s.sqlite3")
    assert sent_copy.record_in_memory(
        screened(), store=store, writer=ListWriter(fail=True)) == "write_failed"
    writer = ListWriter()
    assert sent_copy.record_in_memory(screened(), store=store, writer=writer) == "written"
    assert sent_copy.record_in_memory(screened(), store=store, writer=writer) == "duplicate"
    assert writer.rows == [(501, "Re: Refund for order 1234")]


def test_destinations_are_independent(tmp_path):
    store = DedupStore(tmp_path / "s.sqlite3")
    connector = Connector()
    outcome = sent_copy.process_sent(
        screened(), store=store, client=FakeChatwoot(thread_messages()),
        writer_factory=lambda: ListWriter(fail=True), connect=connector, environ=env())
    assert outcome == {"sent_copy": "appended", "conv_memory": "write_failed"}
    only_memory = sent_copy.process_sent(
        screened(id=777), store=store, client=FakeChatwoot(),
        writer_factory=ListWriter, connect=connector,
        environ=env(BRIDGE_SENT_COPY_ENABLED=None))
    assert only_memory == {"conv_memory": "written"}
    assert len(connector.imap.appended) == 1


# ── Webhook route ─────────────────────────────────────────────────────────────


def post(client, value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    return client.post("/chatwoot/webhook", content=raw,
                       headers=webhook_auth.sign_headers(raw, "secret"))


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    processed = []
    monkeypatch.setattr(main, "process_sent_message", processed.append)
    return TestClient(main.app), processed


def test_delivered_reply_is_scheduled_when_enabled(monkeypatch, webhook):
    client, processed = webhook
    monkeypatch.setenv("BRIDGE_SENT_COPY_ENABLED", "true")
    response = post(client, sent_payload())
    assert response.json() == {"accepted": True, "action": "sent_copy_scheduled",
                               "conversation": 58, "message": 501}
    assert [sent.message_id for sent in processed] == [501]


def test_delivered_reply_is_ignored_while_flags_are_off(webhook):
    client, processed = webhook
    assert post(client, sent_payload()).json() == {
        "accepted": False, "reason": "sent_copy_disabled"}
    assert processed == []


def test_creation_before_send_changes_nothing(monkeypatch, webhook):
    client, processed = webhook
    monkeypatch.setenv("BRIDGE_SENT_COPY_ENABLED", "true")
    response = post(client, sent_payload(event="message_created", source_id=None))
    assert response.json() == {"accepted": False, "reason": "not_incoming"}
    assert processed == []


@pytest.mark.parametrize("updates, reason", [
    ({"private": True}, "private_note"),
    ({"message_type": "incoming"}, "not_outgoing"),
    ({"source_id": None}, "not_sent_yet"),
])
def test_other_message_updates_are_filtered(monkeypatch, webhook, updates, reason):
    client, processed = webhook
    monkeypatch.setenv("BRIDGE_SENT_COPY_ENABLED", "true")
    assert post(client, sent_payload(**updates)).json() == {"accepted": False, "reason": reason}
    assert processed == []


def test_health_reports_sent_copy_without_secrets(monkeypatch):
    for key, value in env().items():
        monkeypatch.setenv(key, value)
    body = TestClient(main.app).get("/health").json()
    assert body["sent_copy"]["mailboxes"]["abs"]["ready"] is True
    assert SECRET not in json.dumps(body)

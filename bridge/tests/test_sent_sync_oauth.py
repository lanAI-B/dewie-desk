"""Sync-only M365 mailboxes (SENT_SYNC_INBOXES): Chatwoot's token, never sent copy.

No network and no Docker: the token source and IMAP fetch are fakes.
"""

from datetime import datetime, timedelta, timezone

import pytest

from parser import parse_message_created
from state import DedupStore
import sent_copy
import sent_sync
from test_sent_sync import FakeClient, inbound_payload, sent_bytes

AL = "support@actuariallearning.com"
TOKEN = "al-access-token-value"
NOW = datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("SENT_COPY_INBOXES", "SENT_SYNC_INBOXES", "BRIDGE_DRY_RUN"):
        monkeypatch.delenv(name, raising=False)
    for label in ("AL", "ABS"):
        for suffix in ("AUTH", "USERNAME", "FOLDER", "IMAP_HOST", "IMAP_PORT"):
            monkeypatch.delenv(f"SENT_SYNC_{label}_{suffix}", raising=False)
        for suffix in ("USERNAME", "PASSWORD", "FOLDER"):
            monkeypatch.delenv(f"SENT_COPY_{label}_{suffix}", raising=False)


def configure_al(monkeypatch, **overrides):
    values = {"AUTH": "chatwoot_oauth", "USERNAME": AL, "FOLDER": "Sent Items", **overrides}
    monkeypatch.setenv("SENT_SYNC_INBOXES", "6:al")
    for suffix, value in values.items():
        if value is None:
            monkeypatch.delenv(f"SENT_SYNC_AL_{suffix}", raising=False)
        else:
            monkeypatch.setenv(f"SENT_SYNC_AL_{suffix}", value)


def token(email=AL, expires=NOW + timedelta(minutes=50)):
    return sent_sync.ChatwootToken(email=email, access_token=TOKEN, expires_on=expires)


class Tokens:
    def __init__(self, value=None, raises=None):
        self.value, self.raises, self.calls = value, raises, []

    def __call__(self, inbox_id):
        self.calls.append(inbox_id)
        if self.raises:
            raise self.raises
        return self.value


class Fetch:
    def __init__(self, replies=()):
        self.replies, self.calls = list(replies), []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.replies)


@pytest.fixture
def store(tmp_path):
    value = DedupStore(tmp_path / "bridge.sqlite3")
    value.record_inbound(parse_message_created(inbound_payload()))
    return value


def clement_reply():
    return sent_sync.parse_sent_email(sent_bytes(message_id="<CLEMENT-1@namprd.outlook.com>"))


def run(store, client, fetch, tokens, **kw):
    return sent_sync.run_sent_sync(store=store, client=client, dry_run=kw.pop("dry_run", False),
                                   fetch=fetch, token_source=tokens, **kw)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    original = sent_sync.sync_mailbox

    def at_now(*args, **kwargs):
        kwargs.setdefault("now", NOW)
        return original(*args, **kwargs)

    monkeypatch.setattr(sent_sync, "sync_mailbox", at_now)


# ── Configuration ─────────────────────────────────────────────────────────────


def test_a_sync_only_mailbox_is_configured_without_a_password(monkeypatch):
    configure_al(monkeypatch)
    [(label, config, problem)], map_problem = sent_sync.configured_mailboxes()
    assert (label, problem, map_problem) == ("al", "", "")
    assert (config.inbox_id, config.host, config.port, config.folder) == (
        6, "outlook.office365.com", 993, "Sent Items")
    assert config.auth == "chatwoot_oauth"


def test_sent_copy_never_sees_a_sync_only_mailbox(monkeypatch):
    configure_al(monkeypatch)
    assert sent_copy.mailbox_for_inbox(6) == (None, "inbox_map_not_configured")
    assert sent_copy.status()["mailboxes"] == {}


@pytest.mark.parametrize("override, problem", [
    ({"AUTH": None}, "auth_unsupported:missing"),
    ({"AUTH": "password"}, "auth_unsupported:password"),
    ({"USERNAME": None}, "mailbox_username_missing"),
    ({"FOLDER": None}, "mailbox_folder_missing"),
])
def test_an_incomplete_sync_only_mailbox_is_reported_not_guessed(monkeypatch, override, problem):
    configure_al(monkeypatch, **override)
    [(_, config, found)], _ = sent_sync.configured_mailboxes()
    assert (config, found) == (None, problem)


def test_a_label_in_both_maps_is_refused(monkeypatch):
    configure_al(monkeypatch)
    monkeypatch.setenv("SENT_COPY_INBOXES", "1:al")
    monkeypatch.setenv("SENT_COPY_AL_USERNAME", "x@example.com")
    monkeypatch.setenv("SENT_COPY_AL_PASSWORD", "pw")
    monkeypatch.setenv("SENT_COPY_AL_FOLDER", "INBOX.Sent")
    found, _ = sent_sync.configured_mailboxes()
    assert [(label, problem) for label, _, problem in found] == [("al", ""), ("al", "label_in_both_maps")]


# ── One pass ──────────────────────────────────────────────────────────────────


def test_the_mailbox_is_read_with_chatwoots_token_and_posts_a_note(monkeypatch, store):
    configure_al(monkeypatch)
    fetch, tokens, client = Fetch([clement_reply()]), Tokens(token()), FakeClient()
    report = run(store, client, fetch, tokens)
    assert tokens.calls == [6]
    [call] = fetch.calls
    assert call["access_token"] == TOKEN and call["password"] == ""
    assert (call["host"], call["folder"], call["username"]) == ("outlook.office365.com", "Sent Items", AL)
    assert report.mailboxes["al"].counts["private_notes_posted"] == 1
    assert [c[0] for c in client.calls] == ["post_private_note"]


@pytest.mark.parametrize("value, problem", [
    (None, "token_missing"),
    (token(email="someone-else@actuariallearning.com"), "token_mailbox_mismatch"),
    (token(expires=NOW + timedelta(minutes=1)), "token_stale"),
    (token(expires=NOW - timedelta(hours=1)), "token_stale"),
])
def test_no_usable_token_skips_the_pass_without_reading(monkeypatch, store, value, problem):
    configure_al(monkeypatch)
    fetch, client = Fetch([clement_reply()]), FakeClient()
    report = run(store, client, fetch, Tokens(value))
    assert report.mailboxes["al"].problem == problem
    assert fetch.calls == [] and client.calls == []


def test_a_failed_token_read_reports_the_type_only(monkeypatch, store):
    configure_al(monkeypatch)
    report = run(store, FakeClient(), Fetch(), Tokens(raises=RuntimeError(f"psql said {TOKEN}")))
    assert report.mailboxes["al"].problem == "token_unavailable:RuntimeError"
    assert TOKEN not in str(report.as_dict())


def test_the_token_never_reaches_health(monkeypatch):
    configure_al(monkeypatch)
    status = sent_sync.sent_sync_status()
    assert status["mailboxes"]["al"] == {"ready": True, "problem": None, "folder": "Sent Items"}
    assert TOKEN not in str(status)
    assert TOKEN not in repr(token())


def test_a_password_mailbox_still_never_asks_for_a_token(monkeypatch, store):
    monkeypatch.setenv("SENT_COPY_INBOXES", "1:abs")
    monkeypatch.setenv("SENT_COPY_ABS_USERNAME", "service@actuarialbookstore.com")
    monkeypatch.setenv("SENT_COPY_ABS_PASSWORD", "pw")
    monkeypatch.setenv("SENT_COPY_ABS_FOLDER", "INBOX.Sent")
    fetch, tokens = Fetch(), Tokens(token())
    run(store, FakeClient(), fetch, tokens)
    assert tokens.calls == []
    assert "access_token" not in fetch.calls[0]


# ── Chatwoot's own replies ────────────────────────────────────────────────────


def test_a_reply_chatwoot_sent_is_never_posted_back(store):
    sent = sent_sync.parse_sent_email(sent_bytes(
        message_id="<conversation/411278c0-f386-4156-a0ad-5c7091703392/messages/11833@actuariallearning.com>"))
    client = FakeClient()
    report = sent_sync.sync_replies([sent], store=store, client=client, dry_run=False)
    assert report.counts["chatwoot_sent_skipped"] == 1
    assert client.calls == []


def test_an_outlook_message_id_that_mentions_conversation_is_still_synced(store):
    sent = sent_sync.parse_sent_email(sent_bytes(message_id="<conversation-notes-1@outlook.com>"))
    report = sent_sync.sync_replies([sent], store=store, client=FakeClient(), dry_run=False)
    assert report.counts["private_notes_posted"] == 1

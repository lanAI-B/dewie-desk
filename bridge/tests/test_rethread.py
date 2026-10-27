from types import SimpleNamespace

import pytest

import rethread
from chatwoot import ConversationResult
from state import DedupStore
from thread import build_draft_thread, fetch_chain


def inbound(conversation_id, message_id, *, subject="Seminar keys (Order #1280884)",
            in_reply_to="", references=(), from_email="quarter0118@outlook.com", msgid=""):
    return SimpleNamespace(
        conversation_id=conversation_id, message_id=message_id, account_id=1,
        subject=subject, from_email=from_email, to_emails=["service@actuarialbookstore.com"],
        in_reply_to=in_reply_to, references=list(references), rfc822_message_id=msgid,
        body="", attachments=[], sender_type="contact", message_type="incoming",
        event="message_created", is_private=False,
    )


@pytest.fixture
def store(tmp_path):
    return DedupStore(tmp_path / "state.sqlite3")


def record(store, message):
    # record_inbound needs a dataclass; the thread map is what rethreading reads.
    store.record_thread_keys(message)
    with store._connect() as c:
        c.execute("INSERT INTO inbound_message(account_id, conversation_id, message_id, "
                  "normalized_json, observed_at) VALUES (1, ?, ?, '{}', 'now')",
                  (message.conversation_id, message.message_id))


def test_a_reply_to_mail_we_sent_from_webmail_finds_its_conversation(store):
    record(store, inbound(1536, 100, msgid="cust-1@outlook.com"))
    # Sent sync filed our webmail reply into 1536 and now remembers its id.
    assert store.record_message_id("<ours-1@apps.rackspace.com>", 1536)
    reply = inbound(1882, 200, subject="Re: Seminar keys (Order #1280884)",
                    in_reply_to="<ours-1@apps.rackspace.com>")
    assert rethread.find_parent(store, reply) == rethread.Parent(1536, "message_id")


def test_a_reply_across_inboxes_finds_its_conversation_by_references(store):
    record(store, inbound(1984, 100, msgid="store-conf@dw1mdwk0001ys"))
    reply = inbound(1985, 200, in_reply_to="<unknown@x>",
                    references=["<store-conf@dw1mdwk0001ys>", "<unknown@x>"])
    assert rethread.find_parent(store, reply) == rethread.Parent(1984, "message_id")


def test_subject_and_sender_is_the_fallback_only_for_replies(store):
    record(store, inbound(1937, 100, subject="Accounting and Finance VEE",
                          from_email="connallyjoseph@gmail.com"))
    reply = inbound(1956, 200, subject="Re: Accounting and Finance VEE",
                    from_email="connallyjoseph@gmail.com", in_reply_to="<nothing@we.hold>")
    assert rethread.find_parent(store, reply) == rethread.Parent(1937, "subject")
    fresh = inbound(1957, 300, subject="Accounting and Finance VEE",
                    from_email="connallyjoseph@gmail.com")
    assert rethread.find_parent(store, fresh) is None


def test_only_a_conversations_first_email_and_only_older_parents(store):
    record(store, inbound(1536, 100, msgid="a@x"))
    record(store, inbound(1882, 200))
    later = inbound(1882, 300, in_reply_to="<a@x>")
    assert rethread.find_parent(store, later) is None  # 1882 already has an email
    record(store, inbound(2000, 400, msgid="b@x"))
    backwards = inbound(1999, 500, in_reply_to="<b@x>")
    assert rethread.find_parent(store, backwards) is None


class FoldClient:
    def __init__(self, parent_status="open"):
        self.notes, self.resolved, self.parent_status = [], [], parent_status

    def post_private_note(self, conversation_id, content):
        self.notes.append((conversation_id, content))
        return SimpleNamespace(ok=True)

    def conversation_details(self, conversation_id):
        return {"id": conversation_id, "status": self.parent_status}

    def resolve_conversation(self, conversation_id):
        self.resolved.append(conversation_id)
        return SimpleNamespace(ok=True)


def test_fold_links_both_ways_and_resolves_an_open_parent_once(store, monkeypatch):
    monkeypatch.setenv("BRIDGE_RETHREAD", "on")
    client = FoldClient("open")
    child = inbound(2072, 900)
    assert rethread.handle(client, store, child, rethread.Parent(1971, "message_id")) \
        == "folded_parent_resolved"
    assert store.parent_of(2072) == 1971
    assert [c for c, _ in client.notes] == [2072, 1971]
    assert client.notes[0][1].startswith(rethread.FOLD_NOTE_PREFIX)
    assert client.resolved == [1971]
    assert rethread.handle(client, store, child, rethread.Parent(1971, "message_id")) \
        == "already_linked"
    assert client.resolved == [1971]


def test_a_resolved_parent_is_left_alone(store, monkeypatch):
    monkeypatch.setenv("BRIDGE_RETHREAD", "on")
    client = FoldClient("resolved")
    assert rethread.handle(client, store, inbound(2072, 900),
                           rethread.Parent(1971, "subject")) == "folded"
    assert client.resolved == [] and [c for c, _ in client.notes] == [2072]


def test_our_own_mail_in_the_other_inbox_is_linked_but_nothing_is_resolved(store, monkeypatch):
    monkeypatch.setenv("BRIDGE_RETHREAD", "on")
    client = FoldClient("open")
    ours = inbound(2023, 900, from_email="service@actuarialbookstore.com")
    assert rethread.handle(client, store, ours, rethread.Parent(2017, "message_id")) \
        == "linked_internal"
    assert client.resolved == [] and [c for c, _ in client.notes] == [2023, 2017]


def test_dry_run_is_the_default_and_changes_nothing(store, monkeypatch):
    monkeypatch.delenv("BRIDGE_RETHREAD", raising=False)
    client = FoldClient()
    assert rethread.handle(client, store, inbound(2072, 900),
                           rethread.Parent(1971, "message_id")) == "dry_run"
    assert client.notes == [] and store.parent_of(2072) is None


def msg(i, kind, content, name="Customer", private=False):
    return {"id": i, "message_type": {"in": 0, "out": 1}[kind], "private": private,
            "content": content, "sender": {"name": name}}


def test_dewie_reads_the_whole_chain_as_one_thread(store):
    store.link_conversations(1882, 1536, "message_id")
    store.link_conversations(2072, 1882, "message_id")
    pages = {
        1536: [msg(1, "in", "I need my seminar keys."), msg(2, "out", "Sent them.", "Paula")],
        1882: [msg(5, "in", "They don't work."),
               msg(6, "out", "**Thread continues** from #1536", private=True),
               msg(7, "out", "@dewie old steering, not for now", "Lana", private=True)],
    }

    class Client:
        def get_conversation(self, conversation_id, before=None):
            rows = [] if before else pages[conversation_id]
            return ConversationResult(True, tuple(rows), {}, 200, "fetched")

    current = [msg(9, "in", "Still waiting!"), msg(10, "out", "@dewie apologise", "Lana", True)]
    chain = fetch_chain(Client(), store, 2072, current)
    t = build_draft_thread(chain, customer_message_id=9)
    assert t.thread.index("seminar keys") < t.thread.index("don't work") < t.thread.index("Still")
    assert "in earlier conversation #1536" in t.thread
    assert "Thread continues" not in t.thread
    assert t.team_notes == "Lana: apologise"  # the earlier conversation's note is history

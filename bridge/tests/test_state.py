import json
import sqlite3

import pytest

from state import DedupStore
from parser import ParsedMessage


def test_claim_survives_new_store_instance(tmp_path):
    path = tmp_path / "bridge.sqlite3"

    assert DedupStore(path).claim("account:1:message:42")
    assert not DedupStore(path).claim("account:1:message:42")
    assert DedupStore(path).claim("account:1:message:43")


def test_released_failed_action_can_be_claimed_by_fresh_command(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")

    assert store.claim("conversation:7:message:42:action:draft")
    store.release("conversation:7:message:42:action:draft")
    assert store.claim("conversation:7:message:42:action:draft")


def test_normalized_inbound_message_is_persisted_once(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    message = ParsedMessage(
        event="message_created",
        account_id=1,
        conversation_id=7,
        message_id=42,
        from_email="person@example.com",
        subject="Question",
        body="Please help",
        message_type="incoming",
        should_process=True,
    )

    assert DedupStore(path).record_inbound(message)
    assert not DedupStore(path).record_inbound(message)
    assert DedupStore(path).latest_inbound_id(7) == 42
    assert DedupStore(path).latest_inbound_id(8) is None
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT normalized_json FROM inbound_message WHERE account_id=1 AND message_id=42"
        ).fetchone()[0]
    assert json.loads(stored)["body"] == "Please help"


def _begin(store, key="refund:123", conversation_id=45, digest="a" * 64, actor="lana"):
    return store.begin_outbound(key, conversation_id, digest, actor, "test")


def test_outbound_claim_is_durable_before_any_result(tmp_path):
    path = tmp_path / "bridge.sqlite3"

    verdict, record = _begin(DedupStore(path))
    assert verdict == "claimed"
    assert record.status == "pending"
    assert record.attempts == 1

    verdict, replay = _begin(DedupStore(path))
    assert verdict == "replay"
    assert replay.status == "pending"
    assert replay.attempts == 1


def test_outbound_accepted_is_terminal_and_replayed(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")
    _begin(store)

    finished = store.finish_outbound(
        "refund:123", "accepted", chatwoot_message_id=501, http_status=200, detail="created"
    )

    assert finished.status == "accepted"
    assert finished.chatwoot_message_id == 501
    verdict, replay = _begin(DedupStore(store.path))
    assert verdict == "replay"
    assert replay.status == "accepted"
    assert replay.chatwoot_message_id == 501


def test_outbound_unknown_is_durable_and_never_reclaimed(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")
    _begin(store)
    store.finish_outbound("refund:123", "unknown", detail="ReadTimeout")

    verdict, replay = _begin(DedupStore(store.path))

    assert verdict == "replay"
    assert replay.status == "unknown"
    assert replay.attempts == 1


def test_outbound_definitive_rejection_can_be_reattempted_with_same_key(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")
    _begin(store, actor="first")
    store.finish_outbound("refund:123", "rejected", http_status=404, detail="HTTP 404")

    verdict, record = _begin(store, actor="second")

    assert verdict == "claimed"
    assert record.status == "pending"
    assert record.attempts == 2
    assert record.http_status is None
    history = store.outbound_attempts("refund:123")
    assert [(row["attempt"], row["actor"], row["status"]) for row in history] == [
        (1, "first", "rejected"),
        (2, "second", "pending"),
    ]


def test_outbound_key_reuse_with_different_request_conflicts(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")
    _begin(store)

    assert _begin(store, conversation_id=46)[0] == "conflict"
    assert _begin(store, digest="b" * 64)[0] == "conflict"
    store.finish_outbound("refund:123", "rejected", http_status=422, detail="bad")
    assert _begin(store, digest="b" * 64)[0] == "conflict"
    assert store.get_outbound("refund:123").attempts == 1


def test_concurrent_outbound_claims_from_separate_stores_yield_one_attempt(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "bridge.sqlite3"
    DedupStore(path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        verdicts = list(pool.map(lambda _: _begin(DedupStore(path))[0], range(16)))

    assert verdicts.count("claimed") == 1
    assert verdicts.count("replay") == 15
    assert len(DedupStore(path).outbound_attempts("refund:123")) == 1


def test_outbound_finish_only_moves_pending_records(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")
    _begin(store)
    store.finish_outbound("refund:123", "accepted", chatwoot_message_id=501, http_status=200)

    with pytest.raises(ValueError):
        store.finish_outbound("refund:123", "unknown", detail="late")
    with pytest.raises(ValueError):
        store.finish_outbound("refund:999", "accepted")
    _begin(store, key="refund:124")
    with pytest.raises(ValueError):
        store.finish_outbound("refund:124", "pending")
    assert store.get_outbound("refund:123").status == "accepted"
    assert store.get_outbound("refund:124").status == "pending"


def test_connections_are_closed_not_just_committed(tmp_path, monkeypatch):
    """Review #16: `with sqlite3.connect()` commits but never closes."""
    import sqlite3 as real_sqlite
    import state

    opened = []
    real_connect = real_sqlite.connect

    def tracking_connect(*a, **k):
        conn = real_connect(*a, **k)
        opened.append(conn)
        return conn

    store = state.DedupStore(tmp_path / "s.sqlite3")
    monkeypatch.setattr(state.sqlite3, "connect", tracking_connect)
    store.get_outbound("refund:1")
    store.claim("some-key")
    assert opened
    for conn in opened:
        with pytest.raises(real_sqlite.ProgrammingError):
            conn.execute("SELECT 1")
def inbound(**updates):
    value = dict(
        event="message_created",
        account_id=1,
        conversation_id=7,
        message_id=42,
        from_email="person@example.com",
        to_emails=["support@actexlearning.com"],
        subject="Access question",
        body="Please help",
        message_type="incoming",
        should_process=True,
    )
    value.update(updates)
    return ParsedMessage(**value)


def test_a_message_id_belongs_to_the_first_conversation_that_claimed_it(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")

    store.record_inbound(inbound(rfc822_message_id="<abc@mail.example>"))
    store.record_inbound(inbound(
        conversation_id=9, message_id=43, rfc822_message_id="<abc@mail.example>",
    ))

    assert store.resolve_thread(["msgid:abc@mail.example"]) == 7


def test_a_subject_key_follows_the_newest_conversation(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")

    store.record_inbound(inbound(rfc822_message_id="<one@mail.example>"))
    store.record_inbound(inbound(
        conversation_id=9, message_id=43, rfc822_message_id="<two@mail.example>",
    ))

    assert store.resolve_thread(["thread:access question|person@example.com"]) == 9
    assert store.resolve_thread(["msgid:one@mail.example"]) == 7
    assert store.resolve_thread(["msgid:two@mail.example"]) == 9


def test_referenced_ids_map_to_the_conversation_that_quoted_them(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")

    store.record_inbound(inbound(
        rfc822_message_id="<reply@mail.example>",
        in_reply_to="<ours@desk.example>",
        references=["<first@mail.example>"],
    ))

    assert store.resolve_thread(["msgid:ours@desk.example"]) == 7
    assert store.resolve_thread(["msgid:first@mail.example"]) == 7
    assert store.resolve_thread(["msgid:nothing@mail.example"]) is None


def test_a_message_without_thread_headers_still_records_a_subject_key(tmp_path):
    store = DedupStore(tmp_path / "bridge.sqlite3")

    assert store.record_thread_keys(inbound()) >= 1
    assert store.resolve_thread(["thread:access question|support@actexlearning.com"]) == 7

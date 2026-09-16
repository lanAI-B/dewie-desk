import json
import sqlite3

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

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

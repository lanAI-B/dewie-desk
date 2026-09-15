import json

from fastapi.testclient import TestClient

import main
import webhook_auth


def payload(**updates):
    value = {
        "event": "message_created",
        "id": 42,
        "message_type": "incoming",
        "private": False,
        "content": "Please help.",
        "content_attributes": {"email": {"subject": "Question"}},
        "sender": {"email": "person@example.com"},
        "conversation": {"id": 7, "messages": [{"sender_type": "Contact"}]},
        "account": {"id": 1},
    }
    value.update(updates)
    return value


class Store:
    def __init__(self):
        self.claims = set()
        self.keys = []
        self.messages = set()

    def claim(self, key):
        self.keys.append(key)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    def record_inbound(self, message):
        key = (message.account_id, message.conversation_id, message.message_id)
        if key in self.messages:
            return False
        self.messages.add(key)
        return True

    def latest_inbound_id(self, conversation_id):
        ids = [message_id for _, found, message_id in self.messages if found == conversation_id]
        return max(ids) if ids else None


def signed_post(client, value, secret="secret"):
    raw = json.dumps(value, separators=(",", ":")).encode()
    headers = webhook_auth.sign_headers(raw, secret)
    return client.post("/chatwoot/webhook", content=raw, headers=headers)


def label_update(previous=None, current=None, **updates):
    value = {
        "event": "conversation_updated",
        "id": 7,
        "account": {"id": 1},
        "labels": current or [],
        "changed_attributes": [{
            "label_list": {
                "previous_value": previous or [],
                "current_value": current or [],
            }
        }],
        "timestamp": 100,
    }
    value.update(updates)
    return value


def test_verified_inbound_message_is_persisted_without_drafting(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    store = Store()
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(
        main,
        "process_message",
        lambda message: (_ for _ in ()).throw(AssertionError("message event must not draft")),
    )
    monkeypatch.setattr(
        main,
        "classifier_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("message event must not classify")),
    )

    response = signed_post(TestClient(main.app), payload())

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["action"] == "recorded"
    assert store.messages == {(1, 7, 42)}
    assert store.keys == []


def test_message_webhook_retry_is_deduplicated(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    store = Store()
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    client = TestClient(main.app)

    assert signed_post(client, payload()).json()["accepted"] is True
    assert signed_post(client, payload()).json() == {
        "accepted": False,
        "reason": "duplicate_message",
    }


def test_bad_signature_is_rejected_before_claim(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    store = Store()
    monkeypatch.setattr(main, "dedup_store", lambda: store)

    response = TestClient(main.app).post(
        "/chatwoot/webhook",
        content=b"{}",
        headers={
            "x-chatwoot-timestamp": "1",
            "x-chatwoot-signature": "sha256=bad",
        },
    )

    assert response.status_code == 401
    assert store.keys == []


def test_transport_filter_and_duplicate_do_not_queue(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    processed = []
    monkeypatch.setattr(main, "process_message", processed.append)

    filtered_store = Store()
    monkeypatch.setattr(main, "dedup_store", lambda: filtered_store)
    filtered = signed_post(TestClient(main.app), payload(message_type="outgoing"))
    assert filtered.json() == {"accepted": False, "reason": "not_incoming"}
    assert filtered_store.keys == []

    assert processed == []


def test_unrelated_conversation_update_does_not_request_draft(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    store = Store()
    queued = []
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "process_label_command", lambda *args: queued.append(args))

    response = signed_post(
        TestClient(main.app),
        label_update(previous=["support"], current=["support"], status="resolved"),
    )

    assert response.json() == {"accepted": False, "reason": "no_draft_label_added"}
    assert queued == []


def test_only_label_add_queues_command_and_retry_is_deduplicated(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    store = Store()
    queued = []
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "process_label_command", lambda *args: queued.append(args))
    client = TestClient(main.app)
    event = label_update(previous=["support"], current=["support", "dewie-draft"])

    signed_post(client, payload())
    first = signed_post(client, event)
    retry = signed_post(client, event)

    assert first.json()["action"] == "draft_requested"
    assert queued == [(7, 1, 42)]
    assert retry.json() == {"accepted": False, "reason": "duplicate_label_command"}


def test_label_removal_is_not_a_new_command(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    queued = []
    monkeypatch.setattr(main, "process_label_command", lambda *args: queued.append(args))

    response = signed_post(
        TestClient(main.app),
        label_update(previous=["dewie-draft"], current=[]),
    )

    assert response.json() == {"accepted": False, "reason": "no_draft_label_added"}
    assert queued == []


def test_label_command_fails_closed_without_recorded_inbound(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    store = Store()
    queued = []
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "process_label_command", lambda *args: queued.append(args))

    response = signed_post(
        TestClient(main.app),
        label_update(previous=[], current=["dewie-draft"]),
    )

    assert response.json() == {"accepted": False, "reason": "no_recorded_inbound"}
    assert queued == []

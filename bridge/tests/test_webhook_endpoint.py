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
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.keys = []

    def claim(self, key):
        self.keys.append(key)
        return self.accepted


def signed_post(client, value, secret="secret"):
    raw = json.dumps(value, separators=(",", ":")).encode()
    headers = webhook_auth.sign_headers(raw, secret)
    return client.post("/chatwoot/webhook", content=raw, headers=headers)


def test_verified_inbound_message_is_claimed_and_queued(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    store = Store()
    processed = []
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "process_message", processed.append)

    response = signed_post(TestClient(main.app), payload())

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert store.keys == ["account:1:message:42"]
    assert len(processed) == 1


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

    duplicate_store = Store(accepted=False)
    monkeypatch.setattr(main, "dedup_store", lambda: duplicate_store)
    duplicate = signed_post(TestClient(main.app), payload())
    assert duplicate.json() == {"accepted": False, "reason": "duplicate_message"}
    assert processed == []

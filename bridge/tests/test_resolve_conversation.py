"""Find-or-create conversation endpoint: nobody looks up a conversation id by hand.

A fake Chatwoot client stands in for the API; the conftest network guard fails
any test that reaches a real socket.
"""

from __future__ import annotations

import pytest
import requests
from fastapi.testclient import TestClient

import chatwoot
import main
from chatwoot import ChatwootClient, ChatwootError

ROUTE = "/internal/chatwoot/resolve-conversation"
TOKEN = "t" * 40
INBOX = 1
EMAIL = "ann.example@example.com"


def body(**updates):
    value = {"email": EMAIL, "subject": "Refund Update: Order #1113621", "name": "Ann Example",
             "actor": "discord:1", "source": "dewieops-refund-button"}
    value.update(updates)
    return value


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


class FakeChatwoot:
    def __init__(self, contacts=(), conversations=(), details=None, fail=None):
        self.contacts = list(contacts)
        self.conversations = list(conversations)
        self.details = details or {}
        self.fail = fail
        self.calls = []
        self.next_conversation = 900

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.fail == name:
            raise ChatwootError("http_500")

    def find_contacts_by_email(self, email):
        self._call("find_contacts_by_email", email)
        return self.contacts

    def contact_conversations(self, contact_id):
        self._call("contact_conversations", contact_id)
        return self.conversations

    def create_contact(self, inbox_id, email, name):
        self._call("create_contact", inbox_id, email, name)
        return {"id": 77}

    def contact_source_id(self, contact_id, inbox_id, email):
        self._call("contact_source_id", contact_id, inbox_id, email)
        return email

    def create_conversation(self, contact_id, inbox_id, source_id, subject):
        self._call("create_conversation", contact_id, inbox_id, source_id, subject)
        self.next_conversation += 1
        return self.next_conversation

    def conversation_details(self, conversation_id):
        self._call("conversation_details", conversation_id)
        return self.details.get(conversation_id,
                                {"inbox_id": INBOX, "meta": {"channel": "Channel::Email", "sender": {"email": EMAIL}}})

    def post_public_outgoing(self, *a, **k):
        raise AssertionError("resolving a conversation must never post a message")

    post_private_note = post_public_outgoing


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", TOKEN)
    monkeypatch.setenv("BRIDGE_OUTBOUND_INBOX_ID", str(INBOX))

    def install(fake):
        monkeypatch.setattr(main, "chatwoot_client", lambda: fake)
        return fake

    return TestClient(main.app), install


def _conv(cid, subject="Refund Update: Order #1113621", inbox=INBOX, at=0):
    return {"id": cid, "inbox_id": inbox, "last_activity_at": at,
            "additional_attributes": {"mail_subject": subject}}


def test_the_notices_own_thread_is_reused(client):
    http, install = client
    fake = install(FakeChatwoot(
        contacts=[{"id": 2, "email": EMAIL}],
        conversations=[
            _conv(5, at=100),
            _conv(9, at=300),
            _conv(12, inbox=99, at=999),                           # other inbox: never chosen
            _conv(14, subject="Where is my book?", at=5000),       # unrelated thread: never chosen
        ]))
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 200
    assert r.json() == {"status": "found", "conversation_id": 9, "contact_id": 2,
                        "contact_email": EMAIL, "inbox_id": INBOX}
    assert not any(c[0].startswith("create") for c in fake.calls)


def test_an_unrelated_recent_thread_is_never_used(client):
    """Lana, 2026-09-22: a notice gets its own thread, never the customer's latest one."""
    http, install = client
    fake = install(FakeChatwoot(contacts=[{"id": 2, "email": EMAIL}],
                                conversations=[_conv(9, subject="get ready for RET 101", at=999)]))
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 200 and r.json()["status"] == "created"
    assert r.json()["conversation_id"] != 9


def test_subject_match_ignores_spacing_but_not_the_order_number(client):
    http, install = client
    install(FakeChatwoot(contacts=[{"id": 2, "email": EMAIL}], conversations=[
        _conv(7, subject="Refund Update:  Order #1113621 "), _conv(8, subject="Refund Update: Order #1113622")]))
    assert http.post(ROUTE, json=body(), headers=auth()).json()["conversation_id"] == 7


def test_contact_without_conversation_gets_a_new_empty_one(client):
    http, install = client
    fake = install(FakeChatwoot(contacts=[{"id": 2, "email": EMAIL}],
                                conversations=[_conv(12, inbox=99)]))
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 200 and r.json()["status"] == "created"
    assert ("create_conversation", 2, INBOX, EMAIL, "Refund Update: Order #1113621") in fake.calls
    assert not any(c[0] == "create_contact" for c in fake.calls)


def test_unknown_customer_gets_a_contact_and_a_conversation(client):
    http, install = client
    fake = install(FakeChatwoot())
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 200
    assert r.json()["status"] == "created" and r.json()["contact_id"] == 77
    assert ("create_contact", INBOX, EMAIL, "Ann Example") in fake.calls


def test_a_conversation_that_is_not_that_customers_is_refused(client):
    http, install = client
    install(FakeChatwoot(
        contacts=[{"id": 2, "email": EMAIL}],
        conversations=[_conv(9)],
        details={9: {"inbox_id": INBOX, "meta": {"channel": "Channel::Email",
                                                  "sender": {"email": "someone.else@example.com"}}}}))
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 409 and r.json()["status"] == "mismatch"


def test_two_contacts_with_the_same_email_are_refused(client):
    http, install = client
    install(FakeChatwoot(contacts=[{"id": 2, "email": EMAIL}, {"id": 3, "email": EMAIL}]))
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 409 and r.json()["status"] == "ambiguous_contact"


def test_chatwoot_errors_are_bounded_codes(client):
    http, install = client
    install(FakeChatwoot(contacts=[{"id": 2, "email": EMAIL}], fail="contact_conversations"))
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 502 and r.json() == {"status": "error", "detail": "http_500", "inbox_id": INBOX}


@pytest.mark.parametrize("headers,code", [({}, 401), (auth("x" * 40), 401)])
def test_auth_fails_closed_before_chatwoot(client, headers, code):
    http, install = client
    fake = install(FakeChatwoot())
    assert http.post(ROUTE, json=body(), headers=headers).status_code == code
    assert fake.calls == []


def test_disabled_until_an_inbox_is_configured(client, monkeypatch):
    http, install = client
    fake = install(FakeChatwoot())
    monkeypatch.delenv("BRIDGE_OUTBOUND_INBOX_ID")
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 503 and fake.calls == []


@pytest.mark.parametrize("bad", [
    {"email": "not-an-email"}, {"email": ""}, {"subject": ""}, {"actor": ""},
    {"conversation_id": 9}, {"private": False},
])
def test_invalid_requests_never_reach_chatwoot(client, bad):
    http, install = client
    fake = install(FakeChatwoot())
    assert http.post(ROUTE, json=body(**bad), headers=auth()).status_code == 422
    assert fake.calls == []


# ── the real client's HTTP calls ─────────────────────────────────────────────

class Recorder:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url.split("/api/v1/accounts/2")[1], kwargs.get("json"),
                           kwargs.get("params")))
        status, payload = self.responses.pop(0)
        resp = requests.Response()
        resp.status_code = status
        resp._content = __import__("json").dumps(payload).encode()
        return resp


def test_real_client_creates_a_conversation_without_posting_a_message(monkeypatch):
    rec = Recorder([
        (200, {"payload": {"id": 2, "contact_inboxes": []}}),     # GET contact
        (200, {"source_id": EMAIL}),                              # POST contact_inboxes
        (200, {"id": 901}),                                       # POST conversations
    ])
    monkeypatch.setattr(chatwoot.requests, "request", rec)
    c = ChatwootClient("http://cw", "2", "token")
    source = c.contact_source_id(2, INBOX, EMAIL)
    assert c.create_conversation(2, INBOX, source, "Refund Update: Order #1") == 901
    assert [(m, p) for m, p, _, _ in rec.calls] == [
        ("GET", "/contacts/2"), ("POST", "/contacts/2/contact_inboxes"), ("POST", "/conversations")]
    created = rec.calls[-1][2]
    assert created == {"source_id": EMAIL, "inbox_id": INBOX, "contact_id": 2, "status": "open",
                       "additional_attributes": {"mail_subject": "Refund Update: Order #1"}}
    assert "message" not in created and not any("/messages" in p for _, p, _, _ in rec.calls)


def test_real_client_matches_contact_email_exactly(monkeypatch):
    rec = Recorder([(200, {"payload": [
        {"id": 2, "email": "ANN.example@example.com"},
        {"id": 3, "email": "ann.example@example.com.evil"},
        {"id": 4, "email": None},
    ]})])
    monkeypatch.setattr(chatwoot.requests, "request", rec)
    assert [c["id"] for c in ChatwootClient("http://cw", "2", "t").find_contacts_by_email(EMAIL)] == [2]


def test_real_client_error_carries_no_response_text(monkeypatch):
    rec = Recorder([(422, {"message": "Ann Example card 4111111111111111"})])
    monkeypatch.setattr(chatwoot.requests, "request", rec)
    with pytest.raises(ChatwootError) as exc:
        ChatwootClient("http://cw", "2", "t").conversation_details(9)
    assert str(exc.value) == "http_422"


def test_a_non_email_inbox_conversation_is_refused(client):
    """Review #5: a widget/API inbox would accept the message and email nobody."""
    http, install = client
    install(FakeChatwoot(
        contacts=[{"id": 2, "email": EMAIL}], conversations=[_conv(9)],
        details={9: {"inbox_id": INBOX, "meta": {"channel": "Channel::Api", "sender": {"email": EMAIL}}}}))
    r = http.post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 409 and r.json()["status"] == "mismatch"


def test_the_email_returned_is_the_one_chatwoot_holds(client):
    """Review #7: echoing the caller's own input would make the consumer's check a no-op."""
    http, install = client
    install(FakeChatwoot(
        contacts=[{"id": 2, "email": EMAIL}], conversations=[_conv(9)],
        details={9: {"inbox_id": INBOX, "meta": {"channel": "Channel::Email",
                                                  "sender": {"email": "Ann.Example@Example.COM"}}}}))
    assert http.post(ROUTE, json=body(), headers=auth()).json()["contact_email"] == "Ann.Example@Example.COM"


def test_real_client_reads_every_page_of_contacts(monkeypatch):
    rec = Recorder([
        (200, {"payload": [{"id": 2, "email": EMAIL}], "meta": {"has_more": True}}),
        (200, {"payload": [{"id": 3, "email": EMAIL}], "meta": {"has_more": False}}),
    ])
    monkeypatch.setattr(chatwoot.requests, "request", rec)
    found = ChatwootClient("http://cw", "2", "t").find_contacts_by_email(EMAIL)
    assert [c["id"] for c in found] == [2, 3], "a duplicate on page 2 must still be seen"
    assert [p["page"] for _, _, _, p in rec.calls] == [1, 2]

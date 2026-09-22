import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
from chatwoot import OutboundResult
from state import DedupStore

ROUTE = "/internal/chatwoot/outbound-message"
TOKEN = "t" * 40
RECIPIENT = "ann.example@example.com"


def email_conversation(email=RECIPIENT, inbox_id=1, channel="Channel::Email"):
    return {"inbox_id": inbox_id, "meta": {"channel": channel, "sender": {"email": email}}}


def body(**updates):
    value = {
        "conversation_id": 45,
        "recipient_email": RECIPIENT,
        "content": "Your refund of $12.00 was issued.",
        "idempotency_key": "refund:123",
        "actor": "lana",
        "source": "dewieops-refund-test",
    }
    value.update(updates)
    return value


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


class FakeChatwoot:
    """Records public-outgoing calls; any private-note call fails the test."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def post_public_outgoing(self, conversation_id, content):
        self.calls.append((conversation_id, content))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def post_private_note(self, *args, **kwargs):
        raise AssertionError("outbound transport must never post a private note")

    details = None

    def conversation_details(self, conversation_id):
        self.reads = getattr(self, "reads", 0) + 1
        return self.details if self.details is not None else email_conversation()


class ForbiddenStore:
    def __getattr__(self, name):
        raise AssertionError(f"store.{name} must not be reached")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", TOKEN)
    store = DedupStore(tmp_path / "bridge.sqlite3")
    monkeypatch.setattr(main, "dedup_store", lambda: store)

    def install(*results, cls=FakeChatwoot):
        fake = cls(*results)
        monkeypatch.setattr(main, "chatwoot_client", lambda: fake)
        return fake

    return store, install


def accepted(message_id=501):
    return OutboundResult("accepted", 200, message_id, "created")


def test_missing_invalid_and_unconfigured_auth_fail_before_claim(monkeypatch):
    fake = FakeChatwoot()
    monkeypatch.setattr(main, "dedup_store", lambda: ForbiddenStore())
    monkeypatch.setattr(main, "chatwoot_client", lambda: fake)
    client = TestClient(main.app)

    monkeypatch.delenv("BRIDGE_OUTBOUND_TOKEN", raising=False)
    assert client.post(ROUTE, json=body(), headers=auth()).status_code == 503

    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", "short")
    response = client.post(ROUTE, json=body(), headers=auth("short"))
    assert response.status_code == 503
    assert response.json()["detail"] == "outbound_token_too_short"

    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", TOKEN)
    for headers in (
        {},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
        auth("x" * 40),
        auth(TOKEN + "x"),
    ):
        response = client.post(ROUTE, json=body(), headers=headers)
        assert response.status_code == 401, headers

    assert fake.calls == []


def test_outbound_token_must_not_reuse_other_bridge_secrets(monkeypatch):
    fake = FakeChatwoot()
    monkeypatch.setattr(main, "dedup_store", lambda: ForbiddenStore())
    monkeypatch.setattr(main, "chatwoot_client", lambda: fake)
    client = TestClient(main.app)
    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", TOKEN)

    for name in ("CHATWOOT_WEBHOOK_SECRET", "CHATWOOT_API_TOKEN"):
        monkeypatch.setenv(name, TOKEN)
        response = client.post(ROUTE, json=body(), headers=auth())
        assert response.status_code == 503
        assert response.json()["detail"] == "outbound_token_reuses_other_secret"
        monkeypatch.delenv(name)

    assert fake.calls == []


def test_webhook_signature_does_not_authorize_outbound(monkeypatch):
    import webhook_auth

    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", TOKEN)
    monkeypatch.setattr(main, "dedup_store", lambda: ForbiddenStore())
    raw = b'{"conversation_id":45}'

    response = TestClient(main.app).post(
        ROUTE, content=raw, headers=webhook_auth.sign_headers(raw, "secret")
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "value",
    [
        body(conversation_id=None),
        body(conversation_id=0),
        body(conversation_id=-4),
        body(conversation_id="45"),
        body(conversation_id=True),
        body(conversation_id=4.5),
        body(content=""),
        body(content="   \n"),
        body(idempotency_key=""),
        body(idempotency_key="has space"),
        body(idempotency_key="k" * 201),
        body(actor=""),
        body(source="  "),
        body(private=True),
        body(message_type="outgoing"),
        body(email="someone@example.com"),
        {key: value for key, value in body().items() if key != "actor"},
        {key: value for key, value in body().items() if key != "source"},
        {key: value for key, value in body().items() if key != "idempotency_key"},
        ["not", "an", "object"],
    ],
)
def test_invalid_request_is_rejected_before_claim(monkeypatch, value):
    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", TOKEN)
    monkeypatch.setattr(main, "dedup_store", lambda: ForbiddenStore())
    fake = FakeChatwoot()
    monkeypatch.setattr(main, "chatwoot_client", lambda: fake)

    response = TestClient(main.app).post(ROUTE, json=value, headers=auth())

    assert response.status_code == 422
    assert fake.calls == []


def test_malformed_json_is_rejected_before_claim(monkeypatch):
    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", TOKEN)
    monkeypatch.setattr(main, "dedup_store", lambda: ForbiddenStore())

    response = TestClient(main.app).post(ROUTE, content=b"{nope", headers=auth())

    assert response.status_code == 400


def test_accepted_send_records_audit_and_duplicate_does_not_call_again(wired):
    store, install = wired
    fake = install(accepted())
    client = TestClient(main.app)

    first = client.post(ROUTE, json=body(), headers=auth())
    second = client.post(ROUTE, json=body(actor="someone-else"), headers=auth())

    assert first.status_code == 200
    assert first.json() == {
        "status": "accepted",
        "idempotency_key": "refund:123",
        "conversation_id": 45,
        "chatwoot_message_id": 501,
        "http_status": 200,
        "detail": "created",
        "attempts": 1,
        "replayed": False,
        "retry_safe": False,
    }
    assert second.status_code == 200
    assert second.json() == {**first.json(), "replayed": True}
    assert fake.calls == [(45, "Your refund of $12.00 was issued.")]

    record = store.get_outbound("refund:123")
    assert record.actor == "lana"
    assert record.source == "dewieops-refund-test"
    assert record.content_sha256 == hashlib.sha256(
        "Your refund of $12.00 was issued.".encode("utf-8")
    ).hexdigest()


def test_claim_is_durable_before_chatwoot_is_called(wired):
    store, install = wired
    seen = []

    class Inspecting(FakeChatwoot):
        def post_public_outgoing(self, conversation_id, content):
            with sqlite3.connect(store.path) as connection:
                seen.append(connection.execute(
                    "SELECT status FROM outbound_message WHERE idempotency_key = ?",
                    ("refund:123",),
                ).fetchone())
            return super().post_public_outgoing(conversation_id, content)

    install(accepted(), cls=Inspecting)

    response = TestClient(main.app).post(ROUTE, json=body(), headers=auth())

    assert response.json()["status"] == "accepted"
    assert seen == [("pending",)]


def test_timeout_after_dispatch_is_durable_unknown_and_not_resent(wired):
    store, install = wired
    fake = install(OutboundResult("unknown", None, None, "ReadTimeout: read timed out"))
    client = TestClient(main.app)

    first = client.post(ROUTE, json=body(), headers=auth())
    retry = client.post(ROUTE, json=body(), headers=auth())

    assert first.status_code == 504
    assert first.json()["status"] == "unknown"
    assert first.json()["retry_safe"] is False
    assert retry.status_code == 504
    assert retry.json()["status"] == "unknown"
    assert retry.json()["replayed"] is True
    assert len(fake.calls) == 1
    assert DedupStore(store.path).get_outbound("refund:123").status == "unknown"


def test_unexpected_client_error_is_recorded_unknown(wired):
    store, install = wired
    fake = install(RuntimeError("boom"))

    response = TestClient(main.app).post(ROUTE, json=body(), headers=auth())

    assert response.status_code == 504
    assert response.json()["status"] == "unknown"
    assert "RuntimeError" in response.json()["detail"]
    assert store.get_outbound("refund:123").status == "unknown"
    assert len(fake.calls) == 1


def test_interrupted_pending_claim_replays_as_unknown_without_sending(wired):
    store, install = wired
    store.begin_outbound(
        "refund:123",
        45,
        hashlib.sha256(body()["content"].encode("utf-8")).hexdigest(),
        "lana",
        "crashed-worker",
    )
    fake = install()

    response = TestClient(main.app).post(ROUTE, json=body(), headers=auth())

    assert response.status_code == 504
    assert response.json()["status"] == "unknown"
    assert response.json()["detail"] == "claim_pending_outcome_unknown"
    assert response.json()["replayed"] is True
    assert fake.calls == []


def test_definitive_rejection_is_distinct_and_same_key_may_retry(wired):
    store, install = wired
    fake = install(
        OutboundResult("rejected", 404, None, "http_404"),
        accepted(777),
    )
    client = TestClient(main.app)

    rejected = client.post(ROUTE, json=body(), headers=auth())
    retried = client.post(ROUTE, json=body(actor="retrier"), headers=auth())
    replay = client.post(ROUTE, json=body(), headers=auth())

    assert rejected.status_code == 502
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["http_status"] == 404
    assert rejected.json()["retry_safe"] is True
    assert retried.status_code == 200
    assert retried.json()["status"] == "accepted"
    assert retried.json()["attempts"] == 2
    assert retried.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    assert len(fake.calls) == 2
    history = store.outbound_attempts("refund:123")
    assert [(row["actor"], row["status"]) for row in history] == [
        ("lana", "rejected"),
        ("retrier", "accepted"),
    ]


def test_key_reuse_for_different_message_conflicts_without_sending(wired):
    store, install = wired
    fake = install(accepted())
    client = TestClient(main.app)

    client.post(ROUTE, json=body(), headers=auth())
    other_content = client.post(ROUTE, json=body(content="Different text"), headers=auth())
    other_conversation = client.post(ROUTE, json=body(conversation_id=46), headers=auth())

    assert other_content.status_code == 409
    assert other_content.json()["detail"] == "idempotency_key_reused_for_different_message"
    assert other_conversation.status_code == 409
    assert len(fake.calls) == 1


def test_content_is_sent_exactly_as_supplied(wired):
    store, install = wired
    fake = install(accepted())
    content = "  Line one\n\nLine two  "

    TestClient(main.app).post(ROUTE, json=body(content=content), headers=auth())

    assert fake.calls == [(45, content)]


def test_health_reports_outbound_configuration_without_secret(monkeypatch):
    monkeypatch.setenv("BRIDGE_OUTBOUND_TOKEN", TOKEN)

    health = TestClient(main.app).get("/health").json()

    assert health["outbound_auth"] == "configured"
    assert TOKEN not in str(health)


def test_accepted_send_whose_outcome_cannot_be_recorded_is_frozen_unknown(
    wired, monkeypatch
):
    store, install = wired
    fake = install(accepted(501))
    real_finish = store.finish_outbound
    failing = [True]

    def flaky_finish(*args, **kwargs):
        if failing[0]:
            raise sqlite3.OperationalError("database is locked")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_outbound", flaky_finish)
    client = TestClient(main.app)

    first = client.post(ROUTE, json=body(), headers=auth())
    failing[0] = False  # the store works again; the claim must still be frozen
    replay = client.post(ROUTE, json=body(), headers=auth())

    assert first.status_code == 504
    assert first.json()["status"] == "unknown"
    assert first.json()["retry_safe"] is False
    assert first.json()["replayed"] is False
    assert first.json()["detail"] == "outcome_not_recorded"
    assert replay.status_code == 504
    assert replay.json()["status"] == "unknown"
    assert replay.json()["retry_safe"] is False
    assert replay.json()["replayed"] is True
    assert fake.calls == [(45, body()["content"])]
    assert DedupStore(store.path).get_outbound("refund:123").status == "pending"


def test_claim_failure_before_the_call_is_not_reported_as_ambiguous(wired, monkeypatch):
    store, install = wired
    fake = install(accepted())

    def broken_begin(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "begin_outbound", broken_begin)

    response = TestClient(main.app, raise_server_exceptions=False).post(
        ROUTE, json=body(), headers=auth()
    )

    # Review #4: a bare 500 was read by DewieOps as `unknown` and froze a key the
    # bridge never recorded. The answer is now a structured, trusted rejection.
    assert response.status_code == 502
    assert response.json() == {
        "status": "rejected", "idempotency_key": "refund:123", "conversation_id": 45,
        "chatwoot_message_id": None, "http_status": None, "detail": "claim_failed",
        "attempts": 0, "replayed": False, "retry_safe": True,
    }
    assert fake.calls == []
    assert store.get_outbound("refund:123") is None


def _stored_text(path):
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT * FROM outbound_message").fetchall()
        rows += connection.execute("SELECT * FROM outbound_attempt").fetchall()
    return rows, " ".join(str(value) for row in rows for value in row)


def test_rejection_body_echoing_content_is_not_persisted(wired, monkeypatch):
    import chatwoot

    store, _ = wired
    content = "Refund REF-SECRET-8841 for Pat Example was issued."

    class Echo:
        status_code = 422
        text = f'{{"error": "could not create message: {content}"}}'

        def json(self):
            return {"error": f"could not create message: {content}"}

    monkeypatch.setattr("chatwoot.requests.post", lambda *args, **kwargs: Echo())
    monkeypatch.setattr(
        main, "chatwoot_client", lambda: chatwoot.ChatwootClient("http://desk", "1", "token")
    )
    monkeypatch.setattr(
        chatwoot.ChatwootClient, "conversation_details", lambda self, cid: email_conversation()
    )

    response = TestClient(main.app).post(ROUTE, json=body(content=content), headers=auth())

    assert response.status_code == 502
    assert response.json()["status"] == "rejected"
    assert response.json()["http_status"] == 422
    assert response.json()["detail"] == "http_422"
    assert "REF-SECRET-8841" not in response.text
    rows, stored = _stored_text(store.path)
    assert len(rows) == 2
    assert "REF-SECRET-8841" not in stored
    assert "Pat Example" not in stored


def test_exception_message_echoing_content_is_not_persisted(wired, monkeypatch):
    import chatwoot
    import requests

    store, _ = wired
    content = "Refund REF-SECRET-8841 was issued."

    def post(*args, **kwargs):
        raise requests.ReadTimeout(f"timed out sending {content}")

    monkeypatch.setattr("chatwoot.requests.post", post)
    monkeypatch.setattr(
        main, "chatwoot_client", lambda: chatwoot.ChatwootClient("http://desk", "1", "token")
    )
    monkeypatch.setattr(
        chatwoot.ChatwootClient, "conversation_details", lambda self, cid: email_conversation()
    )

    response = TestClient(main.app).post(ROUTE, json=body(content=content), headers=auth())

    assert response.json()["status"] == "unknown"
    assert response.json()["detail"] == "request_error:ReadTimeout"
    assert "REF-SECRET-8841" not in _stored_text(store.path)[1]


def test_free_text_client_detail_is_withheld_before_storage(wired):
    store, install = wired
    install(
        OutboundResult("rejected", 400, None, "HTTP 400: Refund REF-SECRET-8841"),
        RuntimeError("failed on Refund REF-SECRET-8841"),
    )
    client = TestClient(main.app)

    rejected = client.post(ROUTE, json=body(idempotency_key="k1"), headers=auth())
    unknown = client.post(
        ROUTE, json=body(idempotency_key="k2", content="Other text"), headers=auth()
    )

    assert rejected.json()["detail"] == "detail_withheld"
    assert unknown.json()["detail"] == "transport_error:RuntimeError"
    assert "REF-SECRET-8841" not in _stored_text(store.path)[1]


@pytest.mark.parametrize(
    "detail",
    [
        "REFSECRET8841",
        "4111111111111111",
        "http_4111111111111111",
        "http_4111",
        "request_error:REFSECRET8841",
        "request_error:4111111111111111",
        "transport_error:RuntimeError",
        "created:4111111111111111",
        "CREATED",
        "",
    ],
)
def test_code_shaped_client_detail_is_withheld_everywhere(wired, detail):
    store, install = wired
    install(OutboundResult("rejected", 400, None, detail))

    response = TestClient(main.app).post(ROUTE, json=body(), headers=auth())

    assert response.json()["status"] == "rejected"
    assert response.json()["detail"] == "detail_withheld"
    rows, stored = _stored_text(store.path)
    assert len(rows) == 2
    assert "REFSECRET8841" not in response.text + stored
    assert "4111111111111111" not in response.text + stored
    assert "transport_error:RuntimeError" not in stored


def test_every_detail_the_real_client_emits_is_allowlisted(monkeypatch):
    import chatwoot
    import outbound
    import requests
    from urllib3.exceptions import MaxRetryError, NewConnectionError

    class Reply:
        text = "ignored"

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    outcomes = [
        Reply(200, {"id": 501}),
        Reply(201, {}),
        *(Reply(status, {}) for status in (400, 404, 408, 422, 429, 500, 503)),
        requests.ConnectTimeout("x"),
        requests.ReadTimeout("x"),
        requests.ConnectionError("x"),
        requests.ConnectionError(MaxRetryError(None, "/", NewConnectionError(None, "x"))),
        requests.exceptions.InvalidURL("x"),
        requests.exceptions.MissingSchema("x"),
        requests.exceptions.InvalidSchema("x"),
        requests.exceptions.InvalidHeader("x"),
        requests.exceptions.ChunkedEncodingError("x"),
        requests.exceptions.SSLError("x"),
        requests.exceptions.TooManyRedirects("x"),
    ]
    client = chatwoot.ChatwootClient("http://desk", "1", "token")
    emitted = [
        client.post_public_outgoing(0, "Hi").detail,
        client.post_public_outgoing(45, " ").detail,
    ]
    for outcome in outcomes:
        def post(*args, _outcome=outcome, **kwargs):
            if isinstance(_outcome, Exception):
                raise _outcome
            return _outcome

        monkeypatch.setattr("chatwoot.requests.post", post)
        emitted.append(client.post_public_outgoing(45, "Hi").detail)

    assert "detail_withheld" not in emitted
    assert [outbound._safe_detail(value) for value in emitted] == emitted
    assert {"created", "accepted_without_message_id", "http_422", "http_503",
            "request_error:ReadTimeout", "request_error:ConnectionRefused"} <= set(emitted)


# ── review 2026-09-22: the send is bound to its recipient ────────────────────

@pytest.mark.parametrize("details,detail", [
    (email_conversation(email="someone.else@example.com"), "recipient_mismatch"),   # contact merged away
    (email_conversation(inbox_id=7), "recipient_mismatch"),                          # another inbox
    (email_conversation(channel="Channel::WebWidget"), "recipient_mismatch"),        # would email nobody
    ({"inbox_id": 1, "meta": {}}, "recipient_mismatch"),
])
def test_a_conversation_that_no_longer_belongs_to_the_recipient_is_never_posted(wired, details, detail):
    store, install = wired
    fake = install(accepted())
    fake.details = details
    r = TestClient(main.app).post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 502
    assert (r.json()["status"], r.json()["detail"], r.json()["retry_safe"]) == ("rejected", detail, True)
    assert fake.calls == [], "nothing may be posted to a conversation that is not the recipient's"
    assert store.get_outbound("refund:123").status == "rejected"


def test_an_unreadable_conversation_is_rejected_without_posting(wired):
    from chatwoot import ChatwootError

    store, install = wired

    class Unreadable(FakeChatwoot):
        def conversation_details(self, conversation_id):
            raise ChatwootError("http_500")

    fake = install(accepted(), cls=Unreadable)
    r = TestClient(main.app).post(ROUTE, json=body(), headers=auth())
    assert (r.json()["status"], r.json()["detail"]) == ("rejected", "recipient_unverified")
    assert fake.calls == []


def test_recipient_email_is_compared_case_insensitively(wired):
    _, install = wired
    fake = install(accepted())
    fake.details = email_conversation(email="Ann.Example@Example.com")
    assert TestClient(main.app).post(ROUTE, json=body(), headers=auth()).json()["status"] == "accepted"


def test_a_replay_is_answered_from_the_record_without_rereading(wired):
    _, install = wired
    fake = install(accepted())
    client = TestClient(main.app)
    client.post(ROUTE, json=body(), headers=auth())
    fake.details = email_conversation(email="merged.elsewhere@example.com")
    again = client.post(ROUTE, json=body(), headers=auth()).json()
    assert (again["status"], again["replayed"]) == ("accepted", True)
    assert fake.reads == 1 and len(fake.calls) == 1


@pytest.mark.parametrize("bad", [
    {"recipient_email": None}, {"recipient_email": "not-an-email"},
    {"actor": "Ann Example"}, {"actor": "card 4111 1111 1111 1111"}, {"source": "refund for Ann"},
])
def test_recipient_is_required_and_labels_are_machine_ids(wired, bad):
    _, install = wired
    fake = install(accepted())
    payload = body(**bad)
    if bad.get("recipient_email", "") is None:
        payload.pop("recipient_email")
    assert TestClient(main.app).post(ROUTE, json=payload, headers=auth()).status_code == 422
    assert fake.calls == []


def test_outbound_is_disabled_until_the_email_inbox_is_configured(wired, monkeypatch):
    store, install = wired
    fake = install(accepted())
    monkeypatch.delenv("BRIDGE_OUTBOUND_INBOX_ID")
    r = TestClient(main.app).post(ROUTE, json=body(), headers=auth())
    assert r.status_code == 503 and r.json()["detail"] == "outbound_inbox_not_configured"
    assert fake.calls == [] and store.get_outbound("refund:123") is None

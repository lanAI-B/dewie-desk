import inspect

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from chatwoot import ChatwootClient


class Response:
    status_code = 200
    text = "ok"

    def json(self):
        return {"id": 91}


def test_private_note_shape_is_hard_coded(monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr("chatwoot.requests.post", post)
    result = ChatwootClient("http://desk", "1", "token").post_private_note(7, "Draft")

    assert result.ok
    assert result.message_id == 91
    assert captured["json"] == {
        "content": "Draft",
        "message_type": "outgoing",
        "private": True,
    }


def test_private_note_failure_is_reported_not_raised(monkeypatch):
    response = Response()
    response.status_code = 500
    response.text = "broken"
    monkeypatch.setattr("chatwoot.requests.post", lambda *a, **k: response)

    result = ChatwootClient("http://desk", "1", "token").post_private_note(7, "Draft")

    assert not result.ok
    assert result.status_code == 500


def test_get_conversation_returns_current_message_page(monkeypatch):
    response = Response()
    response.json = lambda: {
        "meta": {"labels": ["dewie-draft"]},
        "payload": [{"id": 42, "message_type": 0}],
    }
    captured = {}

    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return response

    monkeypatch.setattr("chatwoot.requests.get", get)

    result = ChatwootClient("http://desk", "1", "token").get_conversation(7)

    assert result.ok
    assert result.messages[0]["id"] == 42
    assert result.meta["labels"] == ["dewie-draft"]
    assert captured["url"].endswith("/conversations/7/messages")


def test_remove_label_preserves_every_other_label(monkeypatch):
    read = Response()
    read.json = lambda: {"payload": ["support", "dewie-draft", "vip"]}
    written = Response()
    written.json = lambda: {"payload": ["support", "vip"]}
    captured = {}
    monkeypatch.setattr("chatwoot.requests.get", lambda *args, **kwargs: read)

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return written

    monkeypatch.setattr("chatwoot.requests.post", post)

    result = ChatwootClient("http://desk", "1", "token").remove_label(7, "dewie-draft")

    assert result.ok
    assert result.labels == ("support", "vip")
    assert captured["json"] == {"labels": ["support", "vip"]}


def _public_client(monkeypatch, outcome):
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("chatwoot.requests.post", post)
    return ChatwootClient("http://desk", "1", "token"), captured


def _response(status_code, body=None, text="body"):
    response = Response()
    response.status_code = status_code
    response.text = text
    if isinstance(body, Exception):
        def raise_body():
            raise body

        response.json = raise_body
    else:
        response.json = lambda: body
    return response


def test_public_outgoing_shape_is_hard_coded(monkeypatch):
    client, captured = _public_client(
        monkeypatch, _response(200, {"id": 501, "private": False, "message_type": 1})
    )

    result = client.post_public_outgoing(45, "Your refund was issued.")

    assert result.outcome == "accepted"
    assert result.message_id == 501
    assert result.status_code == 200
    assert captured["url"] == "http://desk/api/v1/accounts/1/conversations/45/messages"
    assert captured["json"] == {
        "content": "Your refund was issued.",
        "message_type": "outgoing",
        "private": False,
    }
    assert captured["headers"]["api_access_token"] == "token"


def test_public_and_private_operations_take_no_visibility_argument():
    for method in (ChatwootClient.post_public_outgoing, ChatwootClient.post_private_note):
        assert list(inspect.signature(method).parameters) == [
            "self",
            "conversation_id",
            "content",
        ]


def test_public_outgoing_definitive_http_rejections(monkeypatch):
    for status in (400, 401, 403, 404, 422, 429):
        client, _ = _public_client(monkeypatch, _response(status, {}, "nope"))
        result = client.post_public_outgoing(45, "Hi")
        assert result.outcome == "rejected", status
        assert result.status_code == status
        assert result.message_id is None


def test_public_outgoing_ambiguous_http_responses_are_unknown(monkeypatch):
    for status in (408, 500, 502, 503, 504):
        client, _ = _public_client(monkeypatch, _response(status, {}, "maybe"))
        result = client.post_public_outgoing(45, "Hi")
        assert result.outcome == "unknown", status
        assert result.status_code == status


def test_public_outgoing_success_without_message_id_is_unknown(monkeypatch):
    for body in ({}, {"id": "not-a-number"}, {"id": True}, ValueError("not json"), ["list"]):
        client, _ = _public_client(monkeypatch, _response(201, body))
        result = client.post_public_outgoing(45, "Hi")
        assert result.outcome == "unknown", body
        assert result.status_code == 201


def test_public_outgoing_read_timeout_after_dispatch_is_unknown(monkeypatch):
    client, _ = _public_client(monkeypatch, requests.ReadTimeout("read timed out"))

    result = client.post_public_outgoing(45, "Hi")

    assert result.outcome == "unknown"
    assert result.status_code is None
    assert "ReadTimeout" in result.detail


def test_public_outgoing_dropped_connection_after_dispatch_is_unknown(monkeypatch):
    client, _ = _public_client(
        monkeypatch, requests.ConnectionError("Connection aborted: RemoteDisconnected")
    )

    assert client.post_public_outgoing(45, "Hi").outcome == "unknown"


def test_public_outgoing_failures_before_dispatch_are_rejected(monkeypatch):
    refused = requests.ConnectionError(
        MaxRetryError(None, "/", NewConnectionError(None, "connection refused"))
    )
    for error in (
        requests.ConnectTimeout("connect timed out"),
        refused,
        requests.exceptions.InvalidURL("bad url"),
        requests.exceptions.MissingSchema("no schema"),
    ):
        client, _ = _public_client(monkeypatch, error)
        result = client.post_public_outgoing(45, "Hi")
        assert result.outcome == "rejected", type(error).__name__
        assert result.status_code is None


def test_public_outgoing_rejects_invalid_input_without_calling(monkeypatch):
    client, captured = _public_client(monkeypatch, _response(200, {"id": 1}))

    for conversation_id, content in ((0, "Hi"), (-1, "Hi"), (True, "Hi"), (45, "  ")):
        result = client.post_public_outgoing(conversation_id, content)
        assert result.outcome == "rejected"

    assert captured == {}

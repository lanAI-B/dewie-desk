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

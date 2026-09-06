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

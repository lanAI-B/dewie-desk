import webhook_auth


def test_valid_signature_is_verified(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    raw = b'{"event":"message_created"}'
    headers = webhook_auth.sign_headers(raw, "secret", timestamp=1000)

    verdict = webhook_auth.verify(raw, headers, now=1001)

    assert verdict.ok
    assert verdict.verified


def test_tampering_and_stale_delivery_are_rejected(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    raw = b'{"event":"message_created"}'
    headers = webhook_auth.sign_headers(raw, "secret", timestamp=1000)

    assert webhook_auth.verify(raw + b" ", headers, now=1001).reason == "signature_mismatch"
    assert webhook_auth.verify(raw, headers, now=2000).reason == "stale_timestamp"


def test_config_gap_is_visible_but_keeps_local_pilot_compatible(monkeypatch):
    monkeypatch.delenv("CHATWOOT_WEBHOOK_SECRET", raising=False)
    verdict = webhook_auth.verify(b"{}", {})

    assert verdict.ok
    assert verdict.mode == "unenforced"
    assert verdict.reason == "webhook_secret_not_configured"


def test_hostile_headers_never_raise(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    verdict = webhook_auth.verify(b"{}", {"x-chatwoot-signature": "sha256=ü"})
    assert not verdict.ok

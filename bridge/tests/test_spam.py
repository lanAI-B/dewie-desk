import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import spam
from chatwoot import ChatwootClient, LabelResult, PostResult
from dewie_brain.desk import Actor, Classification, Intent
from parser import ParsedMessage
from tests.test_webhook_endpoint import Store, payload, signed_post


def message(from_email="person@example.com", subject="Question", conversation_id=7):
    return ParsedMessage(
        event="message_created",
        account_id=1,
        conversation_id=conversation_id,
        inbox_id=1,
        message_id=42,
        from_email=from_email,
        subject=subject,
        body="body",
        message_type="incoming",
        sender_type="contact",
        should_process=True,
    )


class FakeClient:
    def __init__(self, label_ok=True, resolve_ok=True):
        self.calls = []
        self.label_ok = label_ok
        self.resolve_ok = resolve_ok

    def add_label(self, conversation_id, label):
        self.calls.append(("label", conversation_id, label))
        return LabelResult(self.label_ok, status_code=200 if self.label_ok else 401)

    def resolve_conversation(self, conversation_id):
        self.calls.append(("resolve", conversation_id))
        return PostResult(self.resolve_ok, status_code=200 if self.resolve_ok else 401)

    def __getattr__(self, name):  # any other call (notes, sends, deletes) is a bug
        raise AssertionError(f"spam screening must not call {name}")


def audit_lines():
    path = Path(os.environ["SPAM_AUTORESOLVE_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def classification(actor=Actor.SYSTEM, intent=Intent.NOTIFY, conf=0.95):
    return Classification(actor, intent, conf, conf, "fake", "fake")


# ── rules ────────────────────────────────────────────────────────────────────


def test_shipped_rule_file_parses_and_is_seeded_from_dewiebrain():
    rules = spam.load_rules()
    senders = {r.sender for r in rules if r.action == "spam"}
    assert "ebay@ebay.com" in senders
    assert "order_confirmation@corporate-central.com" in senders
    assert any(r.action == "allow" and r.sender == "@actuarialbookstore.com" for r in rules)
    assert all(r.source for r in rules)


@pytest.mark.parametrize("sender", [
    "ebay@ebay.com", "EBAY@eBay.com", "eBay <ebay@ebay.com>", "sales3@bestpricewholesale4all.com",
])
def test_exact_sender_rule_matches_case_insensitively(sender):
    verdict = spam.evaluate_rules(sender, "Anything at all")
    assert verdict.spam and verdict.stage == "rule"
    assert verdict.rule.startswith("spam_rules.csv:")


def test_exact_sender_rule_does_not_match_lookalike():
    assert spam.evaluate_rules("notebay@ebay.com", "hello") is None


def test_sender_and_subject_rule_needs_both():
    sender = "order_confirmation@corporate-central.com"
    assert spam.evaluate_rules(sender, "ACTEX Backorder has been received at warehouse.").spam
    assert spam.evaluate_rules(sender, "Something else entirely") is None


def test_exact_sender_spam_rule_beats_domain_allow():
    verdict = spam.evaluate_rules("sales@actuarialbookstore.com", "Actuarial Bookstore Confirmation")
    assert verdict.spam


def test_allow_list_beats_subject_rules():
    verdict = spam.evaluate_rules("paula@actexlearning.com", "FREE shipping announcement")
    assert not verdict.spam and verdict.stage == "allow"
    verdict = spam.evaluate_rules("disputes@mail.stripe.com", "URGENT ACTION REQUIRED")
    assert not verdict.spam and verdict.stage == "allow"


def test_uppercase_only_rule_ignores_ordinary_words():
    assert spam.evaluate_rules("x@spammy.biz", "Get it FREE today").spam
    assert spam.evaluate_rules("x@spammy.biz", "Is the webinar free?") is None
    assert spam.evaluate_rules("x@spammy.biz", "FREEDOM sale") is None


def test_subject_rule_vetoed_when_subject_reads_like_a_customer():
    verdict = spam.evaluate_rules("jane@gmail.com", "Verify your account - order #123456")
    assert not verdict.spam and verdict.stage == "veto"


def test_rule_file_rejects_malformed_rows(tmp_path):
    bad = tmp_path / "rules.csv"
    bad.write_text("action,sender,subject,subject_match,source\nspam,,,contains,x\n")
    with pytest.raises(ValueError, match="needs a sender or subject"):
        spam.load_rules(bad)
    bad.write_text("action,sender,subject,subject_match,source\ndelete,a@b.c,,contains,x\n")
    with pytest.raises(ValueError, match="spam or allow"):
        spam.load_rules(bad)


# ── classifier stage ─────────────────────────────────────────────────────────


def test_classifier_not_called_unless_enabled(monkeypatch):
    def forbidden(_):
        raise AssertionError("classifier must be opt-in")

    verdict = spam.evaluate(message("x@unknown.biz", "Newsletter"), classify=forbidden)
    assert not verdict.spam


def test_classifier_not_called_when_a_rule_decided(monkeypatch):
    monkeypatch.setenv("SPAM_CLASSIFIER_ENABLED", "true")

    def forbidden(_):
        raise AssertionError("rules first")

    assert spam.evaluate(message("ebay@ebay.com"), classify=forbidden).spam
    assert not spam.evaluate(message("a@actexlearning.com"), classify=forbidden).spam


@pytest.mark.parametrize("result,expected", [
    (classification(), True),
    (classification(conf=0.8), False),
    (classification(actor=Actor.CUSTOMER), False),
    (classification(intent=Intent.ORDER_STATUS), False),
])
def test_classifier_only_resolves_confident_system_notifications(monkeypatch, result, expected):
    monkeypatch.setenv("SPAM_CLASSIFIER_ENABLED", "true")
    verdict = spam.evaluate(message("news@vendor.biz", "Weekly digest"), classify=lambda _: result)
    assert verdict.spam is expected
    assert verdict.score is not None


def test_classifier_failure_never_resolves(monkeypatch):
    monkeypatch.setenv("SPAM_CLASSIFIER_ENABLED", "true")

    def broken(_):
        raise RuntimeError("provider down")

    verdict = spam.evaluate(message("news@vendor.biz", "Weekly digest"), classify=broken)
    assert not verdict.spam and "RuntimeError" in verdict.reason


def test_classifier_verdict_vetoed_by_customer_subject(monkeypatch):
    monkeypatch.setenv("SPAM_CLASSIFIER_ENABLED", "true")
    verdict = spam.evaluate(message("x@vendor.biz", "Your refund"), classify=lambda _: classification())
    assert not verdict.spam and verdict.stage == "veto"


# ── acting on a verdict ──────────────────────────────────────────────────────


def test_disabled_by_default_does_nothing():
    client = FakeClient()
    assert spam.screen(message("ebay@ebay.com"), client) == "disabled"
    assert client.calls == []
    assert audit_lines() == []


def test_dry_run_is_the_default_once_enabled(monkeypatch):
    monkeypatch.setenv("SPAM_AUTORESOLVE_ENABLED", "true")
    client = FakeClient()
    assert spam.screen(message("ebay@ebay.com", "Deal"), client) == "would_resolve"
    assert client.calls == []
    [record] = audit_lines()
    assert record["outcome"] == "would_resolve"
    assert record["dry_run"] is True
    assert record["conversation_id"] == 7
    assert record["sender"] == "ebay@ebay.com"
    assert record["rule"].startswith("spam_rules.csv:")


def test_live_mode_labels_then_resolves_and_logs(monkeypatch):
    monkeypatch.setenv("SPAM_AUTORESOLVE_ENABLED", "true")
    monkeypatch.setenv("SPAM_AUTORESOLVE_DRY_RUN", "false")
    client = FakeClient()
    assert spam.screen(message("ebay@ebay.com"), client) == "resolved"
    assert client.calls == [("label", 7, "spam"), ("resolve", 7)]
    [record] = audit_lines()
    assert record["outcome"] == "resolved" and record["dry_run"] is False


def test_label_failure_stops_before_resolve(monkeypatch):
    monkeypatch.setenv("SPAM_AUTORESOLVE_ENABLED", "true")
    monkeypatch.setenv("SPAM_AUTORESOLVE_DRY_RUN", "false")
    client = FakeClient(label_ok=False)
    assert spam.screen(message("ebay@ebay.com"), client) == "label_failed"
    assert client.calls == [("label", 7, "spam")]
    assert audit_lines()[0]["http_status"] == 401


def test_not_spam_is_not_logged_or_touched(monkeypatch):
    monkeypatch.setenv("SPAM_AUTORESOLVE_ENABLED", "true")
    monkeypatch.setenv("SPAM_AUTORESOLVE_DRY_RUN", "false")
    client = FakeClient()
    assert spam.screen(message("jane@gmail.com", "Question about Exam P"), client) == "not_spam"
    assert client.calls == []
    assert audit_lines() == []


def test_veto_is_logged_but_not_acted_on(monkeypatch):
    monkeypatch.setenv("SPAM_AUTORESOLVE_ENABLED", "true")
    monkeypatch.setenv("SPAM_AUTORESOLVE_DRY_RUN", "false")
    client = FakeClient()
    assert spam.screen(message("x@y.biz", "FREE order #99999"), client) == "vetoed"
    assert client.calls == []
    assert audit_lines()[0]["outcome"] == "vetoed"


# ── Chatwoot client ──────────────────────────────────────────────────────────


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = "x"

    def json(self):
        return self._body


def test_add_label_preserves_existing_labels(monkeypatch):
    posted = {}
    monkeypatch.setattr("chatwoot.requests.get",
                        lambda url, **k: Response(body={"payload": ["dewie-draft"]}))

    def post(url, **kwargs):
        posted.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr("chatwoot.requests.post", post)
    result = ChatwootClient("http://desk", "2", "t").add_label(7, "spam")
    assert result.ok
    assert posted["url"] == "http://desk/api/v1/accounts/2/conversations/7/labels"
    assert posted["json"] == {"labels": ["dewie-draft", "spam"]}


def test_add_label_is_idempotent(monkeypatch):
    monkeypatch.setattr("chatwoot.requests.get",
                        lambda url, **k: Response(body={"payload": ["spam"]}))
    monkeypatch.setattr("chatwoot.requests.post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no write")))
    assert ChatwootClient("http://desk", "2", "t").add_label(7, "spam").detail == "already_present"


def test_resolve_uses_toggle_status_with_explicit_status(monkeypatch):
    posted = {}

    def post(url, **kwargs):
        posted.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr("chatwoot.requests.post", post)
    assert ChatwootClient("http://desk", "2", "t").resolve_conversation(7).ok
    assert posted["url"] == "http://desk/api/v1/accounts/2/conversations/7/toggle_status"
    assert posted["json"] == {"status": "resolved"}


def test_resolve_failure_reports_status(monkeypatch):
    monkeypatch.setattr("chatwoot.requests.post", lambda *a, **k: Response(status=401))
    result = ChatwootClient("http://desk", "2", "t").resolve_conversation(7)
    assert not result.ok and result.status_code == 401


# ── webhook wiring ───────────────────────────────────────────────────────────


def test_webhook_does_not_screen_when_disabled(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(main, "dedup_store", lambda: Store())
    monkeypatch.setattr(main, "screen_inbound",
                        lambda m: (_ for _ in ()).throw(AssertionError("disabled")))
    body = signed_post(TestClient(main.app), payload()).json()
    assert body["action"] == "recorded" and "spam_screen" not in body


def test_webhook_screens_new_inbound_and_never_drafts(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("SPAM_AUTORESOLVE_ENABLED", "true")
    monkeypatch.setenv("SPAM_AUTORESOLVE_DRY_RUN", "false")
    client = FakeClient()
    monkeypatch.setattr(main, "dedup_store", lambda: Store())
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(main, "process_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no draft")))

    body = signed_post(
        TestClient(main.app), payload(sender={"email": "ebay@ebay.com"})
    ).json()

    assert body["spam_screen"] == "queued"
    assert client.calls == [("label", 7, "spam"), ("resolve", 7)]
    assert audit_lines()[0]["conversation_id"] == 7


def test_duplicate_webhook_is_not_screened_twice(monkeypatch):
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("SPAM_AUTORESOLVE_ENABLED", "true")
    monkeypatch.setenv("SPAM_AUTORESOLVE_DRY_RUN", "false")
    client = FakeClient()
    store = Store()
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    test_client = TestClient(main.app)
    value = payload(sender={"email": "ebay@ebay.com"})
    signed_post(test_client, value)
    signed_post(test_client, value)
    assert client.calls == [("label", 7, "spam"), ("resolve", 7)]


def test_screen_error_is_contained(monkeypatch):
    monkeypatch.setenv("SPAM_AUTORESOLVE_ENABLED", "true")

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(main.spam, "screen", boom)
    assert main.screen_inbound(message()) == "error"


def test_health_reports_spam_flags():
    health = main.health()["spam_autoresolve"]
    assert health == {"enabled": False, "dry_run": True, "classifier": False,
                      "rules": len(spam.load_rules())}

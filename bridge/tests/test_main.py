import sys
from collections import Counter
from types import SimpleNamespace

import main
from chatwoot import ConversationResult, LabelResult
from dewie_brain.desk import Actor, Classification, DecisionAction, DraftDecision, Intent
from parser import ParsedAttachment, ParsedMessage


def message(from_email="person@example.com"):
    return ParsedMessage(
        event="message_created",
        account_id=1,
        conversation_id=7,
        message_id=42,
        from_email=from_email,
        subject="Question",
        body="Please help.",
        message_type="incoming",
        sender_type="contact",
        should_process=True,
    )


def test_system_sender_skips_before_classifier_runtime_is_built(monkeypatch):
    def forbidden():
        raise AssertionError("system sender must not invoke a classifier")

    monkeypatch.setattr(main, "classifier_runtime", forbidden)

    decision = main._decision(message("no-reply@example.com"))

    assert decision.action is DecisionAction.SKIP
    assert decision.reason_code == "system_sender_localpart"


def test_classifier_failure_triages_instead_of_drafting(monkeypatch):
    class BrokenRuntime:
        provider = "broken"
        model = "broken"

        def run_turn(self, request):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(main, "classifier_runtime", lambda: BrokenRuntime())

    decision = main._decision(message())

    assert decision.action is DecisionAction.TRIAGE
    assert decision.reason_code == "classifier_failed"


class NoteClient:
    def __init__(self):
        self.notes = []

    def post_private_note(self, conversation_id, content):
        self.notes.append((conversation_id, content))
        return SimpleNamespace(ok=True, status_code=200, detail="posted")


def test_label_drafts_despite_policy_decline_and_shows_doubts(monkeypatch):
    # The label is Lana's explicit request: a policy decline becomes a draft
    # whose note opens with the reasons the classifier doubted the message.
    classification = Classification(
        actor=Actor.CUSTOMER,
        intent=Intent.ACCESS_SUPPORT,
        actor_confidence=0.62,
        intent_confidence=0.70,
        provider="fake",
        model="classifier-test",
    )
    client = NoteClient()
    captured = {}
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "false")
    monkeypatch.setenv("BRIDGE_DRY_RUN", "false")
    monkeypatch.setattr(main, "order_capture_enabled", lambda: False)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(
        main,
        "_decision",
        lambda value, **_: DraftDecision(DecisionAction.TRIAGE, "low_confidence", classification),
    )

    def draft_reply(request):
        captured["category"] = request.category
        return SimpleNamespace(unusable_reason=None, draft_body="Draft", via_template=None, model="m")

    monkeypatch.setattr("dewie_brain.drafter.draft_reply", draft_reply)

    assert main.process_message(message()) is True
    assert captured["category"] == "GENERAL"
    [(_, note)] = client.notes
    assert note.startswith("**Dewie's doubts**")
    assert "`low_confidence`" in note
    assert "0.62/0.70" in note
    assert "**Dewie draft**" in note and note.endswith("Draft")


def test_confident_draft_carries_no_doubts(monkeypatch):
    client = NoteClient()
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "false")
    monkeypatch.setenv("BRIDGE_DRY_RUN", "false")
    monkeypatch.setattr(main, "order_capture_enabled", lambda: False)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(
        main,
        "_decision",
        lambda value, **_: DraftDecision(DecisionAction.DRAFT, "drafted", category="GENERAL"),
    )
    monkeypatch.setattr(
        "dewie_brain.drafter.draft_reply",
        lambda request: SimpleNamespace(unusable_reason=None, draft_body="Draft", via_template=None, model="m"),
    )

    assert main.process_message(message()) is True
    [(_, note)] = client.notes
    assert note.startswith("**Dewie draft**")


def test_unusable_draft_says_so_instead_of_going_silent(monkeypatch):
    client = NoteClient()
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "false")
    monkeypatch.setenv("BRIDGE_DRY_RUN", "false")
    monkeypatch.setattr(main, "order_capture_enabled", lambda: False)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(
        main,
        "_decision",
        lambda value, **_: DraftDecision(DecisionAction.SKIP, "system_sender_localpart"),
    )
    monkeypatch.setattr(
        "dewie_brain.drafter.draft_reply",
        lambda request: SimpleNamespace(unusable_reason="no customer question", draft_body=""),
    )

    # False keeps the claim released, so the label can be re-applied to retry.
    assert main.process_message(message()) is False
    [(_, note)] = client.notes
    assert note.startswith("**Dewie could not draft this** (no customer question)")


def test_shadow_draft_never_imports_drafter(monkeypatch):
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "true")
    monkeypatch.setattr(
        main,
        "_decision",
        lambda value, **_: DraftDecision(DecisionAction.DRAFT, "drafted", category="GENERAL"),
    )
    monkeypatch.setitem(sys.modules, "dewie_brain.drafter", None)

    main.process_message(message())


def test_shadow_metrics_capture_policy_evidence_without_drafting(monkeypatch):
    classification = Classification(
        actor=Actor.CUSTOMER,
        intent=Intent.ACCESS_SUPPORT,
        actor_confidence=0.94,
        intent_confidence=0.82,
        provider="fake",
        model="classifier-test",
    )
    monkeypatch.setattr(main, "_metrics", Counter())
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "true")
    monkeypatch.setattr(
        main,
        "_decision",
        lambda value, **_: DraftDecision(
            DecisionAction.DRAFT,
            "drafted",
            classification=classification,
            category="GENERAL",
        ),
    )

    main.process_message(message())

    assert main._metrics == Counter({
        "decision_draft": 1,
        "reason_drafted": 1,
        "actor_customer": 1,
        "intent_access_support": 1,
        "actor_confidence_high": 1,
        "intent_confidence_medium": 1,
        "classifier_provider_fake": 1,
        "drafter_calls_avoided": 1,
        "drafter_calls_avoided_shadow": 1,
    })


def test_draft_request_includes_extracted_attachment_text(monkeypatch):
    captured = {}
    attachment = ParsedAttachment("application/pdf", "order.pdf", "https://desk/order.pdf")
    value = message()
    value.attachments = [attachment]
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "false")
    monkeypatch.setenv("BRIDGE_DRY_RUN", "true")
    monkeypatch.setattr(
        main,
        "_decision",
        lambda message, **_: DraftDecision(DecisionAction.DRAFT, "drafted", category="GENERAL"),
    )
    monkeypatch.setattr(main, "extract_attachment_text", lambda values, logger: "PDF text")

    def draft_reply(request):
        captured["request"] = request
        return SimpleNamespace(unusable_reason=None, draft_body="Draft")

    monkeypatch.setattr("dewie_brain.drafter.draft_reply", draft_reply)

    main.process_message(value)

    assert captured["request"].image_text == "PDF text"


def api_message(message_id, body):
    return {
        "id": message_id,
        "account_id": 1,
        "inbox_id": 3,
        "message_type": 0,
        "private": False,
        "content": body,
        "sender": {"type": "contact", "email": "person@example.com"},
        "content_attributes": {"email": {"subject": "Question"}},
    }


class CommandStore:
    def __init__(self):
        self.claimed = set()
        self.released = []
        self.recorded = []

    def record_inbound(self, value):
        self.recorded.append(value.message_id)
        return True

    def claim(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def release(self, key):
        self.released.append(key)
        self.claimed.discard(key)


class CommandClient:
    def __init__(self, messages, remove_ok=True):
        self.messages = messages
        self.remove_ok = remove_ok
        self.removed = []

    def get_conversation(self, conversation_id):
        return ConversationResult(
            True,
            tuple(self.messages),
            {
                "contact": {"email": "person@example.com"},
                "additional_attributes": {"mail_subject": "Question"},
            },
            200,
            "fetched",
        )

    def remove_label(self, conversation_id, label):
        self.removed.append((conversation_id, label))
        return LabelResult(self.remove_ok, (), 200 if self.remove_ok else 500)


def test_relabeling_same_inbound_does_not_redraft(monkeypatch):
    store = CommandStore()
    client = CommandClient([api_message(42, "First")])
    drafted = []
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(main, "process_message", lambda value: drafted.append(value.message_id) or True)

    main.process_label_command(7, 1, 42)
    main.process_label_command(7, 1, 42)

    assert drafted == [42]
    assert client.removed == [(7, "dewie-draft")]
    assert store.claimed == {"conversation:7:message:42:action:draft"}


def test_two_successive_customer_messages_each_require_and_receive_fresh_command(monkeypatch):
    store = CommandStore()
    client = CommandClient([api_message(42, "First")])
    drafted = []
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(main, "process_message", lambda value: drafted.append(value.message_id) or True)

    main.process_label_command(7, 1, 42)
    client.messages.append(api_message(43, "Second"))
    main.process_label_command(7, 1, 43)

    assert drafted == [42, 43]
    assert client.removed == [(7, "dewie-draft"), (7, "dewie-draft")]
    assert store.claimed == {
        "conversation:7:message:42:action:draft",
        "conversation:7:message:43:action:draft",
    }


def test_failed_draft_releases_action_for_a_fresh_human_command(monkeypatch):
    store = CommandStore()
    client = CommandClient([api_message(42, "First")])
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(main, "process_message", lambda value: False)

    main.process_label_command(7, 1, 42)

    assert store.claimed == set()
    assert store.released == ["conversation:7:message:42:action:draft"]
    assert client.removed == []


def test_posted_draft_stays_deduplicated_when_label_removal_fails(monkeypatch):
    store = CommandStore()
    client = CommandClient([api_message(42, "First")], remove_ok=False)
    drafted = []
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(main, "process_message", lambda value: drafted.append(value.message_id) or True)

    main.process_label_command(7, 1, 42)
    main.process_label_command(7, 1, 42)

    assert drafted == [42]
    assert store.claimed == {"conversation:7:message:42:action:draft"}


def test_later_reply_cannot_ride_an_older_label_command(monkeypatch):
    store = CommandStore()
    client = CommandClient([api_message(42, "First"), api_message(43, "Later")])
    drafted = []
    monkeypatch.setattr(main, "dedup_store", lambda: store)
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(main, "process_message", lambda value: drafted.append(value.message_id) or True)

    main.process_label_command(7, 1, 42)

    assert drafted == [42]

import sys
from collections import Counter
from types import SimpleNamespace

import main
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
    assert decision.reason_code == "system_sender"


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


def test_triage_path_never_imports_drafter(monkeypatch):
    monkeypatch.setattr(
        main,
        "_decision",
        lambda value: DraftDecision(DecisionAction.TRIAGE, "low_confidence"),
    )
    monkeypatch.setitem(sys.modules, "dewie_brain.drafter", None)

    main.process_message(message())


def test_shadow_draft_never_imports_drafter(monkeypatch):
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "true")
    monkeypatch.setattr(
        main,
        "_decision",
        lambda value: DraftDecision(DecisionAction.DRAFT, "drafted", category="GENERAL"),
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
        lambda value: DraftDecision(
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
        lambda message: DraftDecision(DecisionAction.DRAFT, "drafted", category="GENERAL"),
    )
    monkeypatch.setattr(main, "extract_attachment_text", lambda values, logger: "PDF text")

    def draft_reply(request):
        captured["request"] = request
        return SimpleNamespace(unusable_reason=None, draft_body="Draft")

    monkeypatch.setattr("dewie_brain.drafter.draft_reply", draft_reply)

    main.process_message(value)

    assert captured["request"].image_text == "PDF text"

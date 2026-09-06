import sys

import main
from dewie_brain.desk import DecisionAction, DraftDecision
from parser import ParsedMessage


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

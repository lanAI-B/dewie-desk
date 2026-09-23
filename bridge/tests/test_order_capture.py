"""Order capture on the Chatwoot path (BRIDGE_ORDER_CAPTURE).

No Chatwoot, model, store database or Cloud SQL is reached: the capture call,
the drafter and the Chatwoot client are fakes, and the end-to-end case runs the
real DewieOps capture against a temp SQLite store with the model replaced.
"""

import ast
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy

import main
from conftest import DEWIEOPS
from dewie_brain.desk import DecisionAction, DraftDecision
from parser import ParsedAttachment, ParsedMessage

BRIDGE = Path(__file__).resolve().parents[1]


def message():
    return ParsedMessage(
        event="message_created",
        account_id=1,
        conversation_id=7,
        message_id=42,
        from_email="coord@example.com",
        subject="Order",
        body="Please send 2 FAM manuals.",
        message_type="incoming",
        sender_type="contact",
        should_process=True,
    )


def decide(category):
    return lambda value: DraftDecision(DecisionAction.DRAFT, "drafted", category=category)


class FakeClient:
    account_id = "1"

    def __init__(self, ok=True):
        self.ok = ok
        self.notes = []

    def post_private_note(self, conversation_id, body):
        self.notes.append((conversation_id, body))
        return SimpleNamespace(ok=self.ok, status_code=200 if self.ok else 500, detail="x")


RESULT = SimpleNamespace(packet_id=17, created=True, order_type="NEW_CORPORATE", store="abs",
                         filename="f.json", open_questions=("confirm cost center",))


@pytest.fixture
def live(monkeypatch):
    """Shadow and dry-run off, capture on, every outward call recorded."""
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "false")
    monkeypatch.setenv("BRIDGE_DRY_RUN", "false")
    monkeypatch.setenv("BRIDGE_ORDER_CAPTURE", "true")
    monkeypatch.setattr(main, "_metrics", Counter())
    client = FakeClient()
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(main, "extract_attachment_text", lambda values, logger: "")
    rec = SimpleNamespace(client=client, captures=[], drafts=[], capture_result=RESULT)

    def capture_order(req):
        rec.captures.append(req)
        if isinstance(rec.capture_result, Exception):
            raise rec.capture_result
        return rec.capture_result

    def draft_reply(request):
        rec.drafts.append(request)
        return SimpleNamespace(unusable_reason=None, draft_body="Draft", via_template=None,
                               model="m")

    monkeypatch.setattr("dewie_brain.order_capture.capture_order", capture_order)
    monkeypatch.setattr("dewie_brain.drafter.draft_reply", draft_reply)
    return rec


def test_capture_is_off_by_default(monkeypatch):
    monkeypatch.delenv("BRIDGE_ORDER_CAPTURE", raising=False)
    assert main.order_capture_enabled() is False
    assert main.health()["order_capture"] is False


def test_flag_off_orders_draft_as_before_and_never_load_capture(live, monkeypatch):
    monkeypatch.setenv("BRIDGE_ORDER_CAPTURE", "false")
    monkeypatch.setattr(main, "_decision", decide("ORDER"))
    monkeypatch.setitem(sys.modules, "dewie_brain.order_capture", None)
    assert main.process_message(message()) is True
    assert len(live.drafts) == 1 and live.captures == []


def test_shadow_mode_never_captures(live, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "true")
    monkeypatch.setattr(main, "_decision", decide("ORDER"))
    monkeypatch.setitem(sys.modules, "dewie_brain.order_capture", None)
    assert main.process_message(message()) is False
    assert live.captures == [] and live.drafts == []


@pytest.mark.parametrize("category", ["GENERAL", "RETURN"])
def test_non_order_lanes_are_not_captured(live, monkeypatch, category):
    monkeypatch.setattr(main, "_decision", decide(category))
    main.process_message(message())
    assert live.captures == [] and len(live.drafts) == 1


@pytest.mark.parametrize("category", ["ORDER", "PG"])
def test_captured_order_posts_a_capture_note_instead_of_a_draft(live, monkeypatch, category):
    monkeypatch.setattr(main, "_decision", decide(category))
    assert main.process_message(message()) is True
    assert live.drafts == []
    (req,) = live.captures
    assert (req.category, req.account_id, req.conversation_id, req.message_id) == \
        (category, 1, 7, 42)
    (conversation, note), = live.client.notes
    assert conversation == 7
    assert "packet #17" in note and "NEW_CORPORATE" in note and "confirm cost center" in note
    assert "Nothing has been placed" in note
    assert main._metrics["order_packets_stored"] == 1


def test_dry_run_captures_but_posts_nothing(live, monkeypatch):
    monkeypatch.setenv("BRIDGE_DRY_RUN", "true")
    monkeypatch.setattr(main, "_decision", decide("ORDER"))
    assert main.process_message(message()) is False  # command not consumed
    assert len(live.captures) == 1 and live.client.notes == [] and live.drafts == []


def test_an_order_question_falls_through_to_a_draft(live, monkeypatch):
    live.capture_result = None
    monkeypatch.setattr(main, "_decision", decide("ORDER"))
    assert main.process_message(message()) is True
    assert len(live.captures) == 1 and len(live.drafts) == 1
    assert live.client.notes[0][1].startswith("**Dewie draft**")


def test_capture_failure_falls_back_to_a_draft(live, monkeypatch):
    live.capture_result = RuntimeError("cloud sql down")
    monkeypatch.setattr(main, "_decision", decide("ORDER"))
    assert main.process_message(message()) is True
    assert len(live.drafts) == 1
    assert main._metrics["order_capture_failed"] == 1


def test_failed_capture_note_does_not_consume_the_command(live, monkeypatch):
    live.client.ok = False
    monkeypatch.setattr(main, "_decision", decide("ORDER"))
    assert main.process_message(message()) is False
    assert live.drafts == []


def test_attachments_are_extracted_once_and_passed_to_capture(live, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "extract_attachment_text",
                        lambda values, logger: calls.append(values) or "PDF: 2 manuals")
    live.capture_result = None
    value = message()
    value.attachments = [ParsedAttachment("application/pdf", "po.pdf", "https://desk/po.pdf")]
    monkeypatch.setattr(main, "_decision", decide("ORDER"))
    main.process_message(value)
    assert len(calls) == 1
    assert live.captures[0].image_text == "PDF: 2 manuals"
    assert live.drafts[0].image_text == "PDF: 2 manuals"


def test_end_to_end_packet_matches_the_order_queue_file(monkeypatch, tmp_path):
    """Real DewieOps capture, temp SQLite store, model and lookups replaced."""
    from dewie_brain import order_capture
    from dewie_brain.db.schema import order_packets as table
    from dewie_brain.drafter import core
    from dewie_brain.order_packets import OrderPacketStore, export_pending
    from dewie_brain.order_spec import load_spec

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'packets.db'}")
    table.metadata.create_all(engine, tables=[table])
    store = OrderPacketStore(engine)
    monkeypatch.setattr(order_capture, "OrderPacketStore", lambda: store)
    monkeypatch.setattr(core, "_gather_context", lambda *a: {"customer": None, "kb": []})
    monkeypatch.setattr(core, "_extract_order_lines", lambda *a: {
        "is_order": True, "lines": [{"product": "FAM manual", "quantity": 2, "sku": None}]})
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "false")
    monkeypatch.setenv("BRIDGE_DRY_RUN", "false")
    monkeypatch.setenv("BRIDGE_ORDER_CAPTURE", "true")
    client = FakeClient()
    monkeypatch.setattr(main, "chatwoot_client", lambda: client)
    monkeypatch.setattr(main, "_decision", decide("ORDER"))
    monkeypatch.setitem(sys.modules, "dewie_brain.drafter.service", None)  # no drafting

    assert main.process_message(message()) is True
    assert main.process_message(message()) is True  # relabel: same packet, no second row
    assert len(store.pending()) == 1
    assert all("Dewie order capture" in body for _, body in client.notes)

    queue = tmp_path / "order_queue"
    queue.mkdir()
    (out,) = export_pending(store, queue)
    spec = load_spec(Path(out.path))
    assert spec.stage == "captured" and spec.order_type == "NEW_INDIVIDUAL"
    assert spec.source["chatwoot"] == {"account_id": 1, "conversation_id": 7, "message_id": 42}
    assert json.loads(Path(out.path).read_text(encoding="utf-8"))["lines"][0]["quantity"] == 2


def test_bridge_imports_only_modules_that_exist_in_dewieops():
    missing = []
    for path in BRIDGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for mod in mods:
                if not mod.startswith("dewie_brain"):
                    continue
                rel = DEWIEOPS / Path(*mod.split("."))
                if not (rel.with_suffix(".py").exists() or (rel / "__init__.py").exists()):
                    missing.append(f"{path.name}: {mod}")
    assert missing == []

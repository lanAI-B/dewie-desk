"""#2350: an inline image Chatwoot keeps only as an <img> in the stored email body.

The shape is verbatim from Chatwoot message 9105 (conversation 2350, 2026-11-02):
``attachments`` null, ``content`` the text only, ``html_content.full`` empty, and
the grade report present only as an ActiveStorage <img> in
``text_content.full``, with ``number_of_attachments`` 1.
"""

from types import SimpleNamespace

from conftest import require_dewieops

require_dewieops()

import attachments
import main
from attachments import DownloadedAttachment
from dewie_brain.desk import DecisionAction, DraftDecision
from parser import ParsedAttachment, parse_message_created

BLOB = ("/rails/active_storage/blobs/redirect/eyJfcmFpbHMiOnsibWVzc2FnZSI6IkJBaHBBblFEIiwiZXhw"
        "IjpudWxsLCJwdXIiOiJibG9iX2lkIn19--fcea04f42a26ca91a22dc63b57f62fa3cf2fcba4/image0.jpeg")
TEXT = "Hi,\nI﻿ would like to reactive my ASM study manual for CAS Exam MAS ii. Thanks."


def payload_2350(text_full=None, html_full=None, declared=1, attachments_value=None):
    return {
        "event": "message_created",
        "id": 9105,
        "message_type": "incoming",
        "private": False,
        "content": TEXT,
        "content_type": "incoming_email",
        "attachments": attachments_value,
        "content_attributes": {"email": {
            "subject": "ASM study manual",
            "multipart": True,
            "number_of_attachments": declared,
            "html_content": {"full": html_full},
            "text_content": {
                "full": text_full if text_full is not None else (
                    f'{TEXT}\n\n<img src="https://desk.example.cc{BLOB}" alt="image0.jpeg">'),
                "reply": TEXT,
                "quoted": TEXT,
            },
        }},
        "sender": {"email": "person@example.com", "type": "contact"},
        "conversation": {"id": 2350, "channel": "Channel::Email"},
        "inbox": {"id": 3, "name": "Support"},
        "account": {"id": 1},
    }


def test_inline_image_is_found_as_a_hostless_blob_path():
    parsed = parse_message_created(payload_2350())

    assert parsed.should_process
    assert parsed.body == TEXT
    assert parsed.declared_attachments == 1
    assert parsed.attachments == [ParsedAttachment("image", "image0.jpeg", BLOB)]


def test_html_and_text_copies_of_one_image_count_once_and_foreign_hosts_are_kept_out():
    html = (f'<p>hi</p><img src="https://desk.example.cc{BLOB}">'
            '<img src="https://tracker.example/pixel.gif">'
            '<img src="cid:ii_abc">')
    parsed = parse_message_created(payload_2350(html_full=html))

    assert [a.data_url for a in parsed.attachments] == [BLOB]


def test_inline_images_are_capped():
    imgs = "".join(f'<img src="/rails/active_storage/blobs/redirect/x{i}--y/p{i}.png">'
                   for i in range(6))
    parsed = parse_message_created(payload_2350(text_full=imgs, declared=6))

    assert len(parsed.attachments) == 3


def test_real_attachment_is_not_duplicated_by_its_inline_copy():
    real = [{"file_type": "image", "file_name": "image0.jpeg",
             "data_url": "https://desk.example.cc/real/image0.jpeg"}]
    parsed = parse_message_created(payload_2350(attachments_value=real))

    assert [a.data_url for a in parsed.attachments] == ["https://desk.example.cc/real/image0.jpeg"]


def test_blob_path_is_fetched_from_the_bridges_own_chatwoot(monkeypatch):
    monkeypatch.setenv("CHATWOOT_BASE_URL", "http://127.0.0.1:3000")
    assert attachments.resolve_attachment_url(BLOB) == "http://127.0.0.1:3000" + BLOB
    assert attachments.resolve_attachment_url("https://x/y.png") == "https://x/y.png"


def test_extraction_carries_received_and_unreadable_counts(monkeypatch):
    monkeypatch.setattr("dewie_brain.drafter._transcribe_images",
                        lambda message: "[Image: image0.jpeg]\nOFFICIAL GRADE REPORT")
    text = attachments.extract_attachment_text(
        [ParsedAttachment("image", "image0.jpeg", BLOB),
         ParsedAttachment("file", "notes.docx", "https://desk/notes.docx")],
        downloader=lambda url: DownloadedAttachment(b"x", "image/jpeg" if "jpeg" in url
                                                    else "application/msword"),
    )

    assert "OFFICIAL GRADE REPORT" in text
    assert (text.received, text.unreadable) == (2, 1)


def test_oversized_image_is_labelled_not_transcribed(monkeypatch):
    called = []
    monkeypatch.setattr("dewie_brain.drafter._transcribe_images",
                        lambda message: called.append(message) or "")
    text = attachments.extract_attachment_text(
        [ParsedAttachment("image", "huge.png", BLOB)],
        downloader=lambda url: DownloadedAttachment(
            b"x" * (attachments.MAX_IMAGE_BYTES + 1), "image/png"),
    )

    assert not called
    assert "[Image: huge.png - too large to read]" in text
    assert (text.received, text.unreadable) == (1, 1)


def _draft_request_for(monkeypatch, message_value, attachment_text):
    captured = {}
    monkeypatch.setenv("BRIDGE_SHADOW_MODE", "false")
    monkeypatch.setenv("BRIDGE_DRY_RUN", "true")
    monkeypatch.setattr(main, "_decision", lambda message, **_: DraftDecision(
        DecisionAction.DRAFT, "drafted", category="GENERAL"))
    monkeypatch.setattr(main, "extract_attachment_text", lambda values, logger: attachment_text)

    def draft_reply(request):
        captured["request"] = request
        return SimpleNamespace(unusable_reason=None, draft_body="Draft")

    monkeypatch.setattr("dewie_brain.drafter.draft_reply", draft_reply)
    main.process_message(message_value)
    return captured["request"]


def test_2350_shape_reaches_the_drafter_with_its_attachment(monkeypatch):
    from dewie_brain.drafter import AttachmentTranscript

    request = _draft_request_for(
        monkeypatch, parse_message_created(payload_2350()),
        AttachmentTranscript("[Image: image0.jpeg]\nOFFICIAL GRADE REPORT", 1, 0))

    assert request.attachments_received == 1
    assert request.attachments_unreadable == 0
    assert "OFFICIAL GRADE REPORT" in request.image_text


def test_declared_attachment_chatwoot_never_exposed_is_counted_unreadable(monkeypatch):
    """number_of_attachments 1, no <img>, attachments null: the drafter still
    learns an attachment arrived and that it cannot see it."""
    parsed = parse_message_created(payload_2350(text_full=TEXT))
    assert parsed.attachments == []

    request = _draft_request_for(monkeypatch, parsed, "")

    assert request.attachments_received == 1
    assert request.attachments_unreadable == 1


def test_2350_end_to_end_skips_the_closing_template(monkeypatch):
    """Through the real draft_reply: the template must not fire; the full
    drafter runs (stubbed) and is told about the attachment."""
    seen = {}

    def fake_generate(*args, **kwargs):
        seen["received"] = kwargs.get("received_attachments")
        return "Hi,\n\nDraft.\n\nDewie | ActuarialBookstore Support", "", "test-model"

    monkeypatch.setattr("dewie_brain.drafter.service._generate_draft", fake_generate)
    monkeypatch.setattr("dewie_brain.drafter.service._gather_context",
                        lambda *a, **k: {"kb": []})
    from dewie_brain.drafter import DraftRequest, draft_reply

    parsed = parse_message_created(payload_2350(text_full=TEXT))
    result = draft_reply(DraftRequest(
        category="GENERAL", from_email=parsed.from_email, subject=parsed.subject,
        body=parsed.body, image_text="", mm=object(),
        **main._attachment_fields(DraftRequest, parsed, "")))

    assert result.via_template is None
    assert seen["received"] == 1

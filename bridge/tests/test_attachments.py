from types import SimpleNamespace

import attachments
from attachments import DownloadedAttachment
from parser import ParsedAttachment


def ref(name, file_type="file"):
    return ParsedAttachment(file_type, name, f"https://desk.example/{name}")


def test_pdf_text_is_extracted_and_redacted(monkeypatch):
    monkeypatch.setattr(attachments, "_pdf_text", lambda data: "card 4111111111111111")

    text = attachments.extract_attachment_text(
        [ref("receipt.pdf", "application/pdf")],
        downloader=lambda url: DownloadedAttachment(b"pdf", "application/pdf"),
    )

    assert "[PDF: receipt.pdf]" in text
    assert "4111111111111111" not in text


def test_image_uses_canonical_drafter_vision_path(monkeypatch):
    seen = {}

    def transcribe(message):
        seen["attachments"] = message.attachments
        return "[Image: slip.png]\nPASS"

    monkeypatch.setattr("dewie_brain.drafter._transcribe_images", transcribe)

    text = attachments.extract_attachment_text(
        [ref("slip.png", "image")],
        downloader=lambda url: DownloadedAttachment(b"png", "image/png"),
    )

    assert text.endswith("PASS")
    assert seen["attachments"][0].payload == b"png"
    assert seen["attachments"][0].content_type == "image/png"


def test_audio_is_labeled_without_transcription(monkeypatch):
    monkeypatch.setattr(
        "dewie_brain.drafter._transcribe_images",
        lambda message: (_ for _ in ()).throw(AssertionError("vision must not run")),
    )

    text = attachments.extract_attachment_text(
        [ref("voicemail.m4a", "audio")],
        downloader=lambda url: DownloadedAttachment(b"audio", "audio/mp4"),
    )

    assert text == "[Audio: voicemail.m4a - not transcribed]"


def test_download_failure_is_visible_but_nonfatal():
    def fail(url):
        raise RuntimeError("network down")

    text = attachments.extract_attachment_text([ref("order.pdf")], downloader=fail)

    assert text == "[Attachment: order.pdf - extraction failed]"

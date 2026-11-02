"""Bounded attachment retrieval and text extraction for Chatwoot messages."""

from __future__ import annotations

import io
import logging
import mimetypes
import os
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse

import requests

from parser import ParsedAttachment


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 25
MAX_EXTRACTED_CHARS = 12_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the vision input limit; larger is labelled, not sent


@dataclass(frozen=True)
class DownloadedAttachment:
    data: bytes
    content_type: str = ""


def _max_bytes() -> int:
    try:
        configured = int(os.environ.get("BRIDGE_ATTACHMENT_MAX_BYTES", DEFAULT_MAX_BYTES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return max(1, min(configured, 25 * 1024 * 1024))


def _chatwoot_base() -> str:
    return (
        os.environ.get("CHATWOOT_BASE_URL")
        or os.environ.get("CHATWOOT_API_URL")
        or "http://127.0.0.1:3000"
    )


def resolve_attachment_url(url: str) -> str:
    """An inline image arrives as a host-less ActiveStorage path (parser._inline_images);
    it is always fetched from the bridge's own Chatwoot, never a host the email named."""
    if url.startswith("/"):
        return urljoin(_chatwoot_base().rstrip("/") + "/", url.lstrip("/"))
    return url


def download_attachment(url: str, *, timeout: int = 20) -> DownloadedAttachment:
    """Download one signed Chatwoot attachment URL without forwarding API secrets."""
    url = resolve_attachment_url(url)
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("attachment URL must use http or https")

    limit = _max_bytes()
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        length = response.headers.get("content-length")
        if length and int(length) > limit:
            raise ValueError("attachment exceeds configured byte limit")
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > limit:
                raise ValueError("attachment exceeds configured byte limit")
            chunks.append(chunk)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        return DownloadedAttachment(b"".join(chunks), content_type)


def _mime_type(attachment: ParsedAttachment, downloaded: DownloadedAttachment) -> str:
    response_type = downloaded.content_type.strip().lower()
    if "/" in response_type:
        return response_type
    declared = attachment.file_type.strip().lower()
    if "/" in declared:
        return declared.split(";", 1)[0]
    guessed, _ = mimetypes.guess_type(attachment.name)
    return (guessed or declared).lower()


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    used = 0
    for page in reader.pages[:MAX_PDF_PAGES]:
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        remaining = MAX_EXTRACTED_CHARS - used
        if remaining <= 0:
            break
        parts.append(text[:remaining])
        used += len(parts[-1])
    return "\n\n".join(parts)


def extract_attachment_text(
    attachments: list[ParsedAttachment],
    *,
    downloader=download_attachment,
    logger: logging.Logger | None = None,
) -> str:
    """Return redacted PDF/image text plus explicit labels for unhandled audio.

    The result is the drafter's AttachmentTranscript (a str) carrying how many
    attachments arrived and how many could not be read, so the drafter can tell
    "nothing attached" from "attached but unreadable" (#2709, #2350).
    """
    logger = logger or logging.getLogger("dewie-desk-bridge.attachments")
    results = []
    images = []
    unreadable = 0

    for attachment in attachments:
        label = attachment.name or attachment.file_type or "attachment"
        if not attachment.data_url:
            results.append(f"[Attachment: {label} - unavailable; no data URL]")
            unreadable += 1
            continue
        try:
            downloaded = downloader(attachment.data_url)
            mime = _mime_type(attachment, downloaded)
            if mime == "application/pdf" or attachment.name.lower().endswith(".pdf"):
                text = _pdf_text(downloaded.data)
                results.append(
                    f"[PDF: {label}]\n{text}"
                    if text else f"[PDF: {label} - no extractable text]"
                )
                unreadable += 0 if text else 1
            elif (mime in IMAGE_TYPES or attachment.file_type == "image") and (
                    len(downloaded.data) > MAX_IMAGE_BYTES):
                results.append(f"[Image: {label} - too large to read]")
                unreadable += 1
            elif mime in IMAGE_TYPES or attachment.file_type == "image":
                images.append(SimpleNamespace(
                    content_type=mime if mime in IMAGE_TYPES else "image/jpeg",
                    payload=downloaded.data,
                    filename=attachment.name,
                ))
            elif mime.startswith("audio/") or attachment.file_type == "audio":
                results.append(f"[Audio: {label} - not transcribed]")
                unreadable += 1
            else:
                results.append(f"[Attachment: {label} - unsupported type; not transcribed]")
                unreadable += 1
        except Exception as exc:  # an attachment failure must not suppress the email draft
            logger.warning("attachment extraction failed name=%r error=%s", label, type(exc).__name__)
            results.append(f"[Attachment: {label} - extraction failed]")
            unreadable += 1

    if images:
        from dewie_brain.drafter import _transcribe_images

        image_text = _transcribe_images(SimpleNamespace(attachments=images))
        unreadable += getattr(image_text, "unreadable", 0)
        if image_text:
            results.append(image_text)

    from dewie_brain.drafter import _redact_card_data

    text = _redact_card_data("\n\n".join(results))
    try:
        from dewie_brain.drafter import AttachmentTranscript
    except ImportError:  # an older DewieOps checkout: plain text, as before
        return text
    return AttachmentTranscript(text, received=len(attachments), unreadable=unreadable)

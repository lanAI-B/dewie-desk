"""Bounded attachment retrieval and text extraction for Chatwoot messages."""

from __future__ import annotations

import io
import logging
import mimetypes
import os
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlparse

import requests

from parser import ParsedAttachment


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 25
MAX_EXTRACTED_CHARS = 12_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}


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


def download_attachment(url: str, *, timeout: int = 20) -> DownloadedAttachment:
    """Download one signed Chatwoot attachment URL without forwarding API secrets."""
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
    """Return redacted PDF/image text plus explicit labels for unhandled audio."""
    logger = logger or logging.getLogger("dewie-desk-bridge.attachments")
    results = []
    images = []

    for attachment in attachments:
        label = attachment.name or attachment.file_type or "attachment"
        if not attachment.data_url:
            results.append(f"[Attachment: {label} - unavailable; no data URL]")
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
            elif mime in IMAGE_TYPES or attachment.file_type == "image":
                images.append(SimpleNamespace(
                    content_type=mime if mime in IMAGE_TYPES else "image/jpeg",
                    payload=downloaded.data,
                    filename=attachment.name,
                ))
            elif mime.startswith("audio/") or attachment.file_type == "audio":
                results.append(f"[Audio: {label} - not transcribed]")
            else:
                results.append(f"[Attachment: {label} - unsupported type; not transcribed]")
        except Exception as exc:  # an attachment failure must not suppress the email draft
            logger.warning("attachment extraction failed name=%r error=%s", label, type(exc).__name__)
            results.append(f"[Attachment: {label} - extraction failed]")

    if images:
        from dewie_brain.drafter import _transcribe_images

        image_text = _transcribe_images(SimpleNamespace(attachments=images))
        if image_text:
            results.append(image_text)

    from dewie_brain.drafter import _redact_card_data

    return _redact_card_data("\n\n".join(results))

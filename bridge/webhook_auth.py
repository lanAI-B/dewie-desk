"""Verify the HMAC Chatwoot places on webhook request bytes."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Mapping

SIGNATURE_HEADER = "x-chatwoot-signature"
TIMESTAMP_HEADER = "x-chatwoot-timestamp"
MAX_SKEW_SECONDS = 300


@dataclass(frozen=True)
class AuthVerdict:
    ok: bool
    mode: str
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.mode == "verified"


def configured_secret() -> str | None:
    secret = (os.environ.get("CHATWOOT_WEBHOOK_SECRET") or "").strip()
    return secret or None


def is_enforced() -> bool:
    return configured_secret() is not None


def expected_signature(raw: bytes, secret: str, timestamp: str) -> str:
    signed = timestamp.encode("utf-8") + b"." + raw
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def sign_headers(raw: bytes, secret: str, timestamp: int | None = None) -> dict[str, str]:
    value = str(int(time.time() if timestamp is None else timestamp))
    return {
        TIMESTAMP_HEADER: value,
        SIGNATURE_HEADER: expected_signature(raw, secret, value),
    }


def verify(
    raw: bytes,
    headers: Mapping[str, str],
    *,
    now: float | None = None,
) -> AuthVerdict:
    """Return an authentication verdict without raising on hostile input."""
    try:
        secret = configured_secret()
        if secret is None:
            return AuthVerdict(True, "unenforced", "webhook_secret_not_configured")

        lookup = {str(key).lower(): str(value) for key, value in dict(headers).items()}
        signature = lookup.get(SIGNATURE_HEADER, "").strip()
        timestamp = lookup.get(TIMESTAMP_HEADER, "").strip()
        if not signature:
            return AuthVerdict(False, "rejected", "missing_signature")
        if not timestamp:
            return AuthVerdict(False, "rejected", "missing_timestamp")

        expected = expected_signature(raw, secret, timestamp)
        if not hmac.compare_digest(
            signature.encode("utf-8", "surrogateescape"), expected.encode("ascii")
        ):
            return AuthVerdict(False, "rejected", "signature_mismatch")

        try:
            sent_at = int(timestamp)
        except ValueError:
            return AuthVerdict(False, "rejected", "invalid_timestamp")
        skew = (time.time() if now is None else now) - sent_at
        if abs(skew) > MAX_SKEW_SECONDS:
            return AuthVerdict(False, "rejected", "stale_timestamp")
        return AuthVerdict(True, "verified")
    except Exception:
        return AuthVerdict(False, "rejected", "verification_error")

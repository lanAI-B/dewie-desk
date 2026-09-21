"""Authenticated, idempotent transport for customer-visible Chatwoot replies.

A trusted internal caller names an existing conversation, the exact rendered
message, a stable idempotency key, and who/what is sending it. The bridge claims
the key durably before calling Chatwoot and reports exactly one of:

- ``accepted``: Chatwoot created the public message; replays return it unchanged.
- ``rejected``: no message can exist; the same key may be submitted again.
- ``unknown``: a message may exist; the key is frozen for human reconciliation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from typing import Annotated, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from state import DedupStore, OutboundRecord

TOKEN_ENV = "BRIDGE_OUTBOUND_TOKEN"
MIN_TOKEN_LENGTH = 32
# Secrets with another purpose must never double as the outbound credential.
_OTHER_SECRETS = ("CHATWOOT_WEBHOOK_SECRET", "CHATWOOT_API_TOKEN")
_BEARER = re.compile(r"Bearer ([^\s]+)")

HTTP_STATUS = {"accepted": 200, "rejected": 502, "unknown": 504}

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class OutboundRequest(BaseModel):
    """The whole caller contract; unknown fields such as ``private`` are refused."""

    model_config = ConfigDict(extra="forbid", strict=True)

    conversation_id: int = Field(gt=0)
    content: str = Field(min_length=1, max_length=20000)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._\-]{0,199}$")
    actor: Label
    source: Label


@dataclass(frozen=True)
class AuthVerdict:
    ok: bool
    status_code: int = 200
    reason: str = ""


def configured_token() -> str | None:
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    return token or None


def configuration_problem() -> str | None:
    """Return why outbound is disabled, or None when its credential is usable."""
    token = configured_token()
    if token is None:
        return "outbound_not_configured"
    if len(token) < MIN_TOKEN_LENGTH:
        return "outbound_token_too_short"
    for name in _OTHER_SECRETS:
        other = (os.environ.get(name) or "").strip()
        if other and hmac.compare_digest(other.encode(), token.encode()):
            return "outbound_token_reuses_other_secret"
    return None


def authorize(headers: Mapping[str, str]) -> AuthVerdict:
    """Fail closed: no usable configured token means no caller is authorized."""
    problem = configuration_problem()
    if problem:
        return AuthVerdict(False, 503, problem)
    match = _BEARER.fullmatch((headers.get("authorization") or "").strip())
    if not match:
        return AuthVerdict(False, 401, "missing_bearer_token")
    expected = configured_token().encode("utf-8")
    if not hmac.compare_digest(match.group(1).encode("utf-8", "surrogateescape"), expected):
        return AuthVerdict(False, 401, "invalid_bearer_token")
    return AuthVerdict(True)


def parse_request(payload: object) -> OutboundRequest:
    """Validate a decoded JSON body; raises ``ValueError`` with a short reason."""
    try:
        request = OutboundRequest.model_validate(payload)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) or "body" for error in exc.errors()})
        raise ValueError("invalid_request: " + ", ".join(fields)) from None
    if not request.content.strip():
        raise ValueError("invalid_request: content")
    return request


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _response(record: OutboundRecord, *, replayed: bool) -> tuple[int, dict]:
    status = record.status
    detail = record.detail
    if status == "pending":
        # Another attempt is in flight or died after claiming; either way a
        # message may exist, so the caller must not treat this as retryable.
        status, detail = "unknown", "claim_pending_outcome_unknown"
    return HTTP_STATUS[status], {
        "status": status,
        "idempotency_key": record.idempotency_key,
        "conversation_id": record.conversation_id,
        "chatwoot_message_id": record.chatwoot_message_id,
        "http_status": record.http_status,
        "detail": detail,
        "attempts": record.attempts,
        "replayed": replayed,
        "retry_safe": status == "rejected",
    }


class IdempotencyConflict(Exception):
    pass


def deliver(store: DedupStore, client, request: OutboundRequest) -> tuple[int, dict]:
    """Claim, send at most once, and durably record the outcome."""
    verdict, record = store.begin_outbound(
        request.idempotency_key,
        request.conversation_id,
        content_digest(request.content),
        request.actor,
        request.source,
    )
    if verdict == "conflict":
        raise IdempotencyConflict("idempotency_key_reused_for_different_message")
    if verdict == "replay":
        return _response(record, replayed=True)

    try:
        result = client.post_public_outgoing(request.conversation_id, request.content)
        outcome, message_id = result.outcome, result.message_id
        http_status, detail = result.status_code, result.detail
    except Exception as exc:
        outcome, message_id, http_status = "unknown", None, None
        detail = f"transport_error {type(exc).__name__}: {exc}"
    # If recording fails the claim stays pending, which replays as unknown.
    record = store.finish_outbound(
        request.idempotency_key,
        outcome,
        chatwoot_message_id=message_id,
        http_status=http_status,
        detail=detail[:500],
    )
    return _response(record, replayed=False)

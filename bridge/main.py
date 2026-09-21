"""Chatwoot transport adapter for DewieOps desk decisions and private drafts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import Counter
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from chatwoot import ChatwootClient
from attachments import extract_attachment_text
from parser import (
    ParsedMessage,
    draft_label_added,
    newest_customer_message,
    parse_message_created,
)
from state import DedupStore
import outbound
import webhook_auth

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dewie-desk-bridge")

app = FastAPI(title="dewie-desk-bridge")

_client: ChatwootClient | None = None
_runtime = None
_state: DedupStore | None = None
_metrics: Counter = Counter()
_metrics_lock = threading.Lock()
DRAFT_LABEL = "dewie-draft"


def _enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def shadow_mode() -> bool:
    return _enabled("BRIDGE_SHADOW_MODE", True)


def dry_run() -> bool:
    return _enabled("BRIDGE_DRY_RUN", True)


def _increment(name: str) -> None:
    with _metrics_lock:
        _metrics[name] += 1


def _confidence_band(value: float) -> str:
    if value >= 0.9:
        return "high"
    if value >= 0.75:
        return "medium"
    return "low"


def chatwoot_client() -> ChatwootClient:
    global _client
    if _client is None:
        _client = ChatwootClient(
            base_url=os.environ.get("CHATWOOT_BASE_URL")
            or os.environ.get("CHATWOOT_API_URL")
            or "http://127.0.0.1:3000",
            account_id=os.environ.get("CHATWOOT_ACCOUNT_ID", "1"),
            api_token=os.environ.get("CHATWOOT_API_TOKEN", ""),
        )
    return _client


def classifier_runtime():
    global _runtime
    if _runtime is None:
        from dewie_brain.llm_models import UTILITY
        from dewie_brain.model_runtime import create_model_runtime

        provider = os.environ.get("DESK_CLASSIFIER_PROVIDER", "anthropic").strip().lower()
        configured_model = (os.environ.get("DESK_CLASSIFIER_MODEL") or "").strip()
        model = configured_model or (UTILITY if provider == "anthropic" else None)
        _runtime = create_model_runtime(provider=provider, model=model)
    return _runtime


def dedup_store() -> DedupStore:
    global _state
    if _state is None:
        default = Path(__file__).resolve().parent.parent / "data" / "bridge-state.sqlite3"
        configured = (os.environ.get("BRIDGE_STATE_DB") or "").strip()
        _state = DedupStore(configured or default)
    return _state


def _command_key(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8", "surrogateescape"
    )
    conversation_id = payload.get("id") or 0
    return f"conversation:{conversation_id}:label-add:{hashlib.sha256(canonical).hexdigest()}"


def _draft_action_key(message: ParsedMessage) -> str:
    return (
        f"conversation:{message.conversation_id or 0}:"
        f"message:{message.message_id or 0}:action:draft"
    )


def _decision(message: ParsedMessage):
    from dewie_brain.desk import (
        classify_message,
        decide_draft,
        is_system_sender,
        looks_forwarded,
    )

    if is_system_sender(message.from_email):
        return decide_draft(from_email=message.from_email, classification=None)

    forwarded = looks_forwarded(message.subject, message.body)
    domain = message.from_email.rsplit("@", 1)[-1].lower()
    hints = []
    if domain in {"actuarialbookstore.com", "actexlearning.com"}:
        hints.append("sender is on an internal company domain")
    if forwarded:
        hints.append("message contains recognizable forwarded-mail evidence")

    try:
        _increment("classifier_calls")
        classification = classify_message(
            classifier_runtime(),
            from_email=message.from_email,
            subject=message.subject,
            body=message.body,
            hints=hints,
        )
    except Exception as exc:
        log.warning(
            "classification failed conversation=%s message=%s error=%s",
            message.conversation_id,
            message.message_id,
            type(exc).__name__,
        )
        classification = None

    return decide_draft(
        from_email=message.from_email,
        classification=classification,
        confidence_floor=float(os.environ.get("DESK_CLASSIFIER_CONFIDENCE_FLOOR", "0.75")),
        is_forwarded=forwarded,
        text=f"{message.subject}\n{message.body}",
    )


def _private_note(result, decision) -> str:
    classification = decision.classification
    evidence = ""
    if classification:
        evidence = (
            f"; {classification.actor.value} x {classification.intent.value}; "
            f"confidence {classification.actor_confidence:.2f}/{classification.intent_confidence:.2f}; "
            f"{classification.provider or 'unknown'}/{classification.model or 'unknown'}"
        )
    return (
        f"**Dewie draft** (private; {result.via_template or result.model or 'model'}{evidence})\n\n"
        f"{result.draft_body}"
    )


def process_message(message: ParsedMessage) -> bool:
    """Run one requested draft action; true means a private note was posted."""
    decision = _decision(message)
    _increment(f"decision_{decision.action.value}")
    _increment(f"reason_{decision.reason_code}")
    classification = decision.classification
    if classification:
        _increment(f"actor_{classification.actor.value}")
        _increment(f"intent_{classification.intent.value}")
        _increment(f"actor_confidence_{_confidence_band(classification.actor_confidence)}")
        _increment(f"intent_confidence_{_confidence_band(classification.intent_confidence)}")
        _increment(f"classifier_provider_{classification.provider or 'unknown'}")

    if not decision.should_draft:
        _increment("drafter_calls_avoided")
        log.info(
            "desk decision=%s reason=%s conversation=%s message=%s",
            decision.action.value,
            decision.reason_code,
            message.conversation_id,
            message.message_id,
        )
        return False
    if shadow_mode():
        _increment("drafter_calls_avoided")
        _increment("drafter_calls_avoided_shadow")
        log.info(
            "desk shadow decision=draft conversation=%s message=%s",
            message.conversation_id,
            message.message_id,
        )
        return False

    from dewie_brain.drafter import DraftRequest, draft_reply

    _increment("drafter_calls")
    try:
        attachment_text = extract_attachment_text(message.attachments, logger=log)
        result = draft_reply(DraftRequest(
            category=decision.category or "GENERAL",
            from_email=message.from_email,
            subject=message.subject,
            body=message.body,
            image_text=attachment_text,
        ))
    except Exception as exc:
        _increment("draft_failed")
        log.exception(
            "draft failed conversation=%s message=%s error=%s",
            message.conversation_id,
            message.message_id,
            type(exc).__name__,
        )
        return False
    if result.unusable_reason or not result.draft_body:
        _increment("draft_unusable")
        return False
    if dry_run():
        _increment("notes_avoided_dry_run")
        return False

    posted = chatwoot_client().post_private_note(
        int(message.conversation_id), _private_note(result, decision)
    )
    if posted.ok:
        _increment("private_notes_posted")
        return True
    else:
        _increment("note_post_failed")
        log.error(
            "private note failed conversation=%s message=%s status=%s detail=%s",
            message.conversation_id,
            message.message_id,
            posted.status_code,
            posted.detail,
        )
        return False


def process_label_command(
    conversation_id: int,
    account_id: int,
    through_message_id: int,
) -> None:
    """Fetch current state and execute one draft command against its newest inbound."""
    conversation = chatwoot_client().get_conversation(conversation_id)
    if not conversation.ok:
        _increment("conversation_fetch_failed")
        log.error(
            "draft command fetch failed conversation=%s status=%s detail=%s",
            conversation_id,
            conversation.status_code,
            conversation.detail,
        )
        return

    message = newest_customer_message(
        list(conversation.messages),
        conversation_id=conversation_id,
        account_id=account_id,
        meta=conversation.meta,
        through_message_id=through_message_id,
    )
    if message is None:
        _increment("draft_command_no_customer_message")
        return

    dedup_store().record_inbound(message)
    action_key = _draft_action_key(message)
    if not dedup_store().claim(action_key):
        _increment("duplicate_draft_action")
        return

    _increment("draft_commands")
    if not process_message(message):
        dedup_store().release(action_key)
        return

    consumed = chatwoot_client().remove_label(conversation_id, DRAFT_LABEL)
    if consumed.ok:
        _increment("draft_commands_consumed")
    else:
        _increment("draft_command_consume_failed")
        log.error(
            "draft posted but command label removal failed conversation=%s status=%s detail=%s",
            conversation_id,
            consumed.status_code,
            consumed.detail,
        )


@app.get("/health")
def health() -> dict:
    with _metrics_lock:
        counts = dict(_metrics)
    return {
        "status": "ok",
        "service": "dewie-desk-bridge",
        "shadow_mode": shadow_mode(),
        "dry_run": dry_run(),
        "webhook_auth": "enforced" if webhook_auth.is_enforced() else "unenforced",
        "outbound_auth": outbound.configuration_problem() or "configured",
        "chatwoot_configured": bool(os.environ.get("CHATWOOT_API_TOKEN", "").strip()),
        "counts": counts,
    }


@app.post("/internal/chatwoot/outbound-message")
async def chatwoot_outbound_message(request: Request) -> JSONResponse:
    """Send one customer-visible reply on an existing conversation, at most once."""
    verdict = outbound.authorize(request.headers)
    if not verdict.ok:
        _increment("outbound_auth_rejected")
        raise HTTPException(status_code=verdict.status_code, detail=verdict.reason)

    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        _increment("outbound_invalid_json")
        raise HTTPException(status_code=400, detail="invalid_json")
    try:
        parsed = outbound.parse_request(payload)
    except ValueError as exc:
        _increment("outbound_invalid_request")
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        status_code, result = await run_in_threadpool(
            outbound.deliver, dedup_store(), chatwoot_client(), parsed
        )
    except outbound.IdempotencyConflict as exc:
        _increment("outbound_idempotency_conflict")
        raise HTTPException(status_code=409, detail=str(exc))

    _increment(f"outbound_{'replayed' if result['replayed'] else 'attempted'}_{result['status']}")
    log.info(
        "outbound key=%s conversation=%s status=%s replayed=%s attempts=%s message=%s "
        "http=%s actor=%s source=%s",
        parsed.idempotency_key,
        parsed.conversation_id,
        result["status"],
        result["replayed"],
        result["attempts"],
        result["chatwoot_message_id"],
        result["http_status"],
        parsed.actor,
        parsed.source,
    )
    return JSONResponse(status_code=status_code, content=result)


@app.post("/chatwoot/webhook")
@app.post("/webhook")
async def chatwoot_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    raw = await request.body()
    verdict = webhook_auth.verify(raw, request.headers)
    if not verdict.ok:
        _increment("webhooks_rejected")
        raise HTTPException(status_code=401, detail=verdict.reason)
    _increment("webhooks_verified" if verdict.verified else "webhooks_unenforced")

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _increment("transport_invalid_json")
        raise HTTPException(status_code=400, detail="invalid_json")

    event = payload.get("event") if isinstance(payload, dict) else None
    if event == "message_created":
        message = parse_message_created(payload)
        if not message.should_process:
            _increment("transport_filtered")
            _increment(f"reason_{message.skip_reason}")
            return {"accepted": False, "reason": message.skip_reason}
        if not dedup_store().record_inbound(message):
            _increment("duplicate_message")
            return {"accepted": False, "reason": "duplicate_message"}
        _increment("inbound_recorded")
        return {
            "accepted": True,
            "action": "recorded",
            "conversation": message.conversation_id,
            "message": message.message_id,
        }

    if event == "conversation_updated":
        conversation_id = draft_label_added(payload, DRAFT_LABEL)
        if conversation_id is None:
            _increment("transport_filtered")
            _increment("reason_no_draft_label_added")
            return {"accepted": False, "reason": "no_draft_label_added"}
        through_message_id = dedup_store().latest_inbound_id(conversation_id)
        if through_message_id is None:
            _increment("draft_command_no_recorded_inbound")
            return {"accepted": False, "reason": "no_recorded_inbound"}
        if not dedup_store().claim(_command_key(payload)):
            _increment("duplicate_label_command")
            return {"accepted": False, "reason": "duplicate_label_command"}
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        try:
            account_id = int(account.get("id") or chatwoot_client().account_id)
        except (TypeError, ValueError):
            account_id = 0
        background_tasks.add_task(
            process_label_command,
            conversation_id,
            account_id,
            through_message_id,
        )
        return {
            "accepted": True,
            "action": "draft_requested",
            "conversation": conversation_id,
        }

    _increment("transport_filtered")
    _increment("reason_unsupported_event")
    return {"accepted": False, "reason": "unsupported_event"}

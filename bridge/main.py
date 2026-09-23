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
from conv_memory_sync import ConvMemoryWriter
import sent_copy
from attachments import extract_attachment_text
from parser import (
    ParsedMessage,
    draft_label_added,
    newest_customer_message,
    parse_message_created,
)
from state import DedupStore
import conversations
import outbound
import spam
import webhook_auth

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dewie-desk-bridge")

app = FastAPI(title="dewie-desk-bridge")

_client: ChatwootClient | None = None
_runtime = None
_state: DedupStore | None = None
_metrics: Counter = Counter()
_metrics_lock = threading.Lock()
_memory_writer: ConvMemoryWriter | None = None
DRAFT_LABEL = "dewie-draft"


def _enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def shadow_mode() -> bool:
    return _enabled("BRIDGE_SHADOW_MODE", True)


def dry_run() -> bool:
    return _enabled("BRIDGE_DRY_RUN", True)


def order_capture_enabled() -> bool:
    """Capture ORDER/PG messages as order packets (DewieOps Cloud SQL). Off by default."""
    return _enabled("BRIDGE_ORDER_CAPTURE", False)


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


def conv_memory_writer() -> ConvMemoryWriter:
    global _memory_writer
    if _memory_writer is None:
        _memory_writer = ConvMemoryWriter()
    return _memory_writer


def process_sent_message(sent: sent_copy.SentMessage) -> None:
    """Copy one Chatwoot-sent reply to the Sent folder and/or conv_memory."""
    outcome = sent_copy.process_sent(
        sent,
        store=dedup_store(),
        client=chatwoot_client(),
        writer_factory=conv_memory_writer,
    )
    for destination, result in outcome.items():
        _increment(f"{destination}_{result}")


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


def _capture_order(message: ParsedMessage, decision, attachment_text: str):
    """Capture an actionable order as a DewieOps order packet, or return None.

    None means "draft a reply instead": the lane is not an order lane, the message is
    an order question with nothing to place, or capture failed. Failing open to a draft
    matches the IMAP runner; an order can be answered late but is never dropped.
    """
    from dewie_brain.order_capture import ORDER_CATEGORIES, CaptureRequest, capture_order

    if decision.category not in ORDER_CATEGORIES:
        return None
    _increment("order_capture_calls")
    try:
        result = capture_order(CaptureRequest(
            category=decision.category,
            from_email=message.from_email,
            subject=message.subject,
            body=message.body,
            account_id=int(message.account_id or chatwoot_client().account_id or 0),
            conversation_id=int(message.conversation_id or 0),
            message_id=int(message.message_id or 0),
            image_text=attachment_text or "",
        ))
    except Exception as exc:
        _increment("order_capture_failed")
        log.error(
            "order capture failed conversation=%s message=%s error=%s",
            message.conversation_id,
            message.message_id,
            type(exc).__name__,
        )
        return None
    if result is None:
        _increment("order_capture_not_an_order")
        return None
    _increment("order_packets_stored" if result.created else "order_packets_replayed")
    # No customer data in the log: the packet id and the Chatwoot ids identify it.
    log.info(
        "order captured packet=%s created=%s type=%s conversation=%s message=%s",
        result.packet_id,
        result.created,
        result.order_type,
        message.conversation_id,
        message.message_id,
    )
    return result


def _capture_note(result) -> str:
    lines = [
        f"**Dewie order capture** (private; packet #{result.packet_id}, "
        f"{result.order_type}, store {result.store or 'unknown'})",
        "",
        "Captured for attended order processing. Nothing has been placed and no reply "
        "was drafted; the reply goes out once the order is processed.",
    ]
    if result.open_questions:
        lines += ["", "Open questions:"] + [f"- {q}" for q in result.open_questions]
    return "\n".join(lines)


def _report_capture(message: ParsedMessage, result) -> bool:
    """Post the capture as a private note; true consumes the draft command."""
    if dry_run():
        _increment("notes_avoided_dry_run")
        return False
    posted = chatwoot_client().post_private_note(
        int(message.conversation_id), _capture_note(result)
    )
    if posted.ok:
        _increment("order_capture_notes_posted")
        return True
    _increment("note_post_failed")
    log.error(
        "order capture note failed conversation=%s message=%s status=%s detail=%s",
        message.conversation_id,
        message.message_id,
        posted.status_code,
        posted.detail,
    )
    return False


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

    attachment_text = None
    if order_capture_enabled():
        attachment_text = extract_attachment_text(message.attachments, logger=log)
        captured = _capture_order(message, decision, attachment_text)
        if captured is not None:
            return _report_capture(message, captured)

    from dewie_brain.drafter import DraftRequest, draft_reply

    _increment("drafter_calls")
    try:
        if attachment_text is None:
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


def _spam_classify(message: ParsedMessage):
    """Classifier stage for spam screening; raising means "no evidence"."""
    from dewie_brain.desk import classify_message

    _increment("spam_classifier_calls")
    return classify_message(
        classifier_runtime(),
        from_email=message.from_email,
        subject=message.subject,
        body=message.body,
        hints=[],
    )


def screen_inbound(message: ParsedMessage) -> str:
    """Spam/noise screen for one newly recorded inbound message. Never drafts."""
    try:
        outcome = spam.screen(message, chatwoot_client(), classify=_spam_classify)
    except Exception as exc:  # screening must never break inbound recording
        outcome = "error"
        log.exception(
            "spam screen failed conversation=%s message=%s error=%s",
            message.conversation_id,
            message.message_id,
            type(exc).__name__,
        )
    _increment(f"spam_{outcome}")
    return outcome


@app.get("/health")
def health() -> dict:
    with _metrics_lock:
        counts = dict(_metrics)
    return {
        "status": "ok",
        "service": "dewie-desk-bridge",
        "shadow_mode": shadow_mode(),
        "dry_run": dry_run(),
        "sent_copy": sent_copy.status(),
        "order_capture": order_capture_enabled(),
        "webhook_auth": "enforced" if webhook_auth.is_enforced() else "unenforced",
        "outbound_auth": outbound.configuration_problem() or "configured",
        "spam_autoresolve": spam.status(),
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

    inboxes = conversations.configured_inboxes()
    if not inboxes:
        # The recipient check needs to know which email inboxes are legitimate.
        raise HTTPException(status_code=503, detail="outbound_inbox_not_configured")
    try:
        status_code, result = await run_in_threadpool(
            outbound.deliver, dedup_store(), chatwoot_client(), parsed,
            frozenset(inboxes.values()),
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


@app.post("/internal/chatwoot/resolve-conversation")
async def chatwoot_resolve_conversation(request: Request) -> JSONResponse:
    """Find the customer's conversation in the email inbox, or open an empty one."""
    verdict = outbound.authorize(request.headers)
    if not verdict.ok:
        _increment("resolve_auth_rejected")
        raise HTTPException(status_code=verdict.status_code, detail=verdict.reason)
    inboxes = conversations.configured_inboxes()
    if not inboxes:
        raise HTTPException(status_code=503, detail="outbound_inbox_not_configured")
    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json")
    try:
        parsed = conversations.parse_request(payload)
    except ValueError as exc:
        _increment("resolve_invalid_request")
        raise HTTPException(status_code=422, detail=str(exc))

    # Route by store: the reply-to a customer answers must be their own shop's.
    inbox_id = inboxes.get(parsed.store)
    if inbox_id is None:
        raise HTTPException(status_code=503, detail="outbound_inbox_not_configured")
    status_code, result = await run_in_threadpool(
        conversations.resolve, chatwoot_client(), parsed, inbox_id)
    _increment(f"resolve_{result['status']}")
    # No email in the log: the conversation and contact ids identify it.
    log.info("resolve status=%s conversation=%s contact=%s store=%s inbox=%s actor=%s source=%s",
             result["status"], result.get("conversation_id"), result.get("contact_id"),
             parsed.store, inbox_id, parsed.actor, parsed.source)
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
    if event in sent_copy.SENT_EVENTS:
        # An outgoing public reply Chatwoot has actually delivered (source_id set,
        # which in v4.16.1 happens on message_updated). Incoming messages fall
        # through to the draft path below unchanged.
        sent, reason = sent_copy.screen_sent(payload)
        if sent is not None:
            if not sent_copy.any_enabled():
                _increment("reason_sent_copy_disabled")
                return {"accepted": False, "reason": "sent_copy_disabled"}
            background_tasks.add_task(process_sent_message, sent)
            _increment("sent_copy_scheduled")
            return {
                "accepted": True,
                "action": "sent_copy_scheduled",
                "conversation": sent.conversation_id,
                "message": sent.message_id,
            }
        if event == "message_updated":
            _increment("transport_filtered")
            _increment(f"reason_{reason}")
            return {"accepted": False, "reason": reason}

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
        response = {
            "accepted": True,
            "action": "recorded",
            "conversation": message.conversation_id,
            "message": message.message_id,
        }
        if spam.enabled():
            background_tasks.add_task(screen_inbound, message)
            response["spam_screen"] = "dry_run" if spam.dry_run() else "queued"
        return response

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

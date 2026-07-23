"""dewie-desk-bridge — the only new code in this repo.

Hears Chatwoot webhooks, runs classify -> enrich -> draft using the CANONICAL
drafter imported from the DewieBrain repo (never copied), and posts the result as
a PRIVATE NOTE. It has no drafting logic of its own. SEND stays a human click.

Guardrails (Phase 1/2 pilot):
  - private notes only; never sends a customer-facing reply
  - BRIDGE_DRY_RUN=true logs the draft and posts nothing
  - skips outgoing/agent/activity messages and obvious system senders
"""
from __future__ import annotations

import logging
import os
import sys

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dewie-desk-bridge")

# Load the repo-root .env (one level up from bridge/) so config + secrets are
# present whether launched from bridge/ or the repo root.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

# ── Reference the DewieBrain package; do NOT vendor it. ───────────────────────
_BRAIN = os.environ.get("DEWIE_BRAIN_PATH")
if _BRAIN and _BRAIN not in sys.path:
    sys.path.insert(0, _BRAIN)
    # The brain carries its own secrets (Anthropic key, DB creds). Load them too,
    # WITHOUT overriding the bridge's own vars (load_dotenv won't clobber existing).
    try:
        from dotenv import load_dotenv as _ld
        _ld(os.path.join(_BRAIN, ".env"))
    except ImportError:
        pass

from chatwoot import ChatwootClient  # noqa: E402  (local module)

DRY_RUN = os.environ.get("BRIDGE_DRY_RUN", "true").lower() != "false"

app = FastAPI(title="dewie-desk-bridge")

_client: ChatwootClient | None = None


def client() -> ChatwootClient:
    global _client
    if _client is None:
        _client = ChatwootClient(
            base_url=os.environ["CHATWOOT_API_URL"],
            account_id=os.environ.get("CHATWOOT_ACCOUNT_ID", "1"),
            api_token=os.environ["CHATWOOT_API_TOKEN"],
        )
    return _client


def _classify(subject: str, body: str) -> str:
    """TODO(Phase 2): wire the real desk classifier (actor x intent taxonomy).
    Until then the drafter's own category-agnostic path handles it."""
    return "general"


@app.get("/health")
def health() -> dict:
    return {"ok": True, "dry_run": DRY_RUN, "brain_path": _BRAIN}


@app.post("/webhook")
async def webhook(req: Request) -> dict:
    payload = await req.json()
    event = payload.get("event")

    # Only act on a fresh inbound customer message.
    if event != "message_created" or payload.get("message_type") != "incoming":
        return {"skipped": f"event={event} type={payload.get('message_type')}"}

    conv = payload.get("conversation") or {}
    conv_id = conv.get("id") or payload.get("conversation_id")
    sender = payload.get("sender") or {}
    from_email = (sender.get("email") or "").strip()
    body = payload.get("content") or ""
    subject = (conv.get("additional_attributes") or {}).get("mail_subject") or ""

    if not (conv_id and from_email and body.strip()):
        return {"skipped": "missing conv_id / sender email / body"}

    # Import here so the app still boots for /health if DewieBrain isn't on the path.
    from dewie_brain.drafter import draft_reply, DraftRequest  # noqa: E402

    result = draft_reply(DraftRequest(
        category=_classify(subject, body),
        from_email=from_email,
        subject=subject,
        body=body,
    ))

    if result.unusable_reason:
        log.warning("conv %s: unusable draft (%s) — leaving for manual triage",
                    conv_id, result.unusable_reason)
        return {"conversation": conv_id, "drafted": False,
                "reason": result.unusable_reason}

    note = (f"**Dewie draft** (via {result.via_template or 'Opus'}) — "
            f"reply to {result.customer_email}, subj: {result.reply_subject}\n\n"
            f"{result.draft_body}")

    if DRY_RUN:
        log.info("[DRY RUN] conv %s draft:\n%s", conv_id, note)
        return {"conversation": conv_id, "drafted": True, "posted": False}

    client().post_private_note(int(conv_id), note)
    return {"conversation": conv_id, "drafted": True, "posted": True}

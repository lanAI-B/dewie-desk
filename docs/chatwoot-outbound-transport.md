# Chatwoot outbound transport — consumer contract

The bridge can post one **customer-visible** reply on an **existing** Chatwoot
conversation for a trusted internal caller (first consumer: a later DewieOps
refund notification). Chatwoot delivers it through the conversation's inbox, so
the conversation stays the thread and audit surface. No SMTP/IMAP credentials
leave Chatwoot.

The human draft path is unchanged: `post_private_note` still hard-codes
`private: true`. The public path is a separate method, `post_public_outgoing`,
that hard-codes `private: false`. Neither takes a visibility flag, and the
endpoint refuses any request field other than the five below.

## Enable

Set `BRIDGE_OUTBOUND_TOKEN` in the bridge environment to a random value of at
least 32 characters. Until then the endpoint answers `503` and never touches
state or Chatwoot. The token must differ from `CHATWOOT_WEBHOOK_SECRET` and
`CHATWOOT_API_TOKEN`; reuse is refused with `503`. A valid Chatwoot webhook
signature does not authorize this endpoint.

`BRIDGE_SHADOW_MODE` and `BRIDGE_DRY_RUN` govern only the draft path. The
outbound token is the outbound switch: unset it to disable sending.

`GET /health` reports `outbound_auth` as `configured` or the reason it is
disabled; it never echoes the token.

## Request

```http
POST /internal/chatwoot/outbound-message
Authorization: Bearer <BRIDGE_OUTBOUND_TOKEN>
Content-Type: application/json

{
  "conversation_id": 45,
  "content": "Your refund of $12.00 was issued today.",
  "idempotency_key": "refund:123",
  "actor": "lana",
  "source": "dewieops-refund-button"
}
```

| Field | Rule |
| --- | --- |
| `conversation_id` | JSON integer > 0; an existing conversation in the configured account. No addressing by email. |
| `content` | Final rendered text, 1–20000 characters, not blank. Sent byte-for-byte. |
| `idempotency_key` | 1–200 of `A-Z a-z 0-9 : . _ -`, starting alphanumeric. Derive it from the business event, e.g. `refund:<id>`, and reuse it on every retry. |
| `actor` | Who initiated the send (person or service), 1–200 characters. |
| `source` | Which system/feature sent it, 1–200 characters. |

Errors before any claim or Chatwoot call: `401` (missing/wrong bearer),
`503` (outbound disabled/misconfigured), `400` (not JSON), `422` (contract
violation, including extra fields such as `private`).

`409 idempotency_key_reused_for_different_message`: the key already belongs to a
different conversation or content hash. Nothing is sent. Fix the caller; do not
mint a new key to force the send.

## Outcomes

Every claimed request returns this body:

```json
{
  "status": "accepted",
  "idempotency_key": "refund:123",
  "conversation_id": 45,
  "chatwoot_message_id": 501,
  "http_status": 200,
  "detail": "created",
  "attempts": 1,
  "replayed": false,
  "retry_safe": false
}
```

| `status` | HTTP | Meaning | Caller action |
| --- | --- | --- | --- |
| `accepted` | 200 | Chatwoot returned a created message ID. | Done. Resubmitting the key returns the same record (`replayed: true`) without calling Chatwoot. |
| `rejected` | 502 | Definitive: Chatwoot answered 400/401/403/404/405/409/413/415/422/429, or the connection failed before the request could be sent (connect timeout, refused, invalid URL). No message exists. | `retry_safe: true`. Fix the cause and resubmit the **same** key; that becomes attempt N+1. |
| `unknown` | 504 | The request may have reached Chatwoot: read timeout, dropped connection, 408/5xx, 2xx without a message ID, an unexpected transport error, or an outcome that could not be recorded (`detail: outcome_not_recorded`), or an earlier attempt that is still in flight or died after claiming (`detail: claim_pending_outcome_unknown`). | **Do not resend.** The key is frozen; every resubmission returns `unknown` without calling Chatwoot. A human checks the conversation. |

Always branch on `status`, not the HTTP code alone.

## Durable state

State lives in the bridge SQLite database (`BRIDGE_STATE_DB`):

- `outbound_message` — one row per key: conversation, SHA-256 of the content,
  latest actor/source, `pending|accepted|rejected|unknown`, attempt count,
  Chatwoot message ID, Chatwoot HTTP status, detail, timestamps. The row is
  written as `pending` and committed **before** Chatwoot is called.
- `outbound_attempt` — append-only history per attempt with its actor, source,
  outcome, and start/finish times.

Message content is stored only as its hash. `detail` is never Chatwoot response
text or an exception message, because either can echo the message. Client
details pass through an explicit allowlist; everything else, including
code-shaped strings such as a reference or card number, is stored and returned
as `detail_withheld`. The allowlist is:

- exactly `created`, `accepted_without_message_id`, `invalid_conversation_id`,
  or `empty_content`;
- `http_<status>` with a three-digit status from 100–599;
- `request_error:<ErrorClass>` where `<ErrorClass>` is a class defined in
  `requests.exceptions`, or `ConnectionRefused`.

The bridge itself adds `transport_error:<ErrorClass>` (the Python class name of
an unexpected client failure), `outcome_not_recorded`, and
`claim_pending_outcome_unknown`; these never carry caller or Chatwoot text.

The claim uses a SQLite `BEGIN IMMEDIATE` transaction, so concurrent duplicate
requests (threads or processes sharing the file) produce one attempt. A failure
while claiming happens before any send and surfaces as an ordinary `500`; it is
not an ambiguous outcome. If the bridge dies between the Chatwoot call and
recording its outcome, the row stays `pending`, which replays as `unknown`. If
the outcome cannot be written after Chatwoot answered, that request gets a
`504 unknown` with `detail: outcome_not_recorded` and `retry_safe: false`; the
row stays `pending`, so every later replay is also `unknown` without a call.
The Chatwoot outcome and message ID from that attempt are logged (without
content) at error level for reconciliation.

## Reconciling `unknown`

1. Open the conversation in Chatwoot and look for an outgoing public message
   after the attempt's `started_at` whose text hashes to `content_sha256`.
2. There is no automated resolution in this slice. Recording a human
   reconciliation (marking a key accepted or releasing it) is future work for
   the refund-consumer slice to decide.

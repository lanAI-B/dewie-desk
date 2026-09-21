# Feature: safe Chatwoot outbound system-email transport

Status: PM changes requested (local implementation verified; not deployed)
Tracking: this plan only; source queue task #6176 was closed as transferred after
initial plan commit `48ccbe4`

## Location and agreement (PM owns)

- Repository: `C:\Users\Owner\source\repos\dewie-desk`
- Isolated worktree: `C:\Users\Owner\source\repos\dewie-desk-chatwoot-outbound`
- Feature branch: `feature/chatwoot-outbound-transport`
- Base revision: `65767552619d6483d9079212debe8d882bac8cd9`
  (`origin/feature/desk-bridge-modernization` at setup)
- PM agreement: Lana approved the Chatwoot system-email direction and local/QA
  testing on 2026-09-21. This run is local implementation only.
- Next authorized slice: implement and locally verify the narrow outbound transport.
- Stop after: verified code and this checkpoint are committed locally and ready
  for PM review.

## Desired behavior (PM owns)

Machine-generated customer email must travel through the existing Chatwoot
conversation rather than through general SMTP/IMAP credentials in Discord or a
worker. Chatwoot remains the thread and audit surface.

The first consumer will be a later refund-notification button, but this slice
builds only the reusable dewie-desk transport. A caller supplies an existing QA
conversation, a fixed rendered message, a stable idempotency key, and audit
identity. The transport either proves Chatwoot accepted one public outgoing
message, reports a definitive rejection, or reports an ambiguous/unknown result.
An ambiguous result must never trigger a blind resend.

Example: a later caller submits idempotency key `refund:123` for conversation 45.
Two identical requests produce at most one Chatwoot customer message. A timeout
after the API request returns `unknown`, not `failed_safe_to_retry` and not `sent`.

## Scope and exclusions (PM owns)

In scope:

- A narrow authenticated dewie-desk endpoint/service contract for public outgoing
  Chatwoot conversation messages.
- A distinct client method for public outgoing messages. Preserve the existing
  hard-coded private-note method; visibility must not be a caller-controlled flag.
- Durable idempotency state before the external call, with explicit accepted,
  definitively rejected, and ambiguous/unknown outcomes.
- Stable request/audit evidence sufficient for the later refund consumer to
  reconcile a retry without guessing.
- Synthetic/disposable tests and documentation of the consumer contract.

Implementation details such as the exact route name and small state-table shape
belong to the coder, provided they preserve this behavior. Use a distinct
fail-closed internal credential for the outbound endpoint; do not reuse a
customer-facing token or make the existing inbound webhook secret authorize
outbound sends by accident.

Excluded from this run:

- The DewieOps refund button or template.
- Any real email, including the linked personal QA inbox.
- Chatwoot/QA deployment, image build, service restart/recreate, live/shared-data
  mutation, production cutover, scheduler changes, merges, or pushes.
- General SMTP/IMAP abstraction work, automatic customer replies, or arbitrary
  recipient addressing. The endpoint requires an existing conversation.

## Acceptance criteria (PM owns)

- [x] Public outgoing and private-note operations are separate APIs with fixed
      visibility; existing private-note behavior remains green.
- [x] Missing/invalid internal authentication fails closed before a claim or
      Chatwoot API call.
- [x] The request requires a valid existing conversation ID, non-empty fixed
      content, stable idempotency key, and audit actor/source.
- [x] The durable claim precedes the Chatwoot call. A duplicate accepted request
      does not call Chatwoot again and returns the recorded accepted result.
- [x] A definitive pre-delivery/API rejection is distinguishable from an
      ambiguous timeout or connection loss after dispatch.
- [ ] Ambiguous state is durable and cannot be blindly retried into a duplicate.
- [x] Tests inspect the exact Chatwoot public-outgoing payload and prove no
      customer send occurs during the suite.
- [x] Relevant bridge tests and the broader bridge suite pass from the package
      root, or unrelated baseline failures are demonstrated precisely.
- [x] Only task-owned code/tests/docs are committed locally; no push or deployment.

## Implementation steps (coder maintains within agreed scope)

- [x] Verify worktree, branch, clean status, imports, and no active run marker.
- [x] Inspect the existing `ChatwootClient`, `DedupStore`, bridge configuration,
      webhook authentication, and tests before selecting the smallest compatible
      endpoint and state model.
- [x] Add failing contract/idempotency/error-shape tests before implementation.
- [x] Implement the local transport and durable outcome behavior.
- [x] Run focused tests, then `py -3.14 -m pytest tests -q` from `bridge`.
- [x] Run `git diff --check`, inspect scope, update this checkpoint, and commit
      verified code plus the plan locally.

## Verification

Run from `C:\Users\Owner\source\repos\dewie-desk-chatwoot-outbound\bridge`.
Tests must mock or use disposable local HTTP boundaries; they must not load a
real Chatwoot token or contact the linked QA inbox.

```powershell
py -3.14 -m pytest tests/test_chatwoot.py tests/test_state.py -q
py -3.14 -m pytest tests -q
git -C C:\Users\Owner\source\repos\dewie-desk-chatwoot-outbound diff --check
```

## Current checkpoint (coder maintains)

- Active run: none — manual Claude coder run of 2026-09-21 ended **ready for PM review**
- Observed state at start: worktree clean at `b0d76ff` on
  `feature/chatwoot-outbound-transport`; no active run marker; no `.env` present.
- Changes/commits: one local commit on this branch containing code, tests, docs,
  and this checkpoint (SHA reported in the handoff). Not pushed.
  - `bridge/chatwoot.py`: new `post_public_outgoing(conversation_id, content)`
    with hard-coded `{"message_type": "outgoing", "private": false}` returning
    `OutboundResult(outcome=accepted|rejected|unknown)`. `post_private_note`
    is unchanged. Neither method takes a visibility argument.
  - `bridge/state.py`: `outbound_message` (current state per key) and
    `outbound_attempt` (append-only history) tables; `begin_outbound` claims in
    a `BEGIN IMMEDIATE` transaction before any call; `finish_outbound` moves
    only a pending record to its final status.
  - `bridge/outbound.py` + `bridge/main.py`: `POST /internal/chatwoot/outbound-message`
    with `Authorization: Bearer $BRIDGE_OUTBOUND_TOKEN`. Fails closed (503) when
    unset, under 32 chars, or equal to `CHATWOOT_WEBHOOK_SECRET`/`CHATWOOT_API_TOKEN`;
    401 on missing/wrong bearer. Auth runs before body parsing, state, or Chatwoot.
    Strict body: `conversation_id`, `content`, `idempotency_key`, `actor`,
    `source`; extra fields (e.g. `private`, `email`) are 422. `/health` reports
    `outbound_auth` without the token.
  - `bridge/tests/conftest.py`: autouse guard removes live secrets from the
    environment and fails any test that reaches `requests`' real HTTP adapter,
    even if the caller swallows the error (verified with a throwaway probe test).
  - Docs: `docs/chatwoot-outbound-transport.md` (consumer contract),
    `.env.example`, `HOW_IT_WORKS.md`.
- Checks and results (from `bridge`, DewieOps sibling at `11d4fd5`):
  - `py -3.14 -m pytest tests/test_chatwoot.py tests/test_state.py -q`: 23 passed.
  - `... tests/test_outbound_endpoint.py` added to the focused set: 56 passed.
  - `py -3.14 -m pytest tests -q`: 91 passed, 1 failed. The failure is a
    **pre-existing baseline failure** also present before any edit (42 passed,
    1 failed at `b0d76ff`): `test_main.py::test_system_sender_skips_before_classifier_runtime_is_built`
    expects reason `system_sender` but DewieOps `11d4fd5` ("desk: distinguish
    deterministic and classified system mail") now returns
    `system_sender_localpart`. Unrelated to this slice; left untouched.
  - `git diff --check`: clean.
- Acceptance evidence, by criterion:
  - Separate fixed-visibility APIs: `test_public_outgoing_shape_is_hard_coded`,
    `test_public_and_private_operations_take_no_visibility_argument`, existing
    `test_private_note_shape_is_hard_coded`; `FakeChatwoot` fails on any private-note call.
  - Auth fails closed before claim/call: `test_missing_invalid_and_unconfigured_auth_fail_before_claim`,
    `test_outbound_token_must_not_reuse_other_bridge_secrets`,
    `test_webhook_signature_does_not_authorize_outbound` (store access raises).
  - Required fields: `test_invalid_request_is_rejected_before_claim` (20 cases),
    `test_malformed_json_is_rejected_before_claim`.
  - Claim precedes call; accepted replay: `test_claim_is_durable_before_chatwoot_is_called`
    (reads `pending` from SQLite during the call),
    `test_accepted_send_records_audit_and_duplicate_does_not_call_again`,
    `test_concurrent_outbound_claims_from_separate_stores_yield_one_attempt`.
  - Rejected vs unknown: client classification tests (4xx/connect-refused/
    connect-timeout/invalid URL = rejected; read timeout, dropped connection,
    408/5xx, 2xx without ID = unknown) and
    `test_definitive_rejection_is_distinct_and_same_key_may_retry`.
  - Durable unknown never resent: `test_timeout_after_dispatch_is_durable_unknown_and_not_resent`,
    `test_interrupted_pending_claim_replays_as_unknown_without_sending`,
    `test_unexpected_client_error_is_recorded_unknown`,
    `test_outbound_unknown_is_durable_and_never_reclaimed`.
  - Exact payload / no customer send: payload assertion above plus the conftest
    network guard.
- Routine engineering decisions made within scope (flag for PM review):
  - A definitive `rejected` result may be resubmitted with the **same** key; it
    becomes attempt N+1 and is recorded in `outbound_attempt`. `accepted`,
    `unknown`, and in-flight/interrupted `pending` keys never call Chatwoot again.
  - Reusing a key for a different conversation or content hash is `409` with no send.
  - HTTP mapping: accepted 200, rejected 502, unknown 504; callers must branch on
    the body's `status`.
  - The outbound endpoint is governed only by `BRIDGE_OUTBOUND_TOKEN`, not by
    `BRIDGE_DRY_RUN`/`BRIDGE_SHADOW_MODE` (those remain draft-path controls).
  - Content is stored only as a SHA-256 hash plus Chatwoot's message ID; content
    is capped at 20000 characters.
- Remaining work: none in this slice. Not done by design: refund consumer,
  QA/Chatwoot contact, credentials, deployment, and any tooling to record a
  human reconciliation of an `unknown` key (a decision for the consumer slice).
- Next step: PM reviews this commit against the acceptance criteria.
- Blocker or decision needed: none for this slice. Separately, the PM may want
  the DewieOps-driven `test_main.py` baseline failure fixed on the base branch.

## PM review

- Result: changes requested on `e21227b`; architecture accepted, one safety
  correction required before the ambiguous-outcome criterion can be accepted.
- Independent PM evidence (2026-09-21): focused suite `56 passed`; full bridge
  suite `91 passed, 1 failed`; `git diff --check` clean; worktree was clean and
  branch was one local commit ahead. The sole full-suite failure reproduced the
  reported sibling-DewieOps reason-code drift and is unrelated to this slice.
- Accepted decisions:
  - A definitively rejected attempt may reuse the same key as attempt N+1;
    accepted, unknown, and pending keys remain non-resendable.
  - Conversation/content reuse conflicts, token-only enablement, a 20000-character
    content cap, and hash-only message storage are appropriate for this transport.
  - Human reconciliation tooling is not required in this transport slice. The
    refund consumer must stop on `unknown`; reconciliation remains attended.
- Required correction:
  - Once Chatwoot has been called, failure to persist the final result must not
    escape as an ordinary unstructured 500. Return a structured, non-retryable
    `unknown` response for the current request while leaving the durable claim
    pending so every later replay is also frozen as `unknown`.
  - Do not persist Chatwoot response bodies in `detail`; an error response can
    echo customer content and would defeat the hash-only storage decision. Store
    bounded, non-content-bearing evidence such as the HTTP status and outcome.
  - Add focused tests proving both behaviors, including one where Chatwoot
    returns accepted and `finish_outbound` then fails. The fake failure must
    demonstrate a 504/body `status=unknown`, `retry_safe=false`, and a later
    replay that does not call Chatwoot.
- QA/deployment: not authorized by this plan; the linked personal QA inbox is
  reserved for the later attended QA prompt
- Proposed scope changes: none; this is a correction inside the existing
  ambiguous-outcome and hash-only audit contract.

## Manual Claude coder correction prompt

```text
Resume the manual feature-coder run for the PM correction. Read:
C:\Users\Owner\source\repos\_wt\brain-pm-focus-20260921\agentic\FEATURE_CODER.md
C:\Users\Owner\source\repos\dewie-desk-chatwoot-outbound\docs\feature-trial\plan.md

Work only in C:\Users\Owner\source\repos\dewie-desk-chatwoot-outbound on
feature/chatwoot-outbound-transport. The reviewed implementation commit is
e21227b. Verify the worktree/branch and no active run marker before editing.

Implement only the two required PM corrections recorded under `PM review`:
1. After the Chatwoot call has occurred, a failure to persist the final result
   must return a structured non-retryable unknown response (HTTP 504) for that
   same request, while preserving the pending claim so replays remain frozen and
   never call Chatwoot again. Do not convert pre-call database/claim failures into
   ambiguous sends.
2. Do not store Chatwoot response-body text in outbound `detail`; keep bounded,
   non-content-bearing status/outcome evidence so message content remains hash-only.

Add focused regression tests. At minimum, simulate Chatwoot returning accepted
and then make final-state persistence fail; prove the current response is 504
with status=unknown and retry_safe=false, and prove a later replay makes no
Chatwoot call. Add a test proving a 4xx body that echoes the submitted content is
not written to outbound_message or outbound_attempt.

Run the focused outbound tests, then the full bridge suite, then git diff --check.
Do not fix the unrelated sibling-DewieOps reason-code baseline failure in this
slice. Update the checkpoint and commit the correction locally. Do not push,
deploy, restart services, change credentials, contact Chatwoot, or send email.
Stop ready for PM re-review and report the commit SHA and exact test results.
```

## Manual Claude coder start prompt

```text
Act as the manual feature coder. Read:
C:\Users\Owner\source\repos\_wt\brain-pm-focus-20260921\agentic\FEATURE_CODER.md
C:\Users\Owner\source\repos\dewie-desk-chatwoot-outbound\docs\feature-trial\plan.md

Work only in C:\Users\Owner\source\repos\dewie-desk-chatwoot-outbound on
feature/chatwoot-outbound-transport. Verify the worktree, plan agreement, clean
status, and run ownership. I am manually initiating the authorized local
Chatwoot outbound-transport slice now.

Implement the plan's narrow authenticated public-outgoing conversation transport,
durable accepted/rejected/unknown idempotency behavior, and regression tests.
Preserve the hard-coded private-note path. Run the focused and full bridge tests,
update the plan checkpoint, and commit verified task-owned changes locally.
Make routine engineering decisions within the agreed behavior without waiting.

Stop ready for PM review. Do not implement the refund consumer, contact Chatwoot
QA, send email, load live credentials, push, merge, deploy, restart/recreate a
service, change shared data, use the task queue, or start another slice.
```

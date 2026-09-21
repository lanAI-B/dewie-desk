# Feature: safe Chatwoot outbound system-email transport

Status: agreed; ready for one manual Claude coder run
Tracking: this plan only; source queue task #6176 transfers here after this plan is committed

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

- [ ] Public outgoing and private-note operations are separate APIs with fixed
      visibility; existing private-note behavior remains green.
- [ ] Missing/invalid internal authentication fails closed before a claim or
      Chatwoot API call.
- [ ] The request requires a valid existing conversation ID, non-empty fixed
      content, stable idempotency key, and audit actor/source.
- [ ] The durable claim precedes the Chatwoot call. A duplicate accepted request
      does not call Chatwoot again and returns the recorded accepted result.
- [ ] A definitive pre-delivery/API rejection is distinguishable from an
      ambiguous timeout or connection loss after dispatch.
- [ ] Ambiguous state is durable and cannot be blindly retried into a duplicate.
- [ ] Tests inspect the exact Chatwoot public-outgoing payload and prove no
      customer send occurs during the suite.
- [ ] Relevant bridge tests and the broader bridge suite pass from the package
      root, or unrelated baseline failures are demonstrated precisely.
- [ ] Only task-owned code/tests/docs are committed locally; no push or deployment.

## Implementation steps (coder maintains within agreed scope)

- [ ] Verify worktree, branch, clean status, imports, and no active run marker.
- [ ] Inspect the existing `ChatwootClient`, `DedupStore`, bridge configuration,
      webhook authentication, and tests before selecting the smallest compatible
      endpoint and state model.
- [ ] Add failing contract/idempotency/error-shape tests before implementation.
- [ ] Implement the local transport and durable outcome behavior.
- [ ] Run focused tests, then `py -3.14 -m pytest tests -q` from `bridge`.
- [ ] Run `git diff --check`, inspect scope, update this checkpoint, and commit
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

- Active run: none
- Observed state: PM setup only; worktree is clean at the base revision.
- Changes/commits: plan only
- Checks and results: not run
- Remaining work: the authorized local transport slice
- Next step: Lana manually starts Claude with the prompt below
- Blocker or decision needed: none

## PM review

- Result: not reviewed
- Acceptance evidence: pending coder checkpoint
- QA/deployment: not authorized by this plan; the linked personal QA inbox is
  reserved for the later attended QA prompt
- Proposed scope changes: none

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

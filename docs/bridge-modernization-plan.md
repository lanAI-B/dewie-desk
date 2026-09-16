# Desk bridge modernization plan

Status: replacement implementation continues in source. No image has been
built, QA has not been changed, and production remains unchanged.

## Implementation progress

- DewieOps commit `611818c` adds the typed fail-closed decision policy,
  provider-neutral classifier call, independent runtime selection, and tests.
- dewie-desk commit `2d22eef` adds the safe shadow pipeline, verified-payload
  parser, webhook HMAC verification, durable message claims, private-note-only
  client, metrics, synthetic tests, and safe example configuration.
- Slice 1 is still partial: production's token/readiness checks and remaining
  operational evidence must be reconciled before QA.
- The Compose host port is configurable; Frankie's example uses 3001 because
  Command Center already owns 3000. A Chatwoot release tag is mandatory rather
  than silently falling back to `latest`.
- Slice 2's policy contract is implemented and integrated in source.
- Slice 3's OFFLINE half is done; its QA half has not started. A sanitized
  28-case corpus replays through the real transport gate, the real durable dedup
  store and the real DewieOps policy with no model call, and produces the
  aggregate the review report requires. Result and limits are in
  `docs/shadow-replay-offline.md`; the replay is `bridge/shadow.py` and runs in
  the suite as `bridge/tests/test_shadow.py`. Headline: the replacement
  authorizes 12 drafts where the bridge in production today would authorize 19,
  and authorizes none that it refuses. The corpus RECORDS each classification
  rather than computing it, so this is evidence about the policy and not about
  the classifier - that half still needs a running Chatwoot and a test mailbox.
- Slice 4 is implemented in source: the non-template drafter, read-only tool
  loop, attachment transcription, and utility extraction all consume the
  provider-neutral DewieOps runtime. Contract coverage passes for Anthropic and
  OpenAI, and the drafter has no provider-SDK or private DewieBrain import.
- Task #2708's local attachment seam is implemented in source: Chatwoot
  attachment references are normalized, bounded downloads feed local PDF text
  extraction or the canonical provider-neutral image transcription path, audio
  is labeled but not transcribed, and extracted text is card-redacted before it
  reaches `DraftRequest`. Live payload validation remains part of Slice 3.
- Task #2707's label-trigger seam is implemented in source: `message_created`
  only normalizes and durably records inbound messages; an explicit
  absent-to-present `dewie-draft` label change fetches the current conversation
  and requests one draft for its newest customer message. The successful path
  removes the command label, and durable action deduplication prevents webhook
  retries, sticky labels, or relabeling from redrafting the same inbound message.
  The command snapshots the newest already-recorded inbound ID before background
  work, so a later customer reply cannot ride an older label action.
  Live label-event and permission validation remains part of Slice 3.
- Task #2705's Sent-folder continuity seam is implemented in source: every
  inbound webhook now records the RFC822 threading headers it carries as a
  local thread map, and a read-only Sent-folder pass resolves each externally
  authored reply against that map and posts it as a private note, deduplicated
  on the sent Message-ID. It is off by default, it is an attended command
  rather than an HTTP route, and its live reliability is unproven - see
  "Rethreading reliability" below.
- Slice 5 has not started.

## Rethreading reliability (task #2705, acceptance evidence)

What the Sent-folder sync can and cannot promise, stated before anyone connects
a mailbox to it.

**Proven offline**, by `bridge/tests/test_sent_sync.py` and the state tests:

- An inbound `message_created` webhook records `msgid:` keys for its own
  Message-ID, its `In-Reply-To`, and every `References` entry, plus
  `thread:<subject>|<participant>` keys for the sender and each recipient.
- A sent reply resolves in that order: `In-Reply-To`, then `References`
  newest-first, then subject-and-recipient. The method used is counted
  separately, so a run that leaned on the guess is visible as a guess.
- One sent Message-ID produces at most one private note, across process
  restarts and across a re-parse of the same mail, because the claim is a row
  in SQLite rather than a set in memory.
- A reply that resolves to nothing is reported by subject and recipient and is
  never attached to a conversation by proximity.
- Only `post_private_note` is reachable from this path. The fake client in the
  tests raises on any other attribute, so a public message - which Chatwoot
  would actually *send* - fails the suite rather than the customer.

**Not proven, and it needs a live mailbox to settle:**

1. *Does Chatwoot populate `content_attributes.email.message_id`?* The whole
   map depends on it. Every fixture here is synthetic, written to Chatwoot's
   documented email shape. If the running version omits or renames that field,
   `msgid:` keys are never written and every reply falls back to the subject
   guess. **Check this first**: post one test mail, then read `thread_key` in
   `data/bridge-state.sqlite3` and confirm a `msgid:` row exists.
2. *Does Outlook set `In-Reply-To` on a reply?* Usually yes. A reply composed
   as a new message to the same person will not have it, and will land on the
   subject key or nowhere.
3. *Backfill has no map.* The map is built from inbound webhooks going forward,
   so Sent mail older than the bridge cannot resolve by Message-ID at all. The
   backfill #2705 wanted is limited to what the subject-and-recipient key can
   place, and that key is deliberately last.
4. *Subject keys are lossy on purpose.* Reply and forward prefixes and a
   leading `[tag]` are stripped so one thread has one key. Two genuinely
   different threads with the same trimmed subject and the same participant
   collapse into one key, and the newest conversation wins it.
5. *Sent mail is not filtered by mailbox.* Anything in the configured folder is
   a candidate, including mail a person sent about something else entirely.
   Those land in `unresolved` rather than in a conversation, but the count will
   not be zero and should not be read as a fault.

Until 1 and 2 are observed against a real inbox, treat the drafter as still
partially blind to what CS has already said. The gap is smaller and measured,
not closed.

## Outcome

Keep Chatwoot as the adopted inbox, assignment, and email-connector substrate,
while making Dewie's behavior selective, observable, and independent of any one
model provider. Zammad is not a candidate.

The human remains the sender. Dewie may create a private draft note, but neither
the bridge nor the drafter may send a customer-facing message.

## Current state

The intended architecture is split across three repositories:

- `dewie-desk` owns Chatwoot, webhook handling, payload normalization, and the
  Chatwoot API client.
- `DewieOps` owns deployable intelligence: classification policy, drafting,
  business tools, and model-provider runtimes.
- `DewieBrain` is Lana's private terminal-agent system and must not remain a
  production dependency.

The checked-in `dewie-desk` bridge is still the original thin pilot. Production
has a more complete bridge under `DewieBrain/desk_bridge`, including webhook
authentication, real-payload parsing, asynchronous work, deduplication, health
evidence, classification, and private-note posting. Its imports resolve to the
production DewieBrain checkout.

DewieOps already contains the canonical drafter and the provider-neutral model
runtime used by Discord. It does not contain the desk classifier. The current
drafter also still makes Anthropic-specific calls internally.

This means the first task is reconciliation, not a fresh rewrite.

## Target boundaries

```text
Chatwoot
   |
   | verified webhook
   v
dewie-desk
   payload parsing -> transport filtering -> idempotency -> private note
   |
   | provider-neutral DeskMessage / DraftOutcome
   v
DewieOps
   decision policy -> context/tools -> canonical drafter
   |
   | ModelRuntime
   v
Anthropic | OpenAI | future model provider
```

Rules for the boundary:

- `dewie-desk` knows Chatwoot shapes and APIs, but contains no business or model
  intelligence.
- DewieOps knows normalized desk messages, actors, intents, and draft policy,
  but contains no Chatwoot payload or API assumptions.
- Provider SDK objects stop inside `ModelRuntime` implementations.
- DewieBrain is absent from the deployed import graph.

## Slice 1: reconcile the proven bridge

Bring the production-proven plumbing into `dewie-desk` without changing its
behavior:

1. Port the parser, webhook verification, API result handling, background work,
   health reporting, and message-ID deduplication.
2. Port their tests using synthetic fixtures only. Do not commit captured
   production payloads, customer content, addresses, tokens, or machine paths.
3. Replace process-memory deduplication with a durable idempotency claim before
   production promotion. A restart must not make an old webhook draftable again.
4. Preserve the invariant that only private notes can be written.
5. Pin dependencies and the Chatwoot image; do not deploy `:latest`.

Acceptance gate:

- Real Chatwoot payload shapes parse through sanitized fixtures.
- Invalid or stale signatures are rejected before capture or processing.
- Outgoing messages, agent messages, private notes, activity events, malformed
  events, and webhook retries cannot reach classification.
- The bridge has no import path into DewieBrain.

## Slice 2: make draft eligibility explicit

Add a typed decision result before any expensive drafting work:

The decision pipeline is invoked only after a human adds the one-shot
`dewie-draft` command label. Message ingestion itself performs no model call.

```text
DraftDecision
  action: DRAFT | TRIAGE | SKIP
  actor: customer | corp_coordinator | reseller | vendor_partner | internal |
         system | unknown
  intent: place_order | modify_order | return_refund | order_status |
          product_question | access_support | discount_request |
          policy_question | notify | unknown
  actor_confidence
  intent_confidence
  reason_code
  classifier_provider
  classifier_model
```

Initial policy:

| Evidence | Decision | Drafter called? |
|---|---|---|
| Deterministic system sender | `SKIP` | No |
| Actor is `system` | `SKIP` | No |
| Intent is `notify` | `SKIP` | No |
| Classifier error or malformed result | `TRIAGE` | No |
| Either axis is `unknown` | `TRIAGE` | No |
| Either confidence is below the configured floor | `TRIAGE` | No |
| Internal explicit handoff with actionable intent | `DRAFT` | Yes |
| Customer/corporate/reseller/vendor with actionable intent | `DRAFT` | Yes |
| Everything else | `TRIAGE` | No |

`TRIAGE` means leave the conversation for a human and record a reason. It must
not quietly fall back to the broadest, most expensive drafting path.

Required reason codes include `transport_filtered`, `duplicate_message`,
`system_sender`, `notification_only`, `classifier_failed`, `unknown_actor`,
`unknown_intent`, `low_confidence`, `not_actionable`, `drafted`,
`draft_failed`, and `note_post_failed`.

Acceptance gate:

- Tests prove every `SKIP` and `TRIAGE` path makes zero drafter/model calls.
- Tests prove confidence values affect the decision.
- One inbound Chatwoot message ID can create at most one private draft note.
- Actor identity alone never invents intent.
- Classification failure fails closed to human review, not open to Claude.

## Slice 3: shadow validation

Run the new policy without changing the live desk:

1. Replay sanitized representative fixtures through both the current and new
   decision functions. **Done offline** - see `docs/shadow-replay-offline.md`.
   The current bridge's rule is transcribed rather than imported, because
   `dewie-desk` must keep no import path into DewieBrain (Slice 1 gate); the
   transcription names its source revision and must be re-checked against it.
2. Run the new bridge in an isolated QA/shadow configuration.
3. In shadow mode, record the proposed decision and reason only. Do not call the
   drafter and do not post notes.
4. Review aggregate counts and a small, deliberately selected sample of
   disagreements. Do not bulk-export customer mail.

The review report must show:

- screened, transport-filtered, duplicate, skipped, triaged, and draftable
  counts;
- counts by actor, intent, confidence band, and reason code;
- classifier calls, drafter calls avoided, and provider usage;
- false-skip and false-draft findings from the reviewed sample.

Acceptance gate:

- No system or notification-only sample is draftable. **Passing offline.**
- Unknown and low-confidence samples consistently reach human review.
  **Passing offline.**
- Legitimate customer requests remain draftable. **Passing offline.**
- Lana approves the policy based on the shadow evidence. **Not met** - the
  offline half cannot support this, because the corpus supplies the
  classifications rather than the classifier.

## Slice 4: provider-neutral desk models

Reuse DewieOps' existing `ModelRuntime` contract rather than create a desk-only
Anthropic/OpenAI abstraction.

1. Put classification behind an injected runtime with no tools, a small output
   budget, structured-result validation, and reasoning disabled.
2. Refactor the canonical drafter so its model/tool loop consumes
   `ModelRuntime`; provider SDK calls must not remain in drafter business logic.
3. Preserve the current Anthropic behavior with golden tests before adding an
   alternative provider.
4. Add OpenAI contract tests using the same normalized request and tool-result
   loop.
5. Record the provider, model, stop reason, and usage returned by the runtime.

Desk configuration must be independent of Discord configuration:

```text
DESK_CLASSIFIER_PROVIDER
DESK_CLASSIFIER_MODEL
DESK_DRAFTER_PROVIDER
DESK_DRAFTER_MODEL
DESK_UTILITY_PROVIDER
DESK_UTILITY_MODEL
DESK_CLASSIFIER_CONFIDENCE_FLOOR
```

Changing a desk experiment must not switch the Discord application's model.
Provider choice is environment configuration, while authorization and business
policy remain database/application concerns.

Acceptance gate:

- The same draft request can run through Anthropic or OpenAI without transport
  changes.
- Provider-specific objects do not escape the runtime implementation.
- Tool calls, continuation state, truncation, and usage retain their existing
  contract tests.
- The default provider reproduces the current accepted drafter golden suite.

## Slice 5: production promotion

Promotion is a separate attended operation requiring Lana's explicit approval:

1. Commit and review both repository changes with exact dependency revisions.
2. Build immutable-in-practice images/artifacts from committed source.
3. Deploy and verify QA, including webhook auth, idempotency, private-note shape,
   model-call counters, and the no-send invariant.
4. Inspect the office Compose/service ownership and prove the replacement will
   not create two live webhook consumers.
5. Stop and replace the DewieBrain-hosted bridge only during the approved
   cutover.
6. Verify one controlled inbound message end to end, then observe logs and
   counters for regressions.

Report each state separately:

```text
implemented -> tested -> image built -> QA deployed -> production unchanged
```

Production is not complete until the runtime imports DewieOps intelligence from
the reviewed revision and no longer imports deployable code from DewieBrain.

## Separate work

These issues should not be bundled into the bridge correction:

- The ACTEX inbox IMAP authentication failure.
- Chatwoot upgrades or replacement research.
- Customer-facing automatic sending.
- Agent assignment or workflow redesign inside Chatwoot.
- A generalized non-Chatwoot desk transport adapter. The normalized boundary
  above makes that possible later without requiring it now.

## Recommended implementation order

1. Reconcile the proven bridge and tests into `dewie-desk`.
2. Add the decision contract and fail-closed policy in DewieOps.
3. Run the shadow comparison and tune only from reviewed evidence.
4. Adapt the drafter to the existing provider runtime as a separate change.
5. Build and validate QA.
6. Ask for a distinct production cutover decision.

This order solves the uncontrolled drafting first without coupling it to the
larger provider migration.

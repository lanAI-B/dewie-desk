# Shadow replay — offline half of Slice 3

Generated 2026-09-16 from `bridge/fixtures/shadow_corpus.json` at corpus version
`desk-shadow-v1`. Regenerate with, from `bridge/`:

```
DEWIEOPS_PATH=<DewieOps checkout carrying dewie_brain.desk> python shadow.py
```

It exits non-zero if any acceptance gate fails, and `bridge/tests/test_shadow.py`
runs the same replay in the suite, so the gates below cannot be loosened quietly
by a later policy change.

Nothing here touched a model, a mailbox, the Chatwoot API, or the live desk.

## What this settles, and what it does not

**Settles.** Given a classification, the fail-closed policy decides the right
thing; screened-out transport never reaches the policy at all; a redelivered
webhook is screened exactly once; and the replacement authorizes strictly fewer
drafts than the bridge running in production today — 12 where production would
draft 19, with nothing drafted that production refuses.

**Does not settle — and this is the important half.** The corpus *records* the
classification rather than computing it. Every `actor × intent × confidence`
below is a label written into the fixture by hand, so this report is evidence
about the **policy**, not about the classifier's accuracy. A policy that is
perfectly fail-closed on correct labels is still wrong on wrong labels. Whether
the classifier produces these labels on real mail is the QA/shadow half of Slice
3 and needs a running Chatwoot and a test mailbox, both of which are attended
operations.

Two further limits worth stating before anyone reads a percentage off this:

1. **The corpus is representative, not sampled.** It was written to exercise
   every reason code and both sides of the confidence floor, so the ratios
   (12 draftable of 22 screened) describe the corpus, not the inbox. Do not
   read a drafting rate off it.
2. **In production the policy runs only behind the one-shot `dewie-draft`
   label.** The replay evaluates every screened message, which is the *upper
   bound* on what the policy would ever authorize, not what it would do on a
   normal day.

## The comparison that motivates the replacement

Seven of 22 screened messages are ones the production bridge would draft and
this policy will not:

| Case | New reason | Why production drafts it |
|---|---|---|
| `classifier-failed` | `classifier_failed` | `_classify` degrades to `('unknown','unknown','GENERAL')` and drafts anyway — the comment reads "a hiccup never blocks a draft". Fail-open. |
| `unknown-actor` | `unknown_actor` | The bridge only guards `actor == 'system'`; an unknown actor still drafts. |
| `unknown-intent` | `unknown_intent` | Same guard; unknown intent still drafts, on the GENERAL lane. |
| `low-confidence-both-axes` | `low_confidence` | The bridge reads no confidence at all. |
| `low-confidence-intent-only` | `low_confidence` | As above; one weak axis is invisible to it. |
| `notify-carrier-tracking-update` | `notification_only` | `intent == notify` is not a guard in the bridge, only `actor == system` is. A carrier FYI gets a customer-facing draft. |
| `internal-not-a-handoff` | `internal_not_handoff` | A teammate's own question drafts as though it were a customer's. |

The reverse list is empty: there is no message this policy drafts and the
production bridge refuses. That is the property to re-check on any future policy
change, and `test_the_new_policy_only_ever_drafts_less_than_production` asserts
it.

The baseline is a **transcription**, not an import — `dewie-desk` must have no
import path into DewieBrain (Slice 1 acceptance gate). It is transcribed in
`bridge/shadow.py::legacy_decision` from DewieBrain at `eb66a53`,
`desk_bridge/app.py::_classify` and `::_draft_and_post`. Re-check it against
those two functions before trusting a comparison taken from a later revision.

## Report

```
Desk shadow replay - offline half of Slice 3
corpus desk-shadow-v1 (28 cases), confidence floor 0.75
no model call, no mailbox, no Chatwoot API, no note posted

Transport and decision
    screened                           22
    transport-filtered                 5
    duplicate                          1
    draftable                          12
    triaged                            6
    skipped                            4

  By actor
    customer                           11
    not_classified                     3
    internal                           2
    vendor_partner                     2
    corp_coordinator                   1
    reseller                           1
    system                             1
    unknown                            1

  By intent
    modify_order                       3
    not_classified                     3
    order_status                       3
    place_order                        3
    product_question                   3
    return_refund                      2
    access_support                     1
    discount_request                   1
    notify                             1
    policy_question                    1
    unknown                            1

  By actor confidence band
    high                               9
    medium                             8
    none                               3
    low                                2

  By intent confidence band
    medium                             12
    high                               4
    low                                3
    none                               3

  By reason code
    drafted                            12
    system_sender                      3
    low_confidence                     2
    not_incoming                       2
    classifier_failed                  1
    duplicate_message                  1
    empty_content                      1
    internal_not_handoff               1
    missing_sender_email               1
    notification_only                  1
    private_note                       1
    unknown_actor                      1
    unknown_intent                     1

  By drafter category
    GENERAL                            5
    ORDER                              4
    RETURN                             2
    PG                                 1

  Model usage
    classifier calls                   20
    classifier calls avoided           8
    drafter calls made                 0
    drafter calls policy would allow   12
    drafter calls refused by policy    10
    never offered by transport         6
    providers                          {'offline-fixture': 20}

  Against the bridge running in production today
    agree on drafting                  15
    it would draft, this does not      7
      - internal-not-a-handoff (internal_not_handoff)
      - notify-carrier-tracking-update (notification_only)
      - classifier-failed (classifier_failed)
      - unknown-actor (unknown_actor)
      - unknown-intent (unknown_intent)
      - low-confidence-both-axes (low_confidence)
      - low-confidence-intent-only (low_confidence)
    this drafts, it would not          0

  Findings
    false_draft                        0
    false_skip                         0
    triaged_but_expected_draftable     0
    other_disagreements                0
    corpus_defects                     0

  Acceptance gates
    PASS  no_system_or_notification_sample_is_draftable
    PASS  unknown_and_low_confidence_reach_human_review
    PASS  legitimate_customer_requests_remain_draftable
    PASS  no_corpus_defects
```

`providers: offline-fixture` is deliberate. The replay records a provider name
that cannot be mistaken for `anthropic` or `openai`, so this report can never be
quoted as evidence about a real provider's behaviour.

## Reading the two "avoided" lines

They are kept apart because they are different savings and adding them would
flatter the policy: **10** drafter calls were refused by the policy on messages
it actually looked at, and **6** more were never offered to it because transport
screened them (5 filtered, 1 redelivered webhook). Of the 8 classifier calls
avoided, 6 are those transport cases and 2 are system senders the policy rejects
on the address alone, before spending anything.

## The corpus

28 wholly synthetic cases in `bridge/fixtures/shadow_corpus.json`. No captured
production payload, customer name, real address, order number or token. Every
address is on `example.com` / `example.net` / a `.example` TLD, none of which
resolve, so a mistake in the corpus cannot mail anyone;
`test_corpus_carries_no_reachable_address` enforces that.

Each case carries `expected` — the outcome a human says is correct — written
alongside the fixture so a disagreement with the policy surfaces as a finding
instead of being assumed away. All 28 currently agree.

A case that omits `classification` is asserting the classifier is **never
reached** for it. If the policy ever does reach one, the replay reports a corpus
defect and fails its gate rather than inventing a label — the defect cannot be
signalled by raising, because `main._decision` catches every classifier
exception and would render it indistinguishable from a real classifier failure.

## What is still owed on Slice 3

- Bring Chatwoot up in an isolated QA configuration and run the bridge against
  it in shadow mode (`BRIDGE_SHADOW_MODE=true`, `BRIDGE_DRY_RUN=true`).
- Confirm the classifier reproduces these labels on real mail, and review a
  small deliberately selected sample of disagreements. Do not bulk-export
  customer mail.
- Settle the two rethreading unknowns in `bridge-modernization-plan.md` under
  "Rethreading reliability", which need the same live inbox.
- Lana approves the policy on that evidence. Only then does Slice 5 start.

All four are attended: they need a deploy, credentials, and live mail.

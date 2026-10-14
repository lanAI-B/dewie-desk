# Spec: close the human-written promos the spam screen misses

Wren, 2026-10-14, on Lana's ask ("classifier to auto close spam and promos as it goes").
For a feature coder. Two repos: DewieOps (the classifier) and dewie-desk (the bridge).
Branch from each repo's default branch; never merge (Wren reviews and merges).

## What already exists (don't rebuild it)

Spam auto-resolve has been live since queue #6332 (`4ecc9fc`). In `bridge/spam.py`:
`spam_rules.csv` (45 rules) runs first, then the DewieOps desk classifier
(`DewieOps/dewie_brain/desk/classifier.py`). A spam verdict labels the conversation `spam` and resolves it.
Production `.env`: `SPAM_AUTORESOLVE_ENABLED=true`, `SPAM_AUTORESOLVE_DRY_RUN=false`,
`SPAM_CLASSIFIER_ENABLED=true`, min confidence 0.9. The audit log is `data/spam-autoresolve.jsonl`
(since 09-24: 138 resolved by the classifier, 23 by rules, 15 vetoed).

## The gap

`evaluate_classification` only counts **`actor=system` + `intent=notify`** as noise. Human-written
mail falls through: promos, newsletters, sales pitches, cold outreach, phishing. The classifier reads
those as `vendor_partner` / `unknown` + `notify` / `unknown`, so they come back `not_spam`. Wren's rounds
labelled six of them by hand on 10-14 alone (#1526 #1530 #1531 #1532 #1538 #1550: a webinar, a SaaS pitch, a
Japanese seminar spam, a fake SSA statement, a French newsletter, a compliance newsletter). None
of them shows in the audit log, because `not_spam` isn't audited, so the misses are invisible.

Second problem: two labels for one thing. The bridge writes `spam` (159 conversations), while
`config/chatwoot_routing.csv` and the support rounds write `spampromo` (187). `spampromo` is the one
in DewieBrain `config/support_round_labels.csv`.

## The change

1. **New intent `marketing` in the desk classifier** (DewieOps `desk/decision.py` `Intent` and the
   `_PROMPT` in `desk/classifier.py`): "unsolicited promotion, newsletter, sales pitch, event or
   webinar invite, cold outreach, phishing or scam; nobody is asking us about their own purchase."
   In `decide()` it maps to `SKIP` with reason `marketing`, like `NOTIFY`. Keep it out of
   `ACTIONABLE_INTENTS`. Check every place that switches on `Intent` (`decision.py`,
   `dewie-desk/bridge/shadow.py:310-315`) so `marketing` is never treated as unknown, which would mean triage.
2. **The spam stage counts it** (`bridge/spam.py` `evaluate_classification`): noise is
   `system/notify` (as now) **or** `intent=marketing` with any actor except `customer`,
   `corp_coordinator` and `internal`. Same 0.9 floor (min of both confidences), same subject veto
   (`_CUSTOMER_SUBJECT`).
3. **Two more vetoes, for the marketing path only:**
   - the conversation already holds an outgoing message from us (a reply in a thread we're in);
   - the sender has an order in either store. Use the app's `get_orders_by_email` the cheapest way the
     bridge can reach it. If it can't be reached, treat that as a veto: fail closed, never resolve.
4. **Shadow first.** The new path gets its own switch, `SPAM_MARKETING_DRY_RUN`, default **true**.
   While it's on, a marketing verdict writes `would_resolve` to the audit log and changes nothing in
   Chatwoot. The existing system/notify path is unchanged.
5. **Audit the misses.** Log classifier `not_spam` verdicts too, as `outcome: "passed"` with actor, intent
   and score. That makes the miss rate measurable, and the shadow week comparable with what the
   rounds label.
6. **One label.** Make the bridge's label a setting, `SPAM_LABEL` (default `spam`, today's behaviour).
   Lana picks the final name. Wren recommends `spampromo`, which keeps the round and routing label. Don't
   relabel existing conversations; that's a separate bulk job under `/chatwoot`'s preconditions.
7. **Rule suggestions, not rules.** `spam_dryrun.py` gets a `--suggest-rules` flag. It lists senders and domains
   resolved 3 or more times in the window that have no rule yet, written as ready-to-paste
   `spam_rules.csv` rows. It prints them; it never edits the CSV.

## Tests (both repos, nothing live)

- The classifier JSON parsing accepts `marketing`; `decide()` returns SKIP/`marketing`.
- `evaluate_classification`: marketing at 0.95 from `unknown` → spam; from `customer` → not spam;
  at 0.85 → not spam; a subject with "order #1234567" → veto.
- The two new vetoes, with the order lookup stubbed, and with a lookup error → veto.
- `SPAM_MARKETING_DRY_RUN=true` → `would_resolve` audited, no label or resolve call.
- `not_spam` writes `passed` with actor, intent and score.
- Fixtures: the six conversations above, rebuilt **synthetic** (same shape, invented senders). No real
  mail or customer rows in the repo.

## Out of scope

The rules CSV contents, relabelling history, Chatwoot automation rules, email sync and SMTP.

## Rollout (Wren and Lana, not the coder)

Wren reviews and merges both branches, and queues the deploy for the end-of-day window: the bridge
restart, plus a DewieOps rebuild if the classifier ships in the image. A week in shadow follows. Each
round compares the `would_resolve` lines with what it labelled by hand. Lana flips
`SPAM_MARKETING_DRY_RUN=false` once false positives are near zero.

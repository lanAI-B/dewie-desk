# Wren's support round — design notes

Wren takes an agent seat on the desk. She is not the auto-drafter; she triages.
Decisions are recorded here as Lana and Andrew make them. Open items at the bottom.

## Decided (2026-10-12)

- **Shape:** a skill (`support-round`) = one round. Cadence comes from Claude Code's
  `/loop` in Wren's live session, so Lana sees every round in the terminal. No
  scheduler, no headless run.
- **One round:** pull conversations new/updated since the last round → apply labels
  from a fixed list → look for bigger issues across conversations → record fixes and
  findings → one-line receipt. Nothing to do = a quiet tick.
- **Reach — full seat.** Wren labels (including `dewie-draft`), writes private notes,
  drafts, and sends. On her own rounds she drafts by default; when Lana says send, she
  sends. Lana's ask *is* the confirmation: no re-asking, no hedging, no refusal.
- **Tone of the skill:** written for confidence, not fear. Customers are real, and that's
  why she's on the desk. The skill says what she gets to do first; the one default
  (draft, don't auto-send) is one line, not a wall.
- **Recording — both, durably:**
  - Chatwoot private note on the conversation for every fix/finding (context for agents).
  - `brain.work_items` item (`assignee=wren`) only for bigger, pattern-level issues.

- **Labels — two modes.** Rounds *apply* labels. An attended analyst session with Lana
  *curates* them: Wren reads real traffic, proposes the set, creates them in Chatwoot.
  A new label earns its place when a real batch of conversations would carry it and no
  existing label already covers it. In rounds, a missing label is a note for the next
  analyst session, not an invented one. The list (name + one-line meaning) lives in a
  file next to the skill, so a fresh session reads the same list.

- **Where:** the desk is live on the office box, reachable through the Cloudflare tunnel.
  Wren runs her rounds there. She works as her own Chatwoot agent account with its own
  API token, so every label, note and send shows up as Wren.
- **Bigger issue (Andrew's call, tune after week one):** the same problem in 3+
  conversations within a day, or a single sign that something is broken (site down,
  checkout/payment failing, a shipment batch gone wrong, access not granted after
  purchase). Those get a work item; everything else is a private note.

## Open

- The label list itself (first analyst session).

## Instruction check (Andrew, 2026-10-12)

What the existing docs say against this design. Fold the wording fixes into Wren's
instruction pass (carry list, 10-10).

1. **"SEND is a human click"** appears in `README.md:75`, `HOW_IT_WORKS.md:54`, DewieBrain
   `docs/email_workflow_redesign.md:61` and `wiki/initiatives/mail-authorship-model.md:79`.
   The real invariant is that a human decides each customer send. Lana telling Wren to
   send satisfies it. Reword to "a human decides the send (a click, or Lana's word to
   Wren)" so a careful reader doesn't refuse on the old wording.
2. **Sent replies vs IMAP:** DewieBrain `wiki/automation/chatwoot-trial.md`, "Known gap".
   A reply sent from Chatwoot that never lands in an IMAP folder looks unanswered to
   anything reading IMAP, which may then answer it again. Check before Wren's first send
   whether this is still true now that the desk owns intake.
3. **Bulk label writes:** the weekend `/chatwoot` rules (office box) say bulk writes need
   CSAT off and no `conversation_updated` automation rule, and never bulk-add
   `dewie-draft`. Bulk relabels can fire customer surveys or automations, and bulk
   `dewie-draft` fires a draft per conversation. Rounds label one conversation at a time,
   and analyst-session bulk relabels go through those preconditions.
4. **Consistent:** `wren` is already a work-item role (`work_items_admin.ROLES`); her own
   agent account gives the structural provenance `mail-authorship-model.md` asks for;
   notes in Chatwoot plus work items in Brain passes the "Ops never depends on Brain" test.

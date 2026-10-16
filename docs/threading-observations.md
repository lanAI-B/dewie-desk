# Threading: why replies open new conversations

Status: **observing** (Lana and Wren, from 2026-10-16). No code change yet. We gather a few
more days of data while Lana replies from the desk only and the rest of the team still
replies from Outlook. Then we decide on a fix.

Re-run the numbers (read-only):

```
docker exec -i dewie-desk-postgres-1 psql -U postgres -d chatwoot -v days=14 < scripts/threading_check.sql
```

Add a row to the [log](#observation-log) each time.

## The symptom

Lana, 10-16: threads don't merge automatically. A customer's reply often shows up as a new
open item instead of landing in the conversation it answers. Expected out of the box from
Chatwoot.

## How Chatwoot threads email (v4.16.1, native IMAP inboxes)

Inboxes 1 (ABS Service) and 3 (ACTEX Inbox) are native `Channel::Email` inboxes fetching
over IMAP from Rackspace (`secure.emailsrvr.com`). Threading is therefore Chatwoot's own
logic, not the bridge's:

- An incoming email joins an existing conversation **only if its `In-Reply-To` /
  `References` matches the Message-ID (`messages.source_id`) of a message Chatwoot already
  holds.**
- No match means a new conversation. Chatwoot never merges by contact or by subject.
- Inbox settings: `lock_to_single_conversation = false` (one conversation per thread, the
  normal email setting) and `allow_messages_after_resolved = true`. Nothing in the config
  blocks threading or reopening.

## What the data showed (first run, 10-16, last 14 days)

| | count |
|---|---|
| New conversations | 430 |
| Fresh emails (no `In-Reply-To`) | 342 |
| **Replies whose parent Chatwoot doesn't have** | **83** |
| Replies whose parent it has, but still a new conversation | 5 |
| `Re:`/`Fwd:` subject from a contact who already has an older conversation | 48 |
| Outgoing messages carrying a Message-ID Chatwoot can thread on | 37 of 331 |

The 83 unthreaded replies, by the host in their `In-Reply-To`:

- `apps.rackspace.com` 32 + `email.rackspace.com` 9 = **41**: replies to mail we sent from
  Rackspace webmail.
- `dw*mdwk*` / `wn1xsdwk*` hosts, ~20: workstation hostnames. *Suspicion:* Outlook desktop
  on team PCs. Not yet matched to a person.
- `asana.com` 4: Asana notification threads. Harmless.
- `mail.gmail.com` 3, a few `*.outlook.com`: customers replying to their own earlier
  message or to a forward. Not ours to fix.

## Suspicions

1. **Main cause: we reply outside Chatwoot.** When the team answers from Outlook or
   webmail, Chatwoot never sees that email's Message-ID. The customer's reply points at it,
   Chatwoot finds no match, and a new conversation opens. That accounts for ~60 of the 83.
2. **Sent-sync copies the text but not the ID.** `bridge/sent_sync.py` already reads each
   sent email's Message-ID (line ~237) and uses it to find the conversation the copy
   belongs to. The copy it posts into Chatwoot has no matchable `source_id`, so Chatwoot
   still can't thread the customer's *next* reply. 294 of 331 outgoing messages have no ID.
3. **Replies sent from Chatwoot thread fine.** The 37 outgoing messages that carry an ID
   are Chatwoot-sent replies. If (1) is right, the count of unthreaded replies should fall
   as more of the team replies from the desk. That's the thing to watch.
4. **The 5 near-misses are probably not a config problem.** 1236/1235, 1298/1299 and
   1564/1563 have adjacent ids, so two emails were probably fetched in the same pass (a
   race). 1624 answers an autoreply (1594). 1627 is a reply to a PureChat chat-box email
   (1588) that was still open at the time: unexplained, maybe because the PureChat sender
   isn't the customer. Watch whether this group grows.
5. **Reopening after resolve: unverified.** The settings allow it and Chatwoot is expected
   to reopen a resolved conversation when a threaded reply arrives. No clean example yet
   either way.

## How to verify

- **Reopen test** (one minute): resolve a test conversation on the desk, reply to it from a
  Gmail account, and see whether it reopens or arrives as a new item.
- **Confirm suspicion 1 on one case:** take an unthreaded reply's `In-Reply-To` from the
  `rackspace` group and find that Message-ID in the Sent folder that sent-sync reads.
- **Name the workstation hosts:** match a `dw*mdwk*` Message-ID to a sender in the Sent
  folder. That tells us whose Outlook it is.
- **Trend:** re-run the script daily. Falling `reply_parent_unknown` while the team moves to
  the desk supports suspicion 1. If it stays flat, look again.

## Fixes (not decided)

1. **Reply from the desk, not Outlook** (habit, no code). Lana does this already (10-16); the
   rest of the team doesn't yet. Fixes the cause for everyone who adopts it.
2. **The bridge threads what Chatwoot can't** (dewie-desk feature, for a coder). On a new
   conversation whose `In-Reply-To`/`References` matches a sent email that sent-sync has
   already filed, move the message into that conversation, or close the duplicate with a
   private note linking it. Covers anyone still on Outlook, plus history. Open question for
   the coder: Chatwoot's API has no conversation merge, so "move" means re-posting the
   message into the parent and resolving the stray one. Check what that does to
   attachments and to the contact's view.
3. **Give sent-sync's copies the real Message-ID** as `source_id`, so Chatwoot threads the
   next reply natively. Cleaner than 2 if the API allows setting `source_id` on a created
   message: needs checking against v4.16.1. Doesn't help mail sent before the change.
4. **Not fixable, and normal:** a customer who writes a brand-new email (not a reply) opens a
   new conversation. Contact history in the sidebar is the answer there.

Leaning (Wren): 1 as the habit, plus 2 or 3 for whoever is still on Outlook. Decide after a
few days of the log.

## Observation log

One row per run of `scripts/threading_check.sql` (`days=14` unless noted). Add context: who
replied from where that day.

| date | new | fresh | unthreaded (parent unknown) | parent known, new conv | outgoing with ID | notes |
|---|---|---|---|---|---|---|
| 10-16 | 430 | 342 | 83 (rackspace 41, workstations ~20) | 5 | 37 / 331 | baseline. Lana on the desk only; team still on Outlook |
| 10-16 eve | 423 | 337 | 81 | 5 | 41 / 345 | first day with Lana desk-only; window slid (10-02 dropped off). Script fix: section 3 times were shown 8h off (timestamps are UTC without zone); now NY local |

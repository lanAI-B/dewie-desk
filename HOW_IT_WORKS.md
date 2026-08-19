# How dewie-desk works — the mental model + a debug playbook

Written to *teach*, not just document. If you understand this page, you can operate and
debug the desk without me.

## The one idea: three layers, and you only own the middle-small one

```
   ┌─────────────────────────────────────────────────────────────────┐
   │  CHATWOOT  (rented — you don't maintain this)                      │
   │  email in/out, tickets, agent UI, auth, web chat widget           │
   └───────────────┬───────────────────────────────────┬──────────────┘
                   │ webhook: "new message"             │ API: "post this note"
                   ▼                                    ▲
   ┌─────────────────────────────────────────────────────────────────┐
   │  BRIDGE  (yours — but tiny: ~120 lines, dewie-desk/bridge/)        │
   │  hears the event → asks the brain → posts the answer as a NOTE    │
   │  contains NO intelligence of its own                               │
   └───────────────┬───────────────────────────────────────────────────┘
                   │ import  draft_reply(DraftRequest) -> DraftResult
                   ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  BRAIN  (yours — installed from DewieOps/dewie_brain/drafter/)   │
   │  KB search, order lookups (read-only), Opus draft, tone, SOPs     │
   └─────────────────────────────────────────────────────────────────┘
```

**Why split this way?** So each layer is replaceable without touching the others.
Chatwoot can be swapped for another desk by rewriting only the bridge (the "adapter").
The brain never imports Chatwoot; the bridge never drafts. That's the whole design —
"own the seams, rent the plumbing." The bridge *references* the brain (imports it) and
*references* Chatwoot (pinned Docker image); it copies neither.

## How a customer message becomes a draft

1. Customer emails the mailbox. **Chatwoot polls it over IMAP (~every 15 min)** and turns
   the email into a **conversation** (ticket).
2. That creation fires a **`message_created` webhook** → `POST` to the bridge's `/webhook`.
3. The bridge builds a `DraftRequest` (sender, subject, body) and calls
   `dewie_brain.drafter.draft_reply(...)` — the *same* function the old desk and the
   4x-daily runner use. One brain, three surfaces.
4. The brain does KB search, read-only order/customer lookups, and an Opus draft pass,
   then returns the reply text + metadata.
5. The bridge posts that as a **private note** on the ticket (or, with `BRIDGE_DRY_RUN=true`,
   just logs it). **Sending the reply is always a human click in Chatwoot.**

## Operating it

```powershell
# start the bridge from its own environment; requirements install DewieOps
& ".\.venv\Scripts\python.exe" -m uvicorn main:app `
    --app-dir "C:\Users\lana\Documents\dewie-desk\bridge" --host 0.0.0.0 --port 8624

# is it alive?
curl http://localhost:8624/health      # -> {"ok":true,"dry_run":true,...}
```

- **`BRIDGE_DRY_RUN`** in `.env`: `true` = draft to logs only (safe). `false` = post the
  private note. It never *sends* to the customer regardless.
- **Chatwoot** is `docker compose up/down` in `dewie-desk/`. UI at http://localhost:3000.
- Ports: Chatwoot 3000, bridge 8624, (the live alpha desk is 8623 — untouched).

## Debug playbook — the METHOD, not just fixes

The golden rule from the day we built this: **ask the machine, don't guess.** An LLM will
give you a confident theory; a test gives you the truth. Every hard bug this day fell to
one of these:

1. **Test from *inside* the container**, because "works on my machine" lies about what
   Chatwoot can reach:
   ```powershell
   # DNS + TCP + TLS + SMTP-auth, straight from the Chatwoot container:
   $code = 'require "socket"; Socket.tcp("secure.emailsrvr.com",993,connect_timeout:8){puts "ok"}'
   $code | docker compose exec -T rails ruby
   ```
   This is how we proved the network was fine and the problem was elsewhere.

2. **Inspect stored values as BYTES** to catch what your eyes can't see. Our whole
   afternoon detour was a single leading space in the hostname (`" secure.emailsrvr.com"`)
   that Chatwoot saved silently:
   ```ruby
   ch = Channel::Email.find(2); puts ch.imap_address.bytes.inspect   # [32, 115, ...] <- 32 = space!
   ```

3. **Read the real logs**, they name the real error (which the UI toast usually mislabels):
   ```powershell
   docker compose logs --tail=40 rails
   docker compose logs --tail=40 sidekiq     # IMAP fetch + webhook delivery run here
   ```
   The UI said "could not connect to SMTP"; the log said "getaddrinfo: Name does not
   resolve." The log was right.

4. **Force an IMAP fetch** instead of waiting 15 min:
   ```ruby
   Inboxes::FetchImapEmailsJob.perform_now(Channel::Email.find(2))
   ```

5. **TLS "certificate verify failed (unable to get local issuer certificate)"** = the
   container lacks the mail server's CA. For a local pilot, set **Open SSL Verify Mode =
   none** on the channel. (Production: bake the CA into the image instead.)

## What's proven vs. not (honest status, 2026-07-23)

- ✅ Inbound mail → ticket (IMAP), on a Rackspace test mailbox.
- ✅ Outbound reply from the Chatwoot UI (SMTP).
- ✅ Bridge reachable from the container; a message produces a correct, on-tone Dewie
   draft in dry-run (KB + read-only order lookup + Opus), posting nothing.
- ⚠️ **Not yet verified:** the *real* Chatwoot webhook payload shape. The dry-run test used
   a synthetic payload matching the bridge's current field-reading. Real Chatwoot payloads
   nest `message_type`/`sender`/`content` differently — the bridge's `/webhook` parser
   will likely need adjusting against a captured live payload. **This is the first thing to
   check when you register the real webhook.**
- 🔮 Not started: posting real private notes (`DRY_RUN=false`), the web-chat Agent Bot
   (live replies), and cutover.

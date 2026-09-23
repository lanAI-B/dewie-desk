# dewie-desk

> Modernization is in progress on `feature/desk-bridge-modernization`. The new
> bridge defaults to shadow mode and dry-run, depends on the sibling DewieOps
> checkout, and is not connected to QA or production. See
> `docs/bridge-modernization-plan.md` for the staged cutover contract.

Chatwoot-based support desk for ABS / Actex, with Dewie as the intelligence layer.

**Design doctrine — adopt, don't own the plumbing.** The ticket pipeline, auth, agent UI,
and email transport are Chatwoot's job; this repo owns only a thin layer on top:

- `docker-compose.yml` — **adopts** the upstream `chatwoot/chatwoot` image (pinned via
  `CHATWOOT_TAG`). We do **not** fork Chatwoot's source. Based on Chatwoot's official
  `docker-compose.production.yaml`.
- `bridge/` — `dewie-desk-bridge`: a thin webhook worker. Hears Chatwoot events,
  runs classify → enrich → draft, posts the draft as a **private note**, applies
  labels. It contains **no drafting logic of its own** — it *imports* the canonical
  drafter from the installed `DewieOps` package (`dewie_brain.drafter.draft_reply`). Copy-paste
  is banned; that's how tone drifted last time.
- `.env` — secrets + config (gitignored; copy from `.env.example`).

## The two things this repo does NOT contain (on purpose)

1. A fork of Chatwoot. It's a pinned image.
2. A copy of the drafter/classifier. The bridge references `dewie_brain` as a package.

Both are the same principle: **reference, don't copy** — recreating either would put us
back to owning the plumbing we chose to shed.

## Local pilot — order of operations

Goal: prove the mail loop first — **inbound → ticket → reply back out** — on a
**test mailbox, no live mail, no auto-send**. Pilot against Rackspace IMAP/SMTP (what
we run today); M365 OAuth is a later inbox swap. Do not stop the team's forwarding
until the board stays sane on its own.

1. **Bring Chatwoot up** (needs Docker Desktop):
   ```
   .\scripts\initialize-local-env.ps1  # generates ignored local app/DB secrets
   docker compose run --rm base-prepare   # one-shot: create + migrate + seed DB
   docker compose up -d
   ```
   `base-prepare` is in the `setup` profile, so ordinary `up` will not rerun it.
   Chatwoot uses `CHATWOOT_HOST_PORT` (3001 in the Frankie example) — create the
   super admin account at that local URL.

2. **Connect the mailbox — in the Chatwoot UI, not here.** Chatwoot configures email
   channels in-app (Inbox → Add Inbox → Email).
   - **Now (Rackspace):** we're still on Rackspace, so pilot with generic **IMAP/SMTP**
     and plain auth (`secure.emailsrvr.com`, IMAP 993 / SMTP 465) — the same mailbox
     the current desk already reads. No Azure, no OAuth. Use a **test mailbox**, not a
     live customer inbox.
   - **Later (M365):** when the Rackspace→M365 migration completes, add a new inbox via
     Chatwoot's native **Microsoft OAuth** channel (Azure app with `Mail.ReadWrite`,
     `Mail.Send`, `offline_access`) and retire the Rackspace one. Provider swap is UI
     config only — the bridge and drafter never change.

3. **Observe.** Send a test email to the connected mailbox; confirm it lands as a ticket.
   Reply from the Chatwoot UI; confirm it reaches the sender. **This is the go/no-go.**

4. **Wire the bridge** (Phase 2 — after the mail loop is proven):
   ```
   cd bridge
   pip install -r requirements.txt
   uvicorn main:app --port 8624
   ```
   In Chatwoot: Settings → Integrations → Webhooks → add
   `http://host.docker.internal:8624/webhook` for `message_created` and
   `conversation_updated`. Create an access token (Profile → Access Token) and put it
   in `.env` as `CHATWOOT_API_TOKEN`.
   Incoming messages are recorded but do not classify or draft automatically. Add
   the `dewie-draft` label to request one draft for the newest customer message.
   After a private draft note is posted, the bridge removes the label; a later
   customer reply requires a fresh label action. SEND stays a human click.

## Spam auto-resolve

Inbound email that `bridge/spam_rules.csv` (or, optionally, the DewieOps
classifier) calls spam/noise gets the `spam` label and is resolved — no draft,
nothing deleted, no IMAP folder touched. Off by default
(`SPAM_AUTORESOLVE_ENABLED`), dry-run first (`SPAM_AUTORESOLVE_DRY_RUN`); see
`.env.example`. Every verdict is appended to `data/spam-autoresolve.jsonl`
(conversation, sender, subject, rule or classifier score, outcome); a false
positive is also findable in Chatwoot by filtering on the `spam` label, and a
new message from the sender reopens the conversation. Add rules by editing the
CSV. Before enabling, run the read-only report with a user (not bot) token:

```
cd bridge
python spam_dryrun.py --days 2 --out spam-dryrun.md   # add --classifier to include stage 2
```

The bot-token-safe endpoints used are `conversations/{id}/labels` (index,
create) and `conversations/{id}/toggle_status`. Create the `spam` label under
Settings → Labels once so it shows with a colour; Chatwoot tags the
conversation either way.

## Who unblocks what

| Step | Owner |
|------|-------|
| Repo scaffold, bridge code | Dewie (this repo) |
| Docker running on the box | Lana (deployment side) |
| Rackspace test-mailbox IMAP/SMTP creds | Lana (already in hand — desk uses them) |
| Azure app registration / M365 creds | Lana — **future**, only when M365 migration lands |
| Watching the shadow-run board | Both |

## Status

Phase 0 (extract canonical drafter + golden suite) — **done** in `DewieOps`
(`dewie_brain/drafter/`). This repo is Phase 1: prove the mail loop on local against
Rackspace IMAP/SMTP. M365 OAuth is a later inbox swap, gated on the migration.

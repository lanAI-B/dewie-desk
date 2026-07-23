# dewie-desk

Chatwoot-based support desk for ABS / Actex, with Dewie as the intelligence layer.

**Design doctrine — adopt, don't own the plumbing.** See
`DewieBrain/docs/email_workflow_redesign.md`. The ticket pipeline, auth, agent UI,
and email transport are Chatwoot's job; this repo owns only a thin layer on top:

- `docker-compose.yml` — **adopts** the upstream `chatwoot/chatwoot` image (pinned via
  `CHATWOOT_TAG`). We do **not** fork Chatwoot's source. Based on Chatwoot's official
  `docker-compose.production.yaml`.
- `bridge/` — `dewie-desk-bridge`: a thin webhook worker. Hears Chatwoot events,
  runs classify → enrich → draft, posts the draft as a **private note**, applies
  labels. It contains **no drafting logic of its own** — it *imports* the canonical
  drafter from the `DewieBrain` repo (`dewie_brain.drafter.draft_reply`). Copy-paste
  is banned; that's how tone drifted last time.
- `.env` — secrets + config (gitignored; copy from `.env.example`).

## The two things this repo does NOT contain (on purpose)

1. A fork of Chatwoot. It's a pinned image.
2. A copy of the drafter/classifier. The bridge references `dewie_brain` as a package.

Both are the same principle: **reference, don't copy** — recreating either would put us
back to owning the plumbing we chose to shed.

## Local pilot — order of operations

Goal: prove the highest-risk integration first — **M365 inbound → ticket → reply back
out** — on a **test mailbox, no live mail, no auto-send**. Do not stop the team's
forwarding until the board stays sane on its own.

1. **Bring Chatwoot up** (needs Docker Desktop):
   ```
   cp .env.example .env      # then fill SECRET_KEY_BASE, passwords, CHATWOOT_TAG
   docker compose run --rm base-prepare   # one-shot: create + migrate + seed DB
   docker compose up -d
   ```
   Chatwoot is at http://localhost:3000 — create the super admin account.

2. **Connect the mailbox — in the Chatwoot UI, not here.** Chatwoot configures email
   channels in-app (Inbox → Add Inbox → Email). Modern Chatwoot has a native
   **Microsoft** channel that connects via **OAuth** — this sidesteps the xoauth2-in-IMAP
   quirk. Needs an Azure app registration:
   - API permissions: `Mail.ReadWrite`, `Mail.Send`, `offline_access`
   - Redirect URI pointing at this Chatwoot instance
   - Fallback: generic IMAP inbound + SMTP outbound (the xoauth2 path we were wary of).

3. **Observe.** Send a test email to the connected mailbox; confirm it lands as a ticket.
   Reply from the Chatwoot UI; confirm it reaches the sender. **This is the go/no-go.**

4. **Wire the bridge** (Phase 2 — after the mail loop is proven):
   ```
   cd bridge
   pip install -r requirements.txt
   $env:DEWIE_BRAIN_PATH = "C:\Users\lana\Documents\DewieBrain"   # reference, not copy
   uvicorn main:app --port 8624
   ```
   In Chatwoot: Settings → Integrations → Webhooks → add
   `http://host.docker.internal:8624/webhook` for `message_created`,
   `conversation_created`. Create an access token (Profile → Access Token) and put it
   in `.env` as `CHATWOOT_API_TOKEN`.
   The bridge posts drafts as **private notes only** — SEND stays a human click.

## Who unblocks what

| Step | Owner |
|------|-------|
| Repo scaffold, bridge code | Dewie (this repo) |
| Docker running on the box | Lana (deployment side) |
| Azure app registration / M365 creds | Lana (Azure gate) |
| Watching the shadow-run board | Both |

## Status

Phase 0 (extract canonical drafter + golden suite) — **done** in `DewieBrain`
(`dewie_brain/drafter/`). This repo is Phase 1: prove the M365 loop on local.

# M365 mailbox access: what we know (AL inbox, and the Rackspace → M365 migration)

Written 2026-11-18 (Wren, with Lana) from a live session on ACTEX Box. Everything marked
**verified** was run that day; the rest is labelled as plan or open question. The first M365
mailbox on the desk is AL (`support@actuariallearning.com`, Chatwoot inbox 6). ABS and ACTEX
(`service@actuarialbookstore.com`, `support@actexlearning.com`) are still on Rackspace and will
follow, so this is the playbook for them too.

## 1. How the AL inbox is connected

- **Chatwoot's native Microsoft channel** (`Channel::Email`, `provider = microsoft`), set up by
  Lana with Paul on 11-17 on a live call ("the microservice"). Chatwoot signed in **as the
  mailbox** (delegated OAuth) and reads it over IMAP with that token. Replies sent from Chatwoot
  go out through Microsoft as the mailbox. **Verified.**
- **The Azure app** is the registration named **`chatwoot`** in tenant
  `19c2ccec-e203-4145-ace8-6d1127c4109a` (resolvable as `actuariallearning.com`). Its client id and
  secret are in `dewie-desk/.env` as `AZURE_CLIENT_ID` / `AZURE_SECRET`. **Paul holds the Azure
  portal login**; nobody else can change the app's settings.
- **Chatwoot keeps the tokens** in its own database: `channel_email.provider_config` (jsonb:
  `access_token`, `refresh_token`, `expires_on`). It renews them itself, roughly hourly
  (11-18: renewed 14:59 UTC, valid to 16:25 UTC). **Verified.** This is Chatwoot's internal
  schema, not an API: an upgrade can move it.

## 2. What the `chatwoot` app can and cannot do (verified 11-18)

| Tried | Result | Meaning |
|---|---|---|
| Client-credentials token (Graph and Outlook scopes) | Token issued, `roles: None` | Secret and tenant are right; the app has **no application permissions**. It only works on behalf of a signed-in mailbox. |
| Graph `GET /users/support@…/mailFolders/sentitems/messages` with that token | **403 ErrorAccessDenied** | No app-only mail reading. |
| Device-code sign-in as the mailbox (with and without secret) | Sign-in page succeeds, token redemption fails **`AADSTS7000218`** | The app has **"Allow public client flows" = No**. Only Paul can flip it (App registrations → chatwoot → Authentication → Advanced settings). |
| **IMAP XOAUTH2 to `outlook.office365.com` with Chatwoot's current access token** | **Works**: folder list, `Sent Items` opened read-only, headers fetched | The route we use. No portal change needed. |

## 3. What the AL mailbox looks like (verified)

- Sent folder is **`Sent Items`** (not `INBOX.Sent` as on Rackspace). There's a deep `Archive Messages/…`
  tree by quarter.
- Human replies (Clement's, from Outlook) carry **`In-Reply-To`**, so sent sync places them exactly.
- The mailbox also sends **automated mail** with no `In-Reply-To`: "Password Reset Request at the
  Actuarial University", "GOAL Feedback (Do Not Reply)". Sent sync will count these as
  `unresolved` and skip them. Expected; don't chase.
- 11-18 proof of the blind spot: Clement answered #2947 and #2959 overnight (03:30, 03:35) and the
  desk still showed both as "awaiting reply".

## 4. Probing a mailbox

`scripts/m365_mailbox_probe.py --inbox <id> [--folder "Sent Items"] [--n 5]`: read-only.
Borrows Chatwoot's token for that inbox, prints its expiry, the folders, and the last N headers.
Never prints the token. On a Rackspace (password) inbox it says so and stops. Run it first after any
Chatwoot upgrade, and on each mailbox as it moves to M365.

## 5. Sent sync for AL: the plan

The bridge's sent sync (`bridge/sent_sync.py`) already does the hard parts for ABS/ACTEX: match a
sent reply to its conversation (In-Reply-To → References → subject+recipient), post it as a
**private note only**, skip desk copies, dry run first. The bridge already records AL thread keys
from inbound webhooks (#2947, #2959 had 3–4 keys each on 11-18), so matching works as-is.

The only new piece is **how the mailbox is opened**: a per-mailbox *auth source*.

- `password` (today's ABS/XL): `MailBox(host).login(user, password)`.
- `chatwoot_oauth` (new): read the current access token from Chatwoot's DB for that inbox, then
  `MailBox("outlook.office365.com").xoauth2(user, token)`. No refresh logic of our own: if the
  token is stale, the pass reports `token_stale` and the next pass (5 min) picks up Chatwoot's
  renewed one.
- Folder per mailbox (`Sent Items`). Read-only select and `mark_seen=False`, as now.

**Later, with Paul:** an app-only permission (`Mail.Read`, scoped to the support mailboxes with an
Exchange application access policy or RBAC for Applications) removes the dependency on Chatwoot's
internals. Then the auth source becomes `app_token` (client-credentials), and nothing else changes.

## 6. The Rackspace → M365 migration: what will break

M365 turns off password (basic) auth for IMAP and SMTP, so **"provider swap is UI config only" is
not true** (README said so until 11-18). The Chatwoot inbox swap is UI config; everything below
that logs in to Rackspace with a password is not.

| Component | What it does today | At cutover |
|---|---|---|
| Chatwoot inboxes 1 (ABS), 3 (ACTEX) | Rackspace IMAP/SMTP, plain auth | Add new Microsoft-channel inboxes in the UI (as AL), retire the old ones. Inbox ids change: update every `*_INBOX_ID*` / `SENT_COPY_INBOXES` map. |
| `bridge/sent_sync.py` | Reads `INBOX.Sent` on `secure.emailsrvr.com` with a password | Switch each mailbox to `chatwoot_oauth` (or `app_token`), folder `Sent Items`. **Same change as AL**: build it once. |
| `bridge/sent_copy.py` | APPENDs desk-sent replies into the Rackspace Sent folder (`X-Dewie-Desk-Copy`) | Probably **not needed**: Chatwoot's Microsoft channel sends through the mailbox, so M365 files the reply in Sent Items itself. Verify on AL (reply from Chatwoot, then probe `Sent Items`) before retiring it, and keep sent sync's desk-copy skip working on whatever header M365 leaves. |
| DewieOps `dewie_brain/utils.py` | Worker email over `smtplib.SMTP_SSL` to `secure.emailsrvr.com` | Needs OAuth SMTP or Graph `sendMail`. That's an app permission (`Mail.Send`), so it waits on Paul's portal. The desk's SMTP endpoints for DewieOps workers fall in the same bucket. |
| DewieBrain `scripts/mail_archive.py`, `email_dewie/*`, `tools/mail_tools.py`, `mail_cats` | Rackspace IMAP (`RACKSPACE_IMAP_HOST`) | `mail_cats`/the Agent folder are retired; the others need an auth source too, or retire. Inventory before cutover. |
| Threading (`docs/threading-observations.md`) | Counts replies from `apps.rackspace.com` / `email.rackspace.com` webmail | New client strings after cutover; re-baseline. |
| Identity | One person, one address | Parallel run: the same person writes from old and new addresses. Dedupe and identity have to hold across both. |

## 7. Migration checklist (per mailbox)

1. Paul: the mailbox exists in M365 and the `chatwoot` app (or a new app) has consent for it.
2. Lana: Chatwoot → Add Inbox → Email → Microsoft, sign in as the mailbox.
3. `scripts/m365_mailbox_probe.py --inbox <new id>`: token found, `Sent Items` readable.
4. Bridge config: new inbox id in the outbound and sent maps, auth source `chatwoot_oauth`, folder
   `Sent Items`. Sent sync dry run over 3 days: replies resolve, automated mail is unresolved.
5. Reply once from Chatwoot; confirm it lands in `Sent Items` and isn't double-posted as a note.
6. Lana's go: bridge restart (elevated). Watch `/health` → `sent_sync.mailboxes.<label>`.
7. Retire the Rackspace inbox in Chatwoot when the parallel run ends.

## Open questions

- Does M365 keep any marker on a message sent from Chatwoot that sent sync can use as the desk-copy
  skip? (Test on AL, step 5.)
- App-only permissions with Paul: one app for all support mailboxes, scoped by policy, or one per
  mailbox?
- The DewieOps worker SMTP path: which messages still go out that way, and who owns the switch.

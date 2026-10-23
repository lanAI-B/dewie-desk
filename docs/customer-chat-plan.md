# Customer chat on the desk: plan (Wren, 2026-10-23, for next week)

**Goal:**
- Replace Pure Chat with Chatwoot's website chat, so chats land on the desk next to email.
- Bring customer-chat Dewie back as the first responder, with a hand-off to a person.
- Cancel the Pure Chat subscription.

This plan comes after Paula's demo (Mon 10-26). Nothing here is built yet.

## Today

- **Where Pure Chat is embedded** (all three sites):
  - ACTEXLearning `Views/Shared/_Layout.cshtml`
  - ABS `MasterPage.master` + `js/purechat.js`
  - Actex/MRB `Site.Master`
- **Missed chats** arrive as "You received a question while you were away" emails in the ABS
  inbox, even for ACTEX customers (#1886 on 10-22 was an ACTEX receipt page).
- **Transcripts** come from `noreply@purechat.com`, and Lana's rule 1 ("Spam and promos": sender
  contains `noreply`) auto-resolves them. So today chat history effectively never reaches
  anyone.
- **The old chatbot** (8621, `chat.biscuitfactory.cc`) is decommissioned. Its hostname is a
  dormant line on the desk tunnel.

## Shape

1. **Two Website inboxes** in Chatwoot, `ABS Chat` and `ACTEX Chat` (the latter covering ACTEX +
   MRB). The store matters: drafts sign as the store, and order lookups need it.
2. **A public path of its own.**
   - Per the 10-20 rule, it gets a new tunnel, `dewie-chat`, never the desk tunnel. The dormant
     `chat.` line comes off the desk tunnel.
   - The tunnel's ingress passes only what the widget needs to Chatwoot (`/widget`,
     `/api/v1/widget/*`, `/cable`, `/packs`, `/assets`). Everything else returns 404, so the admin
     UI is never reachable from the chat host.
   - **To verify first, on QA:** the widget's websocket and asset URLs follow the embed's
     `baseUrl` (the chat host), not `FRONTEND_URL` (the desk host).
3. **Hours and offline:** Chatwoot business hours, plus a pre-chat form (name and email). An
   offline chat becomes a normal conversation the desk answers by email. That replaces the "while
   you were away" mails.
4. **Dewie as an Agent Bot:**
   - Chatwoot posts each new message to a bot webhook in the bridge (dewie-desk owns the
     transport).
   - The bridge asks DewieOps's drafter or chat runtime for the reply (DewieOps owns the
     drafter). The bot answers, or hands off by moving the conversation from `pending` to `open`.
   - Rolled out in stages (below), because a live bot is a new kind of customer-facing send.
5. **Transcripts:**
   - Chatwoot keeps every chat on the desk, so we stop depending on Pure Chat's emails.
   - Emailing a transcript to the customer, or continuing an offline chat by email, needs Chatwoot
     to send mail. It has no SMTP host today (the reason the 10-23 invites couldn't send).
     **That touches SMTP, so it needs Lana's go.**

## Stages

| Stage | What | Customer sees | Gate |
|---|---|---|---|
| **A. Widget, people only** | Two inboxes, `dewie-chat` tunnel, pre-chat form, hours. The widget is embedded on **one** site first (ABS, the smallest). | Chatwoot chat, answered by the team | Lana's go on the tunnel and the embed |
| **B. Bot in shadow** | The bot writes its suggested reply as a **private note**; a person sends it or fixes it. Same review loop as the email drafts, and we measure the fix rate. | No change | None beyond A |
| **C. Bot live, narrow** | The bot answers live **only** in topics where stage B showed it right (order status with email + order number, access how-tos, product facts from the KB). Anything else, and anything about money, hands off at once. | Instant answers, a person for the rest | **Lana's explicit go**: an unreviewed customer-facing reply is new authority |
| **D. Cutover** | Embed on ACTEX + MRB, then remove Pure Chat from all three layouts and cancel the subscription | n/a | After a week of A–C running clean |

## Guardrails

- **The bot's tools are read-only.** It never moves money, changes orders, or promises refunds or
  exceptions.
- **Identity:** an order lookup needs the email and order number to match before the bot reveals
  anything.
- **The public widget:** rate limited at Cloudflare, CSAT off, and no admin paths on the chat host.
- **The store embeds** are Paul's or Claire's changes (the webstore repos are read-only to us).
  We send them a one-line script swap per site.

## Decisions for Lana (with defaults)

1. **Which site goes first** in stage A. Default: ABS.
2. **Chatwoot SMTP for transcripts and offline follow-up:** yes or no, and which mailbox. Default:
   none for now; offline chats get answered from the desk's normal email inboxes.
3. **Chat hours:** default to the store hours already on the autoreply (Mon–Thu 8:30–5:30 ET,
   Fri 8:30–5:00 ET).
4. **Who takes chats** once Marta and Paula are on the desk. Default: Lana, plus whoever opts in.

## Rough size

- **A:** a day: inboxes, tunnel, QA'd on Frankie first.
- **B:** 2–3 days: the bot webhook in the bridge, reusing the drafter.
- **C:** a config change plus a topic allow-list, after a week of B.
- **D:** depends on Paul and Claire for the embeds.

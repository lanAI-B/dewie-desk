# Attended QA prompt: Chatwoot transport and refund notification

Use only after both feature plans record PM acceptance of their local code commits.
This is an attended QA operation, not a manual feature-coder run.

```text
Act as the attended QA integrator for the Chatwoot outbound transport and refund
notification consumer. Read:
C:\Users\Owner\source\repos\DewieBrain\AGENTS.md
C:\Users\Owner\source\repos\dewie-desk-chatwoot-outbound\docs\feature-trial\plan.md
C:\Users\Owner\source\repos\DewieOps-refund-chatwoot\docs\feature-trial\plan.md

First verify both plans record PM acceptance, exact commits, clean worktrees, and
passing local checks. Stop if either is missing. Then inspect the actual Frankie
QA Compose ownership and current container/image revisions before changing it.

Lana has authorized QA-only integration testing through the Chatwoot QA account
linked to her personal testing inbox. Build immutable-in-practice artifacts from
the accepted commits and change only the confirmed QA services. Do not touch the
office production machine, production Discord token, production mailbox, real
customer conversations, schedulers, Stripe, or store databases.

Use one clearly labeled test Chatwoot conversation in Lana's personal testing
inbox. Prove, in order:
1. unauthenticated/incorrectly authenticated outbound requests fail closed;
2. one synthetic fixed system message is accepted once in the existing thread;
3. the same idempotency key cannot create a second customer email;
4. private-note posting is still private and cannot be confused with public send;
5. one synthetic processed-refund fixture produces the fixed notice through the
   refund button for Lana's QA identity only;
6. denied/unset authorization sends nothing;
7. a duplicate click sends nothing; and
8. definitive rejection versus timeout/ambiguous delivery leaves an honest,
   reconcilable state and never falsely shows Sent.

Use only synthetic refund/order identifiers and no real payment/refund action.
If shared QA data is required, label it as test data, record every row/message
created, and do not delete evidence needed for review. Capture Chatwoot
conversation/message IDs, Message-ID or equivalent delivery identity, transport
request ID, timestamps, container image IDs, embedded Git revisions, exact test
commands, and logs without secrets.

Stop after the QA evidence packet and plan updates are committed on their feature
branches. Report separately: implemented, committed, integrated, image built,
QA deployed, QA message delivered, and production unchanged. Do not push, merge,
cut over, restart production, send another message, or declare production ready.
```

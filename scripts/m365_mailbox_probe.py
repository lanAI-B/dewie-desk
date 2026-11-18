"""Read-only probe of a Microsoft (M365) mailbox that a Chatwoot inbox is connected to.

Borrows the access token Chatwoot already holds for that inbox (it renews it itself, about
hourly) and opens the mailbox over IMAP XOAUTH2, read-only. Prints the token's expiry, the
folder list, and the last N headers of one folder. Never prints the token; never writes to
the mailbox, Chatwoot, or the bridge state.

    python scripts/m365_mailbox_probe.py --inbox 6
    python scripts/m365_mailbox_probe.py --inbox 6 --folder INBOX --n 10

See docs/m365-mailbox-access.md for why this works and what it depends on.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

POSTGRES_CONTAINER = "dewie-desk-postgres-1"
IMAP_HOST = "outlook.office365.com"

# Chatwoot keeps a Microsoft channel's OAuth tokens in channel_email.provider_config
# (jsonb: access_token, refresh_token, expires_on). Internal schema: a Chatwoot upgrade can move it.
SQL = """
select c.email, c.provider, c.provider_config::jsonb->>'expires_on', c.provider_config::jsonb->>'access_token'
from channel_email c
join inboxes i on i.channel_id = c.id and i.channel_type = 'Channel::Email'
where i.id = {inbox:d};
"""


def chatwoot_token(inbox: int) -> tuple[str, str, str, str]:
    out = subprocess.run(
        ["docker", "exec", "-i", POSTGRES_CONTAINER, "sh", "-c",
         'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F "|" -f -'],
        input=SQL.format(inbox=inbox), capture_output=True, text=True, timeout=30,
    )
    if out.returncode:
        sys.exit(f"psql failed: {out.stderr.strip()[:200]}")
    row = out.stdout.strip()
    if not row:
        sys.exit(f"inbox {inbox}: not an email inbox")
    email, provider, expires_on, token = row.split("|", 3)
    if provider != "microsoft" or not token:
        sys.exit(f"inbox {inbox} ({email}): provider {provider or 'imap'}, no Microsoft token to borrow")
    return email, provider, expires_on, token


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--inbox", type=int, required=True, help="Chatwoot inbox id (AL = 6)")
    p.add_argument("--folder", default="Sent Items")
    p.add_argument("--n", type=int, default=5)
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from imap_tools import AND, MailBox

    email, _, expires_on, token = chatwoot_token(args.inbox)
    print(f"inbox {args.inbox}: {email}, Chatwoot token expires {expires_on}")
    with MailBox(IMAP_HOST, timeout=30).xoauth2(email, token, initial_folder=None) as mb:
        print("folders:", ", ".join(f.name for f in mb.folder.list()))
        mb.folder.set(args.folder, readonly=True)
        for m in mb.fetch(AND(all=True), mark_seen=False, reverse=True, limit=args.n, headers_only=True):
            irt = "in-reply-to" if m.headers.get("in-reply-to") else "no in-reply-to"
            print(f"  {m.date:%m-%d %H:%M} | {m.subject[:70]} | {irt}")


if __name__ == "__main__":
    main()

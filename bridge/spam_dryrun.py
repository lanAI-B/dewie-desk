"""Read-only spam dry-run over recent Chatwoot conversations.

    python spam_dryrun.py --days 2 --out spam-dryrun.md [--classifier]

Runs the same rules the bridge uses over the newest customer message of every
conversation active in the window and writes a Markdown report: what would be
resolved, and what looks like a real customer and needs a human look.

Only GET requests are made. Listing conversations is an account-wide endpoint
Chatwoot denies to AgentBot tokens, so this needs a user access token
(CHATWOOT_API_TOKEN of an agent/admin). ``--classifier`` spends one model call
per conversation that no rule decided.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import spam
from parser import newest_customer_message


class ReadOnlyChatwoot:
    """GET-only client; there is no method that can write."""

    def __init__(self, base_url: str, account_id: str, token: str, timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.account_id = account_id
        self._headers = {"api_access_token": token}
        self.timeout = timeout

    def get(self, path: str, **params) -> dict:
        response = requests.get(
            f"{self.base}/api/v1/accounts/{self.account_id}{path}",
            headers=self._headers, params=params, timeout=self.timeout,
        )
        if response.status_code // 100 != 2:
            raise RuntimeError(f"GET {path} -> HTTP {response.status_code}")
        return response.json()

    def recent_conversations(self, since: float, max_pages: int = 100):
        for page in range(1, max_pages + 1):
            data = self.get("/conversations", status="all", page=page,
                            sort_by="last_activity_at_desc")
            items = (data.get("data") or {}).get("payload") or []
            if not items:
                return
            fresh = [c for c in items if (c.get("last_activity_at") or 0) >= since]
            yield from fresh
            if len(fresh) < len(items):
                return


def _row(fields) -> str:
    return "| " + " | ".join(str(f).replace("|", "\\|").replace("\n", " ") for f in fields) + " |"


def run(days: float, out: Path, use_classifier: bool) -> dict:
    client = ReadOnlyChatwoot(
        os.environ.get("CHATWOOT_BASE_URL") or "http://127.0.0.1:3000",
        os.environ.get("CHATWOOT_ACCOUNT_ID", "1"),
        os.environ.get("CHATWOOT_API_TOKEN", ""),
    )
    if use_classifier:
        os.environ["SPAM_CLASSIFIER_ENABLED"] = "true"
        import dewie_brain.desk  # noqa: F401 - fail loudly, not as "no evidence"
        import main

        classify = main._spam_classify
    else:
        classify = None

    since = time.time() - days * 86400
    would, check, vetoed, clean, skipped, failed = [], [], [], 0, 0, 0
    for conversation in client.recent_conversations(since):
        cid = conversation.get("id")
        data = client.get(f"/conversations/{cid}/messages")
        meta = data.get("meta") or {}
        sender = (conversation.get("meta") or {}).get("sender") or {}
        meta.setdefault("contact", sender)
        meta.setdefault("additional_attributes", conversation.get("additional_attributes") or {})
        message = newest_customer_message(
            data.get("payload") or [], conversation_id=cid, meta=meta,
        )
        if message is None:
            skipped += 1
            continue
        verdict = spam.evaluate(message, classify=classify)
        entry = {
            "id": cid,
            "status": conversation.get("status"),
            "inbox": conversation.get("inbox_id"),
            "sender": message.from_email,
            "subject": message.subject or "(no subject)",
            "rule": verdict.reason,
            "labels": ",".join(conversation.get("labels") or []),
            "signals": "; ".join(spam.customer_signals(message.from_email, message.subject)),
        }
        if verdict.spam:
            would.append(entry)
            if entry["signals"]:
                check.append(entry)
        elif verdict.stage == "veto":
            vetoed.append(entry)
        else:
            clean += 1
            failed += verdict.reason.startswith("classifier failed")

    total = len(would) + len(vetoed) + clean + skipped
    lines = [
        "# Chatwoot spam auto-resolve — dry run",
        "",
        f"- Instance: `{client.base}` account {client.account_id}",
        f"- Window: last {days:g} days (by last activity); generated "
        f"{time.strftime('%Y-%m-%d %H:%M %Z')}",
        f"- Rules: `bridge/spam_rules.csv` ({len(spam.load_rules())} rows); "
        f"classifier stage {'ON' if use_classifier else 'off'}",
        f"- Conversations examined: **{total}** — would resolve **{len(would)}**, "
        f"vetoed **{len(vetoed)}**, left alone **{clean}**, no customer message {skipped}"
        + (f", classifier failures {failed}" if use_classifier else ""),
        "- Read-only: GET requests only. Nothing was labelled, resolved or posted.",
        "",
        f"## Would resolve ({len(would)})",
        "",
        _row(["conv", "status", "inbox", "sender", "subject", "rule"]),
        _row(["---"] * 6),
        *[_row([e["id"], e["status"], e["inbox"], e["sender"], e["subject"], e["rule"]])
          for e in would],
        "",
        f"## Looks like a real customer — please check ({len(check) + len(vetoed)})",
        "",
        "Would-resolve rows with customer signals, plus rows a rule matched but the "
        "customer-subject veto kept open.",
        "",
        _row(["conv", "sender", "subject", "rule", "why it might be real"]),
        _row(["---"] * 5),
        *[_row([e["id"], e["sender"], e["subject"], e["rule"], e["signals"]]) for e in check],
        *[_row([e["id"], e["sender"], e["subject"], e["rule"], "vetoed — stays open"])
          for e in vetoed],
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return {"total": total, "would": len(would), "check": len(check),
            "vetoed": len(vetoed), "clean": clean, "skipped": skipped}


def main_cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=float, default=2.0)
    parser.add_argument("--out", type=Path, default=Path("spam-dryrun.md"))
    parser.add_argument("--classifier", action="store_true")
    args = parser.parse_args(argv)
    counts = run(args.days, args.out, args.classifier)
    print(f"wrote {args.out}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())

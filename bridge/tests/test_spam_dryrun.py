import time

import spam_dryrun


def test_dry_run_report_lists_would_resolve_and_customer_checks(monkeypatch, tmp_path):
    now = time.time()
    conversations = [
        {"id": 1, "status": "open", "inbox_id": 1, "last_activity_at": now, "labels": [],
         "meta": {"sender": {"email": "ebay@ebay.com"}},
         "additional_attributes": {"mail_subject": "Deals"}},
        {"id": 2, "status": "open", "inbox_id": 1, "last_activity_at": now, "labels": [],
         "meta": {"sender": {"email": "jane@gmail.com"}},
         "additional_attributes": {"mail_subject": "FREE"}},
        {"id": 3, "status": "open", "inbox_id": 1, "last_activity_at": now, "labels": [],
         "meta": {"sender": {"email": "sam@gmail.com"}},
         "additional_attributes": {"mail_subject": "FREE order #12345"}},
        {"id": 4, "status": "open", "inbox_id": 1, "last_activity_at": now - 9 * 86400,
         "labels": [], "meta": {"sender": {"email": "ebay@ebay.com"}},
         "additional_attributes": {"mail_subject": "old"}},
    ]
    calls = []

    def get(self, path, **params):
        calls.append(path)
        if path == "/conversations":
            return {"data": {"payload": conversations if params["page"] == 1 else []}}
        cid = int(path.split("/")[2])
        email = conversations[cid - 1]["meta"]["sender"]["email"]
        return {"meta": {}, "payload": [{
            "id": 100 + cid, "message_type": 0, "private": False, "content": "hi",
            "sender": {"type": "contact", "email": email},
        }]}

    monkeypatch.setattr(spam_dryrun.ReadOnlyChatwoot, "get", get)
    out = tmp_path / "report.md"
    counts = spam_dryrun.run(2, out, use_classifier=False)

    assert counts == {"total": 3, "would": 2, "check": 1, "vetoed": 1, "clean": 0, "skipped": 0}
    text = out.read_text(encoding="utf-8")
    assert "ebay@ebay.com" in text and "personal mailbox (gmail.com)" in text
    assert "vetoed" in text
    assert "/conversations/4/messages" not in calls

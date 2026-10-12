"""Sent sync on its own: every mailbox, a guarded periodic loop, /health, the CLI.

No network: IMAP is a fake ``fetch`` (or a fake MailBox), Chatwoot is a fake
client that fails the test on anything but ``post_private_note``.
"""

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from parser import parse_message_created
from state import DedupStore
import sent_sync
from test_sent_sync import FakeClient, inbound_payload, sent_bytes

SECRET_ABS = "abs-imap-secret-value"
SECRET_ACTEX = "actex-imap-secret-value"
SENT_SYNC_ENV = (
    "SENT_SYNC_ENABLED",
    "SENT_SYNC_INTERVAL_SECONDS",
    "SENT_SYNC_LOOKBACK_DAYS",
    "SENT_SYNC_USERNAME",
    "SENT_SYNC_PASSWORD",
    "SENT_SYNC_IMAP_HOST",
    "SENT_SYNC_FOLDER",
    "SENT_COPY_INBOXES",
    "BRIDGE_DRY_RUN",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in SENT_SYNC_ENV:
        monkeypatch.delenv(name, raising=False)
    for label in ("ABS", "ACTEX"):
        for suffix in ("USERNAME", "PASSWORD", "FOLDER", "IMAP_HOST", "IMAP_PORT", "FROM"):
            monkeypatch.delenv(f"SENT_COPY_{label}_{suffix}", raising=False)


def configure_both(monkeypatch, *, actex_folder="INBOX.Sent"):
    monkeypatch.setenv("SENT_COPY_INBOXES", "1:abs,3:actex")
    monkeypatch.setenv("SENT_COPY_ABS_USERNAME", "orders@actuarialbookstore.com")
    monkeypatch.setenv("SENT_COPY_ABS_PASSWORD", SECRET_ABS)
    monkeypatch.setenv("SENT_COPY_ABS_FOLDER", "INBOX.Sent")
    monkeypatch.setenv("SENT_COPY_ACTEX_USERNAME", "support@actexlearning.com")
    monkeypatch.setenv("SENT_COPY_ACTEX_PASSWORD", SECRET_ACTEX)
    monkeypatch.setenv("SENT_COPY_ACTEX_IMAP_PORT", "994")
    if actex_folder is not None:
        monkeypatch.setenv("SENT_COPY_ACTEX_FOLDER", actex_folder)


class FakeFetch:
    """Stands in for fetch_sent_replies: replies per mailbox username."""

    def __init__(self, by_username=None, raises=None):
        self.by_username = by_username or {}
        self.raises = raises or {}
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        username = kwargs["username"]
        if username in self.raises:
            raise self.raises[username]
        return list(self.by_username.get(username, ()))


@pytest.fixture
def store(tmp_path):
    value = DedupStore(tmp_path / "bridge.sqlite3")
    value.record_inbound(parse_message_created(inbound_payload()))
    second = inbound_payload(id=43, conversation={"id": 8, "channel": "Channel::Email"})
    second["content_attributes"]["email"].update(
        message_id="<inbound-2@mail.example>", subject="Order 5512"
    )
    second["sender"] = {"email": "buyer@example.com", "type": "contact"}
    value.record_inbound(parse_message_created(second))
    return value


def abs_reply():
    return sent_sync.parse_sent_email(sent_bytes(
        message_id="<abs-1@outlook.example>",
        in_reply_to="<inbound-2@mail.example>",
        subject="Re: Order 5512",
        to="buyer@example.com",
        body="Your order shipped.",
    ))


def actex_reply():
    return sent_sync.parse_sent_email(sent_bytes())


# ── 1. Both mailboxes, sent_copy's credentials ────────────────────────────────


def test_every_mailbox_is_synced_with_sent_copy_credentials(monkeypatch, store):
    configure_both(monkeypatch)
    fetch = FakeFetch({
        "orders@actuarialbookstore.com": [abs_reply()],
        "support@actexlearning.com": [actex_reply()],
    })
    client = FakeClient()

    report = sent_sync.run_sent_sync(store=store, client=client, dry_run=False, fetch=fetch)

    assert list(report.mailboxes) == ["abs", "actex"]
    assert report.mailboxes["abs"].counts["private_notes_posted"] == 1
    assert report.mailboxes["actex"].counts["private_notes_posted"] == 1
    assert report.totals()["private_notes_posted"] == 2
    assert sorted(call[1] for call in client.calls) == [7, 8]
    by_user = {call["username"]: call for call in fetch.calls}
    assert by_user["orders@actuarialbookstore.com"] == {
        "host": "secure.emailsrvr.com",
        "port": 993,
        "username": "orders@actuarialbookstore.com",
        "password": SECRET_ABS,
        "folder": "INBOX.Sent",
        "lookback_days": 3,
    }
    assert by_user["support@actexlearning.com"]["port"] == 994


def test_a_misconfigured_mailbox_is_skipped_and_the_other_still_syncs(monkeypatch, store):
    configure_both(monkeypatch, actex_folder=None)  # no folder: there is no default
    fetch = FakeFetch({"orders@actuarialbookstore.com": [abs_reply()]})
    client = FakeClient()

    report = sent_sync.run_sent_sync(store=store, client=client, dry_run=False, fetch=fetch)

    assert report.mailboxes["actex"].problem == "mailbox_folder_missing"
    assert report.mailboxes["actex"].counts["mailbox_skipped"] == 1
    assert report.mailboxes["abs"].counts["private_notes_posted"] == 1
    assert [call["username"] for call in fetch.calls] == ["orders@actuarialbookstore.com"]
    assert [call[1] for call in client.calls] == [8]


def test_a_mailbox_whose_fetch_fails_never_stops_the_other(monkeypatch, store):
    configure_both(monkeypatch)
    fetch = FakeFetch(
        {"support@actexlearning.com": [actex_reply()]},
        raises={"orders@actuarialbookstore.com": ConnectionRefusedError(SECRET_ABS)},
    )
    client = FakeClient()

    report = sent_sync.run_sent_sync(store=store, client=client, dry_run=False, fetch=fetch)

    assert report.mailboxes["abs"].problem == "fetch_failed:ConnectionRefusedError"
    assert report.mailboxes["actex"].counts["private_notes_posted"] == 1
    assert SECRET_ABS not in json.dumps(report.as_dict())


def test_no_inbox_map_is_reported_and_reads_nothing(monkeypatch, store):
    monkeypatch.setenv("SENT_SYNC_USERNAME", "old@example.com")  # removed; ignored
    monkeypatch.setenv("SENT_SYNC_PASSWORD", "old-secret")
    fetch = FakeFetch()

    report = sent_sync.run_sent_sync(store=store, client=FakeClient(), fetch=fetch)

    assert report.problem == "inbox_map_not_configured"
    assert report.mailboxes == {}
    assert fetch.calls == []


def test_a_label_mapped_from_two_inboxes_is_read_once(monkeypatch, store):
    configure_both(monkeypatch)
    monkeypatch.setenv("SENT_COPY_INBOXES", "1:abs,2:abs,3:actex")
    fetch = FakeFetch()

    sent_sync.run_sent_sync(store=store, client=FakeClient(), fetch=fetch)

    assert [call["username"] for call in fetch.calls] == [
        "orders@actuarialbookstore.com",
        "support@actexlearning.com",
    ]


def test_one_mailbox_can_be_selected(monkeypatch, store):
    configure_both(monkeypatch)
    fetch = FakeFetch()

    report = sent_sync.run_sent_sync(store=store, client=FakeClient(), fetch=fetch, mailbox="ACTEX")
    missing = sent_sync.run_sent_sync(store=store, client=FakeClient(), fetch=fetch, mailbox="nope")

    assert list(report.mailboxes) == ["actex"]
    assert missing.problem == "mailbox_not_configured:nope"
    assert [call["username"] for call in fetch.calls] == ["support@actexlearning.com"]


def test_bridge_dry_run_decides_posting_when_not_overridden(monkeypatch, store):
    configure_both(monkeypatch)
    fetch = FakeFetch({"support@actexlearning.com": [actex_reply()]})
    client = FakeClient()

    default = sent_sync.run_sent_sync(store=store, client=client, fetch=fetch)
    assert default.totals()["notes_avoided_dry_run"] == 1
    assert client.calls == []

    monkeypatch.setenv("BRIDGE_DRY_RUN", "false")
    live = sent_sync.run_sent_sync(store=store, client=client, fetch=fetch)
    assert live.totals()["private_notes_posted"] == 1
    assert [call[0] for call in client.calls] == ["post_private_note"]


def test_lookback_comes_from_the_environment(monkeypatch, store):
    configure_both(monkeypatch)
    fetch = FakeFetch()

    sent_sync.run_sent_sync(store=store, client=FakeClient(), fetch=fetch)
    monkeypatch.setenv("SENT_SYNC_LOOKBACK_DAYS", "9")
    sent_sync.run_sent_sync(store=store, client=FakeClient(), fetch=fetch)
    sent_sync.run_sent_sync(store=store, client=FakeClient(), fetch=fetch, lookback_days=1)

    assert [call["lookback_days"] for call in fetch.calls] == [3, 3, 9, 9, 1, 1]


def test_interval_defaults_and_rejects_nonsense(monkeypatch):
    assert sent_sync.interval_seconds() == 300
    monkeypatch.setenv("SENT_SYNC_INTERVAL_SECONDS", "60")
    assert sent_sync.interval_seconds() == 60
    for bad in ("0", "-5", "soon", ""):
        monkeypatch.setenv("SENT_SYNC_INTERVAL_SECONDS", bad)
        assert sent_sync.interval_seconds() == 300


# ── Invariants across passes ──────────────────────────────────────────────────


def test_one_claim_per_message_across_passes_and_mailboxes(monkeypatch, tmp_path):
    """The same message seen in both mailboxes and on a later pass posts once."""
    configure_both(monkeypatch)
    path = tmp_path / "bridge.sqlite3"
    DedupStore(path).record_inbound(parse_message_created(inbound_payload()))
    reply = actex_reply()
    fetch = FakeFetch({
        "orders@actuarialbookstore.com": [reply],
        "support@actexlearning.com": [reply],
    })
    client = FakeClient()

    first = sent_sync.run_sent_sync(store=DedupStore(path), client=client, dry_run=False, fetch=fetch)
    after_restart = sent_sync.run_sent_sync(
        store=DedupStore(path), client=client, dry_run=False, fetch=fetch
    )

    assert first.totals()["private_notes_posted"] == 1
    assert first.totals()["duplicate_sent"] == 1
    assert after_restart.totals()["duplicate_sent"] == 2
    assert len(client.calls) == 1


def test_a_failed_post_is_retried_by_the_next_pass(monkeypatch, store):
    configure_both(monkeypatch)
    fetch = FakeFetch({"support@actexlearning.com": [actex_reply()]})

    failed = sent_sync.run_sent_sync(
        store=store, client=FakeClient(ok=False), dry_run=False, fetch=fetch
    )
    retried = sent_sync.run_sent_sync(store=store, client=FakeClient(), dry_run=False, fetch=fetch)

    assert failed.totals()["note_post_failed"] == 1
    assert retried.totals()["private_notes_posted"] == 1


def test_desk_copies_are_skipped_in_every_mailbox(monkeypatch, store):
    configure_both(monkeypatch)
    copy = sent_sync.parse_sent_email(sent_bytes(
        message_id="<conversation/abc/messages/501@mail.example>",
        extra_headers=("X-Dewie-Desk-Copy: 501",),
    ))
    fetch = FakeFetch({
        "orders@actuarialbookstore.com": [copy],
        "support@actexlearning.com": [copy],
    })
    client = FakeClient()

    report = sent_sync.run_sent_sync(store=store, client=client, dry_run=False, fetch=fetch)

    assert report.totals()["desk_copy_skipped"] == 2
    assert client.calls == []


def test_the_real_fetch_is_read_only_for_every_mailbox(monkeypatch, store):
    """Through the real fetch_sent_replies: read-only select, mark_seen=False, nothing else."""
    import imap_tools

    configure_both(monkeypatch)
    seen = []

    class FakeFolderManager:
        def __init__(self, owner):
            self.owner = owner

        def set(self, folder, readonly=False):
            self.owner["folder"] = (folder, readonly)

    class FakeMailbox:
        def __init__(self, host, port=993, timeout=None):
            self.record = {"host": host, "port": port}
            seen.append(self.record)
            self.folder = FakeFolderManager(self.record)

        def login(self, username, password, initial_folder="INBOX"):
            self.record.update(username=username, initial_folder=initial_folder)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch(self, criteria, **kwargs):
            self.record["mark_seen"] = kwargs.get("mark_seen")
            return []

        def __getattr__(self, name):  # move, flag, delete, append, expunge, ...
            raise AssertionError(f"sent sync must not call mailbox.{name}")

    monkeypatch.setattr(imap_tools, "MailBox", FakeMailbox)

    report = sent_sync.run_sent_sync(store=store, client=FakeClient(), dry_run=False)

    assert report.problem == ""
    assert [record["port"] for record in seen] == [993, 994]
    for record in seen:
        assert record["initial_folder"] is None
        assert record["folder"] == ("INBOX.Sent", True)
        assert record["mark_seen"] is False


# ── 2. The periodic loop ──────────────────────────────────────────────────────


def test_overlap_guard_skips_a_pass_while_one_is_running():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_pass():
        calls.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return sent_sync.PassReport()

    runner = sent_sync.SentSyncRunner(slow_pass)
    worker = threading.Thread(target=runner.run_once)
    worker.start()
    assert entered.wait(5)

    assert runner.run_once() is False
    release.set()
    worker.join(5)

    assert len(calls) == 1
    snapshot = runner.snapshot()
    assert snapshot["overlaps_skipped"] == 1
    assert snapshot["passes"] == 1
    assert runner.run_once() is True  # the guard is released afterwards


def test_a_pass_that_raises_is_counted_and_the_loop_carries_on():
    calls = []

    def flaky_pass():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            raise RuntimeError("imap exploded with secret detail")
        return sent_sync.PassReport()

    runner = sent_sync.SentSyncRunner(flaky_pass)

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(runner.run_forever(0.01, stop))
        for _ in range(500):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 2)
        return threading.get_ident()

    loop_thread = asyncio.run(drive())

    snapshot = runner.snapshot()
    assert len(calls) >= 3
    assert snapshot["failures"] == 1
    assert snapshot["passes"] >= 3
    assert snapshot["last_error"] is None  # cleared by the passes that followed
    assert snapshot["loop_running"] is False
    assert loop_thread not in calls  # the blocking pass ran in a worker thread


def test_a_failed_pass_reports_only_the_exception_type():
    def failing():
        raise TimeoutError(f"LOGIN failed for {SECRET_ABS}")

    runner = sent_sync.SentSyncRunner(failing)
    runner.run_once()

    snapshot = runner.snapshot()
    assert snapshot["last_error"] == "TimeoutError"
    assert snapshot["last_run_at"] is not None
    assert SECRET_ABS not in json.dumps(snapshot)


def test_last_counts_are_per_mailbox(monkeypatch, store):
    configure_both(monkeypatch, actex_folder=None)
    fetch = FakeFetch({"orders@actuarialbookstore.com": [abs_reply()]})
    runner = sent_sync.SentSyncRunner(
        lambda: sent_sync.run_sent_sync(store=store, client=FakeClient(), fetch=fetch)
    )

    runner.run_once()

    counts = runner.snapshot()["last_counts"]
    assert counts["abs"] == {
        "counts": {"sent_seen": 1, "resolved_in_reply_to": 1, "notes_avoided_dry_run": 1},
        "problem": None,
    }
    assert counts["actex"] == {"counts": {"mailbox_skipped": 1}, "problem": "mailbox_folder_missing"}


@pytest.mark.parametrize("value", [None, "false", "0", "no", ""])
def test_the_loop_never_starts_unless_enabled(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("SENT_SYNC_ENABLED", value)
    calls = []
    runner = sent_sync.SentSyncRunner(lambda: calls.append(1))

    async def start():
        return sent_sync.start_loop(runner)

    assert asyncio.run(start()) == (None, None)
    assert calls == []


def test_app_startup_does_not_run_sent_sync_when_disabled(monkeypatch):
    import main

    calls = []
    monkeypatch.setattr(main, "_sent_sync_runner", sent_sync.SentSyncRunner(lambda: calls.append(1)))

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
    assert calls == []
    assert main._sent_sync_runner.snapshot()["loop_running"] is False


def test_app_startup_runs_passes_in_the_background_when_enabled(monkeypatch):
    import main

    monkeypatch.setenv("SENT_SYNC_ENABLED", "true")
    monkeypatch.setenv("SENT_SYNC_INTERVAL_SECONDS", "3600")
    calls = []

    def fake_pass():
        calls.append(threading.current_thread().name)
        return sent_sync.PassReport()

    runner = sent_sync.SentSyncRunner(fake_pass)
    monkeypatch.setattr(main, "_sent_sync_runner", runner)

    with TestClient(main.app) as client:
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        body = client.get("/health").json()["sent_sync"]

    assert len(calls) == 1  # one pass now; the next is an hour away
    assert body["enabled"] is True
    assert body["loop_running"] is True
    assert body["passes"] == 1
    assert runner.snapshot()["loop_running"] is False  # stopped on shutdown


# ── 3. /health ────────────────────────────────────────────────────────────────


def test_health_reports_sent_sync_without_secrets(monkeypatch):
    import main

    configure_both(monkeypatch, actex_folder=None)
    monkeypatch.setenv("SENT_SYNC_INTERVAL_SECONDS", "120")
    runner = sent_sync.SentSyncRunner(lambda: sent_sync.PassReport())
    runner.run_once()
    monkeypatch.setattr(main, "_sent_sync_runner", runner)

    response = TestClient(main.app).get("/health")
    block = response.json()["sent_sync"]

    assert block["enabled"] is False
    assert block["interval_seconds"] == 120
    assert block["lookback_days"] == 3
    assert block["mailboxes"] == {
        "abs": {"ready": True, "problem": None, "folder": "INBOX.Sent"},
        "actex": {"ready": False, "problem": "mailbox_folder_missing", "folder": None},
    }
    assert block["last_run_at"] is not None
    assert block["last_counts"] == {}
    assert block["last_error"] is None
    assert SECRET_ABS not in response.text
    assert SECRET_ACTEX not in response.text


def test_health_reports_an_unconfigured_map():
    status = sent_sync.sent_sync_status()

    assert status["problem"] == "inbox_map_not_configured"
    assert status["mailboxes"] == {}
    assert status["last_run_at"] is None


# ── 5. Attended CLI ───────────────────────────────────────────────────────────


def test_cli_is_a_dry_run_by_default_and_reports_per_mailbox(monkeypatch, store, capsys):
    configure_both(monkeypatch)
    monkeypatch.setenv("BRIDGE_DRY_RUN", "false")  # the CLI ignores it: --post decides
    fetch = FakeFetch({
        "orders@actuarialbookstore.com": [abs_reply()],
        "support@actexlearning.com": [actex_reply()],
    })
    client = FakeClient()

    code = sent_sync.main([], store=store, client=client, fetch=fetch)

    out = capsys.readouterr().out
    assert code == 0
    assert client.calls == []
    assert "dry run" in out
    assert "== abs ==" in out and "== actex ==" in out
    assert "notes_avoided_dry_run: 1" in out
    assert "notes_avoided_dry_run: 2" in out  # totals


def test_cli_post_lookback_and_mailbox(monkeypatch, store, capsys):
    configure_both(monkeypatch)
    fetch = FakeFetch({
        "orders@actuarialbookstore.com": [abs_reply()],
        "support@actexlearning.com": [actex_reply()],
    })
    client = FakeClient()

    code = sent_sync.main(
        ["--post", "--lookback-days", "14", "--mailbox", "actex", "--json"],
        store=store,
        client=client,
        fetch=fetch,
    )

    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):])
    assert code == 0
    assert [call["username"] for call in fetch.calls] == ["support@actexlearning.com"]
    assert fetch.calls[0]["lookback_days"] == 14
    assert list(report["mailboxes"]) == ["actex"]
    assert report["mailboxes"]["actex"]["counts"]["private_notes_posted"] == 1
    assert [call[:2] for call in client.calls] == [("post_private_note", 7)]


def test_cli_does_not_need_the_loop_flag_but_fails_on_an_unknown_mailbox(monkeypatch, store, capsys):
    configure_both(monkeypatch)

    code = sent_sync.main(["--mailbox", "nope"], store=store, client=FakeClient(), fetch=FakeFetch())

    assert code == 1
    assert "mailbox_not_configured:nope" in capsys.readouterr().out

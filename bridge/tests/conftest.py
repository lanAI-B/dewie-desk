"""Test the bridge against the sibling DewieOps checkout required in production."""

import os
import sys
from pathlib import Path

import dotenv
import pytest
import requests

# main.py (and the DewieOps drafter) call load_dotenv() on import. In the live
# checkout that reads the real dewie-desk/.env, so tests saw live inbox ids and
# settings and failed only there. Disarm it before any bridge module is imported;
# `from dotenv import load_dotenv` then binds this no-op.
dotenv.load_dotenv = lambda *args, **kwargs: False

REQUIRE_DEWIEOPS = os.environ.get("REQUIRE_DEWIEOPS", "").strip() == "1"


def _dewieops_root() -> Path | None:
    """The DewieOps checkout under test: env override first, sibling otherwise.

    A feature worktree is not a sibling of ``DewieOps``, and the desk decision
    contract lives on a DewieOps feature branch rather than on ``main``, so the
    checkout that supplies ``dewie_brain`` has to be selectable.

    Without a checkout this returns ``None`` and only the test modules that
    call ``require_dewieops()`` are skipped. ``REQUIRE_DEWIEOPS=1`` (office box,
    CI with both repos) restores the hard failure, so a lost checkout can never
    pass as a green run there.
    """
    configured = (os.environ.get("DEWIEOPS_PATH") or "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"DEWIEOPS_PATH does not exist: {root}")
        return root
    root = Path(__file__).resolve().parents[3] / "DewieOps"
    if not root.is_dir():
        if REQUIRE_DEWIEOPS:
            raise RuntimeError(
                f"Sibling DewieOps checkout not found at {root}; set DEWIEOPS_PATH"
            )
        return None
    return root


DEWIEOPS = _dewieops_root()
if DEWIEOPS is not None and str(DEWIEOPS) not in sys.path:
    sys.path.insert(0, str(DEWIEOPS))

DEWIEOPS_SKIP_REASON = (
    "needs a DewieOps checkout (dewie_brain): set DEWIEOPS_PATH or clone it as a "
    "sibling ../DewieOps; REQUIRE_DEWIEOPS=1 turns this skip into an error"
)


def require_dewieops() -> None:
    """Skip the calling test module when no DewieOps checkout is available.

    Call it at the top of the module, before anything imports ``dewie_brain``.
    The decision rests on the checkout being present, not on ``dewie_brain``
    importing: with a checkout, a broken import still fails loudly rather than
    skipping (which ``pytest.importorskip`` would do).
    """
    __tracebackhide__ = True  # report the skip against the test module, not here
    if DEWIEOPS is None:
        pytest.skip(DEWIEOPS_SKIP_REASON, allow_module_level=True)


@pytest.fixture
def dewieops():
    """The same gate for one test in a module that otherwise runs without DewieOps."""
    require_dewieops()
    return DEWIEOPS


LIVE_SECRETS = (
    "CHATWOOT_API_TOKEN",
    "CHATWOOT_WEBHOOK_SECRET",
    "BRIDGE_OUTBOUND_TOKEN",
)


SPAM_SETTINGS = (
    "SPAM_AUTORESOLVE_ENABLED",
    "SPAM_AUTORESOLVE_DRY_RUN",
    "SPAM_CLASSIFIER_ENABLED",
    "SPAM_CLASSIFIER_MIN_CONFIDENCE",
)


@pytest.fixture(autouse=True)
def spam_defaults(monkeypatch, tmp_path):
    """Every test starts from the shipped spam defaults with a throwaway audit log."""
    for name in SPAM_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPAM_AUTORESOLVE_LOG", str(tmp_path / "spam-audit.jsonl"))


@pytest.fixture(autouse=True)
def no_live_chatwoot(monkeypatch):
    """Prove no test reaches a real HTTP endpoint or inherits live credentials.

    Every ``requests`` call ends in ``HTTPAdapter.send``; tests that exercise the
    client replace ``requests.post``/``get`` with local fakes before that point.
    Attempts are recorded as well as refused so a caller that swallows the error
    still fails the test.
    """
    for name in LIVE_SECRETS:
        monkeypatch.delenv(name, raising=False)
    # The outbound recipient check needs the legitimate email inbox; tests that
    # exercise the unconfigured case delete it.
    monkeypatch.setenv("BRIDGE_OUTBOUND_INBOX_ID", "1")
    attempts = []

    def refuse(adapter, request, *args, **kwargs):
        attempts.append(f"{request.method} {request.url}")
        raise AssertionError(f"test attempted real HTTP: {request.method} {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    yield
    assert attempts == [], f"tests must not contact a real HTTP service: {attempts}"

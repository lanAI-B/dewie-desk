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

def _dewieops_root() -> Path:
    """The DewieOps checkout under test: env override first, sibling otherwise.

    A feature worktree is not a sibling of ``DewieOps``, and the desk decision
    contract lives on a DewieOps feature branch rather than on ``main``, so the
    checkout that supplies ``dewie_brain`` has to be selectable.
    """
    configured = (os.environ.get("DEWIEOPS_PATH") or "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"DEWIEOPS_PATH does not exist: {root}")
        return root
    root = Path(__file__).resolve().parents[3] / "DewieOps"
    if not root.is_dir():
        raise RuntimeError(
            f"Sibling DewieOps checkout not found at {root}; set DEWIEOPS_PATH"
        )
    return root


DEWIEOPS = _dewieops_root()
if str(DEWIEOPS) not in sys.path:
    sys.path.insert(0, str(DEWIEOPS))


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

"""Test the bridge against the sibling DewieOps checkout required in production."""

import sys
from pathlib import Path

import pytest
import requests


DEWIEOPS = Path(__file__).resolve().parents[3] / "DewieOps"
if not DEWIEOPS.is_dir():
    raise RuntimeError(f"Sibling DewieOps checkout not found at {DEWIEOPS}")
sys.path.insert(0, str(DEWIEOPS))

LIVE_SECRETS = (
    "CHATWOOT_API_TOKEN",
    "CHATWOOT_WEBHOOK_SECRET",
    "BRIDGE_OUTBOUND_TOKEN",
)


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

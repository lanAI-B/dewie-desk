"""Test the bridge against the sibling DewieOps checkout required in production."""

import sys
from pathlib import Path


DEWIEOPS = Path(__file__).resolve().parents[3] / "DewieOps"
if not DEWIEOPS.is_dir():
    raise RuntimeError(f"Sibling DewieOps checkout not found at {DEWIEOPS}")
sys.path.insert(0, str(DEWIEOPS))

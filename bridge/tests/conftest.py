"""Test the bridge against the sibling DewieOps checkout required in production."""

import os
import sys
from pathlib import Path


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

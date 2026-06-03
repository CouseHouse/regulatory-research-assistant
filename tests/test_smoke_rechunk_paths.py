"""Guard against parents[N] depth regression in smoke_rechunk path constants.

Both BASELINE_PATH and the derived detail path must resolve inside the project
root's evals/ directory — not a sibling (the parents[4] bug would land them
under ~/projects/evals/ instead of ~/projects/regulatory-research-assistant/evals/).
"""
from __future__ import annotations

from pathlib import Path

import rra.evals.smoke_rechunk as smoke

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_EVALS_DIR = _PROJECT_ROOT / "evals"


def test_baseline_path_under_project_evals() -> None:
    assert smoke.BASELINE_PATH.is_relative_to(_EVALS_DIR), (
        f"BASELINE_PATH {smoke.BASELINE_PATH!r} is not under {_EVALS_DIR!r}. "
        "parents[N] depth is wrong — check smoke_rechunk.BASELINE_PATH."
    )


def test_detail_path_under_same_evals_dir() -> None:
    for table in smoke._VALID_TABLES:
        detail = smoke.BASELINE_PATH.parent / f"smoke-{table}-detail.json"
        assert detail.is_relative_to(_EVALS_DIR), (
            f"detail_path for table={table!r} ({detail!r}) "
            f"is not under {_EVALS_DIR!r}. "
            "The detail path has drifted from BASELINE_PATH.parent."
        )


def test_baseline_and_detail_share_parent() -> None:
    """Both arms of the smoke must write to the same directory."""
    for table in smoke._VALID_TABLES:
        detail = smoke.BASELINE_PATH.parent / f"smoke-{table}-detail.json"
        assert detail.parent == smoke.BASELINE_PATH.parent, (
            f"detail_path.parent ({detail.parent!r}) != "
            f"BASELINE_PATH.parent ({smoke.BASELINE_PATH.parent!r})"
        )

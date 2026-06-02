"""Golden dataset loader.

The golden set is JSONL at evals/golden.jsonl. Each line:

    {
        "id": "easy-001",
        "difficulty": "easy" | "medium" | "hard",
        "query": "...",
        "product_context": "...",
        "expected_facts": ["fact 1", "fact 2", ...],
        "expected_guidance_ids": ["GUID-001", "GUID-002"],
        "notes": "optional human notes about why this case matters"
    }

Write all 30 questions BEFORE building the agent. Picking questions after
the system exists biases toward "questions my system already answers."
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Difficulty = Literal["easy", "medium", "hard"]

GOLDEN_PATH = Path(__file__).resolve().parents[3] / "evals" / "golden.jsonl"


@dataclass(frozen=True)
class GoldenCase:
    id: str
    difficulty: Difficulty
    query: str
    product_context: str
    expected_facts: tuple[str, ...]
    expected_guidance_ids: tuple[str, ...]
    notes: str = ""
    # Non-empty only in CI-fixture cases. Pre-built (guidance_id, chunk_index)
    # pairs used by the runner instead of invoking run_agent (ADR 0012 D2).
    ci_citations: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "GoldenCase":
        return cls(
            id=d["id"],
            difficulty=d["difficulty"],
            query=d["query"],
            product_context=d.get("product_context", ""),
            expected_facts=tuple(d.get("expected_facts", [])),
            expected_guidance_ids=tuple(d.get("expected_guidance_ids", [])),
            notes=d.get("notes", ""),
            ci_citations=tuple(
                (str(pair[0]), int(pair[1]))
                for pair in d.get("_ci_citations", [])
            ),
        )


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldenCase]:
    if not path.exists():
        raise FileNotFoundError(
            f"Golden set not found at {path}. "
            f"Create it before running evals — see module docstring."
        )
    cases: list[GoldenCase] = []
    with path.open() as f:
        for line_num, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw or raw.startswith("//"):
                continue
            try:
                cases.append(GoldenCase.from_dict(json.loads(raw)))
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Bad golden case at line {line_num}: {e}") from e
    return cases

"""LangGraph orchestrator: planner → researcher → analyst ⇌ critic loop.

State machine (ADR 0008, ADR 0009):

  START → planner → researcher → analyst → critic
                                      ↑           │
                                      │    route_after_critic()
                               revise + count<cap  │
                                      └────────────┘
                                      approve / escalate / cap_hit → END

Checkpointing: PostgresSaver backed by the shared pool (ADR 0004).
Each request uses session_id as the LangGraph thread_id so the Langfuse
trace and the Postgres checkpoint share a single identifier.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal

import structlog
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from rra.agents.critic import run_critic
from rra.agents.planner import run_planner
from rra.agents.researcher import run_researcher
from rra.agents.analyst import run_analyst
from rra.agents.types import CriticNote
from rra.config import settings
from rra.db import get_pool
from rra.schemas import RetrievedPassage

log = structlog.get_logger(__name__)


# ─── State reducer for token_usage ───────────────────────────────────────────

def _merge_token_usage(
    left: dict[str, int], right: dict[str, int]
) -> dict[str, int]:
    """Merge two token-usage dicts. Right-side values overwrite left on key conflicts
    (each agent uses unique keys so conflicts indicate a bug, not normal operation)."""
    return {**left, **right}


# ─── GraphState ──────────────────────────────────────────────────────────────

class GraphState(TypedDict):
    """Inter-agent state for the Day 4 orchestrator.

    Field ownership is documented in ADR 0008. Every field has exactly one
    writer; accidental overwrites indicate a logic error, not a merge race.
    """

    # ── Set by api.py before graph entry ──────────────────────────────────
    query: str
    product_context: str
    session_id: str
    trace_id: str | None

    # ── Written by planner ────────────────────────────────────────────────
    sub_questions: list[str]
    outline: str

    # ── Written by researcher ─────────────────────────────────────────────
    passages: list[RetrievedPassage]

    # ── Written by analyst ────────────────────────────────────────────────
    draft: str

    # ── Written by critic ─────────────────────────────────────────────────
    verdict: Literal["approve", "revise", "escalate"] | None
    critic_notes: list[CriticNote]
    revision_count: int  # incremented by critic when verdict == "revise"
    cap_hit: bool        # set True by critic when revision_count hits the cap

    # ── Written by every agent (merged via reducer) ───────────────────────
    token_usage: Annotated[dict[str, int], _merge_token_usage]


# ─── Routing function ─────────────────────────────────────────────────────────

def route_after_critic(state: GraphState) -> str:
    """Return the next node name or END.

    ADR 0009 routing policy:
      cap_hit=True            → END  (return best-effort draft with warning)
      verdict approve/escalate → END
      verdict revise           → "analyst"  (revision_count already incremented)
    """
    if state["cap_hit"]:
        return END
    if state["verdict"] in ("approve", "escalate"):
        return END
    return "analyst"


# ─── Checkpointer singleton ───────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_checkpointer() -> PostgresSaver:
    """Process-lifetime PostgresSaver backed by the shared connection pool.

    setup() is idempotent — creates langgraph schema tables if they don't
    exist and runs pending migrations. Safe to call on every process start.
    """
    checkpointer = PostgresSaver(get_pool())
    checkpointer.setup()
    log.info("graph.checkpointer_ready")
    return checkpointer


# ─── Graph construction ───────────────────────────────────────────────────────

def _build_graph() -> Any:
    """Build and compile the LangGraph state machine."""
    builder: StateGraph[GraphState] = StateGraph(GraphState)

    # Node functions accept dict[str, Any]; LangGraph passes state as a plain dict
    # at runtime. The type-ignore[type-var] suppresses the NodeInputT mismatch.
    builder.add_node("planner", run_planner)  # type: ignore[type-var]
    builder.add_node("researcher", run_researcher)  # type: ignore[type-var]
    builder.add_node("analyst", run_analyst)  # type: ignore[type-var]
    builder.add_node("critic", run_critic)  # type: ignore[type-var]

    builder.set_entry_point("planner")
    builder.add_edge("planner", "researcher")
    builder.add_edge("researcher", "analyst")
    builder.add_edge("analyst", "critic")

    builder.add_conditional_edges(
        "critic",
        route_after_critic,
        {"analyst": "analyst", END: END},
    )

    return builder.compile(checkpointer=_get_checkpointer())


# Module-level compiled graph. Created once on first import.
_graph: Any = None


def _get_graph() -> Any:
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


# ─── Public entry point ───────────────────────────────────────────────────────

def run_graph(initial_state: dict[str, Any]) -> dict[str, Any]:
    """Invoke the orchestrator and return the final GraphState as a plain dict.

    Args:
        initial_state: Must contain at minimum: query, session_id.
                       product_context and trace_id default to "" / None.

    Returns:
        The final graph state dict. Key fields consumed by api.py:
          draft, passages, verdict, cap_hit, trace_id, token_usage.
    """
    # Fill in defaults for optional initial fields.
    state: dict[str, Any] = {
        "query": initial_state["query"],
        "product_context": initial_state.get("product_context", ""),
        "session_id": initial_state.get("session_id", ""),
        "trace_id": initial_state.get("trace_id"),
        "sub_questions": [],
        "outline": "",
        "passages": [],
        "draft": "",
        "verdict": None,
        "critic_notes": [],
        "revision_count": 0,
        "cap_hit": False,
        "token_usage": {},
    }

    session_id = state["session_id"]
    config = {"configurable": {"thread_id": session_id}}

    log.info("graph.run.start", session_id=session_id, query=state["query"][:120])

    final_state: dict[str, Any] = _get_graph().invoke(state, config=config)

    total_tokens = sum(final_state.get("token_usage", {}).values())
    log.info(
        "graph.run.complete",
        session_id=session_id,
        verdict=final_state.get("verdict"),
        cap_hit=final_state.get("cap_hit"),
        revision_count=final_state.get("revision_count", 0),
        total_tokens=total_tokens,
    )

    return final_state

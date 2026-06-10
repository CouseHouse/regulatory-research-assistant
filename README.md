# Regulatory Research Assistant

A multi-agent RAG system that helps compliance analysts draft first-pass positions on FDA guidance applicability, with verified citations and an evaluation harness that runs on every change.

Built as a portfolio project to demonstrate production-grade patterns for agentic AI systems: LangGraph orchestration, a custom MCP server with citation verification, evaluation as a first-class deliverable, and infrastructure as code for a real cloud deployment.

> **Status:** Live demo deployed on ECS Fargate; active development. See [`docs/dev-log.md`](docs/dev-log.md) for the running journal and [`docs/spec.md`](docs/spec.md) for the full design.

---

## Demo

*Walkthrough video coming soon — it will cover problem framing → architecture → live query → evaluation results → cloud deployment → what broke and how I fixed it.*

---

## What this is, briefly

A compliance analyst pastes a 1–3 sentence description of a proposed product change. The system returns a structured analysis:

- **Applicable FDA guidance documents**, retrieved and reranked from a corpus of ~200 public guidances
- **A draft position statement** synthesized from the retrieved passages
- **Verified inline citations** — every citation is checked against the source document before being included
- **A Langfuse trace URL** so a reviewer can see exactly how the answer was constructed

Behind the API are four specialized agents (planner, researcher, analyst, critic) coordinated by a LangGraph state machine, with tools exposed through a custom MCP server.

## Why each piece is the way it is

Short version below. Full reasoning with rejected alternatives in [`docs/spec.md`](docs/spec.md).

| Decision | Choice | Short rationale |
|---|---|---|
| Orchestration | LangGraph | Explicit state machine; portable to AgentCore and Foundry |
| Pattern | Planner-worker-critic | Simplest multi-agent pattern that pays for itself here |
| Models | Claude Sonnet + Haiku | Role-to-model matched to per-step difficulty and cost |
| Vector store | pgvector in Postgres | One DB does state + vectors; no SaaS dependency |
| Chunking | Structural splitter (paragraph→sentence), 512-token budget, 50 overlap | Structural boundaries keep quotes intact; recall@10=1.00, faithfulness 386/446 (ADR 0014) |
| Embeddings | Voyage 3 | Current MTEB leader at the price point |
| Reranker | Voyage rerank-2 | 5–15 point precision@5 lift; latency acceptable |
| Tool layer | Custom MCP server | Reusable by any MCP client; includes a real `check_citation` tool |
| Observability | Langfuse, self-hosted | Open source; meets regulated-vertical data residency needs |
| Identity | API key (v1); OAuth 2.0 designed | Production design in [`docs/identity-design.md`](docs/identity-design.md) |
| Deployment | ECS Fargate via Terraform | Real cluster behind a real load balancer, $5 demo cost |

## Architecture

```
┌────────────────────────────────────────────────────┐
│  FastAPI gateway (API key auth)                    │
│  POST /query  → returns structured analysis        │
└────────────────────────────────────────────────────┘
                    │
                    ▼
┌────────────────────────────────────────────────────┐
│  LangGraph orchestrator                            │
│                                                    │
│   Planner ──► Researcher ──► Analyst ──► Critic    │
│      ▲                                     │       │
│      └─────────── (revise on critique) ◄───┘       │
│                                                    │
│  Hard limit: 2 critic loops, then return as-is     │
└────────────────────────────────────────────────────┘
                    │
                    ▼
┌────────────────────────────────────────────────────┐
│  Custom MCP server (stdio + HTTP)                  │
│  Tools: search_corpus, fetch_guidance,             │
│         check_citation, list_recent_guidances      │
└────────────────────────────────────────────────────┘
                    │
                    ▼
       ┌─────────────────────────┐
       │  Postgres + pgvector    │
       │  Langfuse (traces)      │
       └─────────────────────────┘
```

## Evaluation results

The eval harness runs 30 golden questions across three difficulty bands on every change. Current scores against the live system:

| Metric | Score | Gate |
|---|---|---|
| Citation validity (deterministic) | 0.97 | ≥0.95 required |
| Key fact coverage (LLM-as-judge) | 0.81 | ≥0.80 warn |
| Position quality (LLM-as-judge w/ source context) | 4.8 / 5.0 | ≥4.0 warn |

Per-difficulty-band breakdown and the full golden set in [`evals/`](evals/).

The evaluation strategy — including why every LLM-as-judge scorer sees the source passages to prevent reward-hacking — is documented in [`docs/spec.md` §6](docs/spec.md#6-evaluation-strategy).

## Three things that broke (and what I learned)

Detailed writeups in [`docs/postmortems/`](docs/postmortems/). Short version:

1. **503 from the load balancer — and a fix that looked like it failed.** The deployed app returned 503 with no healthy target: the ECS task was running the ingest image, not the uvicorn one, because a bare `docker build` had shipped the wrong Dockerfile stage. Rebuilding and re-pushing `:latest` didn't help — a running ECS deployment pins its image digest, so the new image was invisible until `--force-new-deployment`. ([writeup](docs/postmortems/01-alb-503-image-pinning.md))

2. **A third of citation quotes failed verification — and the corpus wasn't the cause.** Quote-faithfulness checking failed ~30% of quotes (309/446 at τ=0.85). I assumed the corpus needed re-chunking; a $0 smoke across a dirty arm and a cleaned arm returned identical results (delta zero), pointing at the matcher instead. Normalizing smart quotes and stripping PDF line numbers took it to 386/446; recall@10 held at 1.00. ([writeup](docs/postmortems/02-quote-faithfulness-matcher.md))

3. **An LLM-as-judge that silently scored nothing.** The key-fact-coverage judge returned N/A on all 30 cases — it looked unwired, but Haiku was wrapping its JSON verdict in prose and strict parsing rejected it. An assistant-turn JSON prefill fixed it; the scorer went from zero signal to a 0.908 mean and now fails a third of cases on partial coverage, as intended. ([writeup](docs/postmortems/03-judge-blind-spot.md))

---

## Quickstart (local)

**Prerequisites:** Docker, Python 3.11+, `uv` (or pip), an Anthropic API key, a Voyage API key.

```bash
# Clone and bootstrap
git clone https://github.com/CouseHouse/regulatory-research-assistant
cd regulatory-research-assistant
cp .env.example .env  # add your API keys

# Bring up Postgres, Langfuse, and the MCP server
docker compose up -d

# Install Python deps
uv sync

# Download and ingest the FDA guidance corpus (~15 min, one-time)
uv run python -m rra.ingest

# Start the API
uv run uvicorn rra.api:app --reload

# In another terminal, run a query
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-key" \
  -d '{"query": "We want to update the firmware on our Class II infusion pump to add a new alarm. Do we need a new 510(k)?", "product_context": "FDA-cleared infusion pump, K-number K123456"}'
```

The response includes a Langfuse trace URL — open it at `http://localhost:3000` to see every agent step, tool call, token cost, and intermediate output.

## Running the eval harness

```bash
uv run python -m rra.evals.run
```

Outputs a markdown summary to `evals/results/latest.md` and pushes traces to Langfuse. CI runs this on every PR and gates merges on the citation validity threshold.

## Deploying to AWS

```bash
cd infra/terraform
terraform init
terraform apply  # ~6 min; ~$0.10/hr while running

# Once you're done with the demo:
terraform destroy
```

Provisions: VPC, ECS Fargate cluster, RDS Postgres (with pgvector), Secrets Manager, ALB. See [`infra/terraform/README.md`](infra/terraform/README.md) for the full topology and a cost breakdown.

## Repo layout

```
.
├── docs/
│   ├── spec.md                  # Full design doc with rejected alternatives
│   ├── decisions/               # Architecture Decision Records (ADRs 0001–0018)
│   ├── identity-design.md       # OAuth 2.0 production design (not implemented in v1)
│   ├── cost-model.md            # Token + infra cost breakdown
│   ├── future-work.md           # Out-of-scope items with treatments
│   ├── dev-log.md               # Running journal of decisions and surprises
│   └── postmortems/             # The three things that broke
├── src/rra/
│   ├── api.py                   # FastAPI gateway
│   ├── graph.py                 # LangGraph state machine
│   ├── config.py                # Pydantic Settings — all config flows through here
│   ├── agents/                  # Planner, researcher, analyst, critic
│   ├── mcp_server/              # Custom MCP server + tools (check_citation)
│   ├── retrieval.py             # pgvector similarity search + Voyage rerank
│   ├── ingest.py                # Corpus loader
│   └── evals/                   # Golden set + scorers + runner
├── evals/
│   ├── golden.jsonl             # 30 annotated questions
│   ├── fixtures/                # CI citation-gate fixtures
│   └── results/                 # Run outputs (gitignored except latest)
├── infra/terraform/             # ECS Fargate + RDS + ALB + VPC (Terraform)
├── tests/                       # Unit + integration tests
├── Dockerfile                   # Multi-stage: runtime (API) + bootstrap (corpus init)
├── docker-compose.yml           # Local dev: Postgres + Langfuse + MCP
└── .github/workflows/           # CI: tests + evals on every PR
```

## What's deliberately out of scope

These would be in v2; explanations of why each was cut from v1 are in [`docs/future-work.md`](docs/future-work.md):

- Real OAuth 2.0 implementation (designed, not built)
- Hybrid retrieval (BM25 + vector)
- Multi-tenant isolation
- Streaming responses
- A frontend (Loom demo and curl examples instead)

## What I'd build next

If this were going into actual production at a customer:
1. Implement the OAuth 2.0 design end-to-end, including agent-as-NHI patterns
2. Add hybrid retrieval to lift recall on terminology-mismatch queries
3. Add a "guidance currency" check using `list_recent_guidances` to flag analyses that may be stale
4. Move the MCP server to its own service with independent scaling and a Bedrock Guardrails layer

## License

MIT. The FDA guidance documents in the corpus are US government works in the public domain.

## Acknowledgments

Anthropic's [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) post shaped the orchestration design. The citation-verification pattern is adapted from published patterns in regulated-domain RAG literature.

---

*Built by Kyle Couse — [LinkedIn](https://www.linkedin.com/in/kyle-couse-9b17b659/) — [kcouse1@gmail.com](mailto:kcouse1@gmail.com)*




# Future work

Items deliberately deferred from v1. Each follows the same shape so Claude Code and human reviewers can scan uniformly:

- **Status in v1:** what exists now
- **Why deferred:** what was traded off
- **Reopen trigger:** the specific condition that would cause us to revisit
- **Implementation path:** how to do it when the trigger fires
- **Cost:** rough estimate

This document is the companion to [`spec.md`](spec.md). The spec records *what is*. This document records *what was considered and deferred, and what would change that*.

> **For Claude Code:** When a request would add functionality, check whether it appears here first. If it does, the trigger condition is the gate — don't reopen without it. If a new omission is being introduced, it gets a section here.

---

## 1. OAuth 2.0 implementation

**Status in v1:** Designed end-to-end in [`identity-design.md`](identity-design.md). Implementation is a single API key in AWS Secrets Manager, validated by FastAPI middleware.

**Why deferred:** OAuth implementation is well-understood and well-documented. The 15–20 hours it would take to implement end-to-end across the gateway, the agent-as-non-human-identity flow, and the MCP server's role as an OAuth resource server are better spent on the parts of the system that demonstrate AI engineering judgment specifically. The design is the harder artifact; the implementation is mechanical.

**Reopen trigger:** Any of: (1) a real user other than the author, (2) deployment to a shared environment, (3) a customer evaluation where the demo audience expects to see the full identity flow.

**Implementation path:** Stand up Cognito (or Entra) → implement authorization code flow in the gateway → register the agent as a client with its own credentials → add JWT validation middleware to the MCP server → update the eval harness to mint test tokens.

**Cost:** 15–20 hours focused work.

---

## 2. Hybrid retrieval (BM25 + vector)

**Status in v1:** Pure vector retrieval via pgvector, with cross-encoder reranking on top-25 candidates. Pegged to spec §4.3.

**Why deferred:** Current chunking and embedding choices target recall@10 ≥ 0.85, which the eval set will validate. If we hit that, hybrid retrieval is premature optimization. If we don't, hybrid is the standard fix and should be the first thing attempted — not chunking or embedding model swaps, which are much higher-disruption changes.

**Reopen trigger:** Recall@10 stalls below 0.75 on the medium-difficulty band of the golden set, OR specific failure modes appear where retrieval misses on rare proper nouns, guidance IDs, or specific regulatory terminology (BM25's strength).

**Implementation path:** Postgres supports `tsvector` natively, so the BM25 side adds no new infrastructure. Fuse with Reciprocal Rank Fusion (RRF) rather than weighted scores — RRF is parameter-free and consistently strong.

**Cost:** 1–2 days including re-evaluation against the golden set.

---

## 3. Multi-tenant isolation

**Status in v1:** Single-tenant. All data shares one Postgres schema, one MCP server instance, one Langfuse project.

**Why deferred:** This is a portfolio project, not a SaaS product. Multi-tenancy adds significant complexity — tenant-scoped Postgres roles, row-level security policies, separate Langfuse projects per tenant, audit log partitioning — for zero v1 value.

**Reopen trigger:** Pivoting to a hosted service, or onboarding a second user with data that must not be visible to the first.

**Implementation path:** Lighter-weight option is one Postgres database per tenant rather than RLS within a shared database — simpler isolation guarantees, simpler audit story. Tenant identity should come from JWT claims (depends on §1 being shipped first).

**Cost:** 1–2 weeks, AFTER §1 OAuth lands.

---

## 4. Streaming responses

**Status in v1:** The gateway returns the complete response when the LangGraph state machine reaches a terminal state.

**Why deferred:** Streaming intermediate agent output (researcher findings, analyst draft, critic feedback) is a meaningful UX improvement but adds complexity in three places: FastAPI endpoint (server-sent events), LangGraph invocation (`astream_events` instead of `invoke`), and the client (consuming a stream rather than awaiting a response). For the demo use case — analyst pastes a query, waits 30–60 seconds, reviews the structured output — the wait is acceptable and the structured terminal output is more reviewable than a streamed one.

**Reopen trigger:** A UI (§6) that needs perceived responsiveness. Streaming without a UI is a backend feature with no user-visible benefit.

**Implementation path:** A middle option worth trying first: stream *status updates* ("researcher found 8 candidate passages, reranking now...") without streaming the answer itself. Useful for long-running queries and much simpler than full answer streaming.

**Cost:** Status streaming: 1 day. Full answer streaming: 3–5 days.

---

## 5. Production monitoring (SLOs, alerting, on-call)

**Status in v1:** Langfuse traces for every query. No PagerDuty, no SLO dashboard, no synthetic monitoring.

**Why deferred:** SLO definition is a product question, not an engineering one. Setting SLOs for a system with no users produces fake numbers that survive into production and warp later decisions.

**Reopen trigger:** First real user or first production deployment with a stakeholder.

**Implementation path:** Langfuse remains for trace-level observability. CloudWatch (if AWS) or Azure Monitor for infrastructure metrics (ECS task health, RDS connections, ALB error rates). Synthetic check runs one easy golden-set question every 5 minutes; alerts on failure or latency regression. Initial SLOs to propose: 99% of queries complete in under 90s; citation validity score ≥0.95 on rolling 24-hour synthetic checks.

**Cost:** 3–5 days for a basic SLO + synthetic monitoring story.

---

## 6. User interface

**Status in v1:** No UI. Demo is a Loom video; live usage via curl or Python client.

**Why deferred:** A UI is the highest-effort, lowest-signal addition for a portfolio project. The audience for this repo is engineers, not analysts. A working API + good demo video proves the architecture; a half-finished React app proves the candidate spent the wrong week on frontend work.

**Reopen trigger:** Pivoting to user testing with actual compliance analysts. At that point the UI is the project, not a feature of it.

**Implementation path:** Thin Streamlit or Next.js page that POSTs to the existing API and renders the structured response (analysis, citations with hover-to-source, confidence indicators, Langfuse trace link). The API does not change.

**Cost:** 1 week for a credible Streamlit demo; 2–3 weeks for production-grade Next.js.

---

## 7. Guidance currency check

**Status in v1:** The `list_recent_guidances` MCP tool exists but is not invoked by any agent. Analyses make no statement about whether the guidances they cite are still current.

**Why deferred:** Building it without ground truth data is guesswork. Adding it now risks "fixing" a problem we haven't measured.

**Reopen trigger:** Any eval question where the correct answer hinges on guidance supersession, OR any user-reported issue where an analysis cited a superseded guidance.

**Implementation path:** Add a "currency check" step after the critic — given the cited guidance IDs, look up their status (active, superseded, withdrawn) and append a section to the response. FDA publishes guidance status in the document database; the lookup is straightforward once the schema is decided.

**Cost:** 2–3 days.

---

## 8. MCP server as a separately scaling service

**Status in v1:** The MCP server runs in-process alongside the agent (stdio transport) for local dev, and as a sidecar in the same Fargate task for the cloud demo. Pegged to spec §4.7.

**Why deferred:** In-process is dramatically simpler to debug and adds no network failure modes. The "scale independently" benefit only matters at load levels far beyond v1.

**Reopen trigger:** Sustained QPS that makes the agent and the MCP server resource-compete on the same task, OR the MCP server gaining tools that other services (not just this agent) want to call. The second is more likely to come first.

**Implementation path:** Split the MCP server into its own Fargate service behind an internal ALB → switch the agent's MCP client to HTTP transport → add a Bedrock Guardrails (or Azure Content Safety) layer at the MCP boundary. Natural moment to add per-tool rate limiting.

**Cost:** 1 week.

---

## 9. Fine-tuned or domain-adapted embeddings

**Status in v1:** Voyage 3 off-the-shelf. Pegged to spec §4.5.

**Why deferred:** General-purpose embeddings perform well on regulatory English; the cost of fine-tuning, plus the cost of re-embedding the corpus on every model iteration, isn't justified at this corpus size (~50k chunks). Fine-tuning also adds a model-versioning problem that v1 doesn't need.

**Reopen trigger:** Corpus grows past ~1M chunks (where the cost of re-embedding starts mattering less than the recall lift), OR eval shows recall failures concentrated on domain-specific terminology that hybrid retrieval (§2) didn't fix.

**Implementation path:** **Try domain-specific reranking first** — fine-tuning the reranker on a small set of regulatory query-passage pairs is much cheaper than fine-tuning the embedding model and typically delivers a similar or larger precision lift. Only if reranker tuning is insufficient, retrain embeddings.

**Cost:** Domain reranker fine-tune: 1 week. Embedding fine-tune + re-embed: 2–3 weeks.

---

## 10. Caching layer

**Status in v1:** No caching. Every query hits the model end-to-end. Anthropic prompt caching at the model layer is enabled where applicable.

**Why deferred:** Query distribution is unknown. Caching without knowing the hit rate just adds invalidation problems.

**Reopen trigger:** Observed query repetition in production traces, particularly on the researcher's retrieval queries (which are derived from user queries and may exhibit more repetition than user queries themselves).

**Implementation path:** Redis with a short TTL (1 hour) on retrieval results keyed by normalized query, and a longer TTL (24 hours) on full analyses keyed by `(query, product_context)` tuples. Invalidation on corpus updates is the hard part; the easy version is a global cache flush on every ingest run.

**Cost:** 2–3 days.


## 11. Retrieval deduplication at the retrieval layer.


**Cap chunks per guidance_id at 2, take top-5 across distinct guidances. 
This is a known RAG technique (sometimes called "MMR" for 
maximal marginal relevance, or "diversity reranking").

## 12. Integration tests run by default. 
Your pytest -q includes @pytest.mark.integration tests. Most projects configure 
pytest.ini/pyproject.toml to exclude integration markers from the default run 
(addopts = -m "not integration") so pytest is fast and key-independent, 
with integration run explicitly in CI or on demand. One-line config fix, Day 5 or later.
Why the test-env Voyage key is invalid when the runtime key works. 
Probably .env resolution differs under pytest, or there's a test-specific key. 
Worth understanding before Day 8 CI work, since CI will hit the same wall. Not urgent.

---

## How to use this document

When closing out v1 and planning v2: read this top to bottom. The trigger section for each item is the part that matters — most should *not* be reopened on a calendar schedule, only when their specific trigger fires.

When a trigger does fire: copy the relevant section into a new design doc, expand the "implementation path" into actual tasks, and link back here so the historical context isn't lost.

When proposing a *new* deferral (something Claude Code or a contributor wants to cut from v2 scope): add it here with the same shape. The list grows; the discipline of triggers prevents scope creep from camouflaging itself as engineering judgment.

# Future work

Each item below was deliberately cut from v1. The cuts are recorded here with their reasoning so they can be reopened with full context — by me, by a reviewer, or by a future contributor — without re-running the original decision.

This document is the companion to [`spec.md`](spec.md). The spec records *what is*; this document records *what was considered and deferred, and what would trigger reopening*.

---

## 1. OAuth 2.0 implementation

**Status in v1:** Designed end-to-end in [`identity-design.md`](identity-design.md). Implementation is a single API key in AWS Secrets Manager, validated by FastAPI middleware.

**Why deferred:** OAuth implementation is well-understood and well-documented. The 15–20 hours it would take to implement end-to-end across the gateway, the agent-as-non-human-identity flow, and the MCP server's role as an OAuth resource server are better spent on the parts of the system that demonstrate AI engineering judgment specifically. The design is the harder artifact; the implementation is mechanical.

**Trigger to reopen:** Any of: (1) a real user other than me, (2) deployment to a shared environment, (3) a customer evaluation where the demo audience expects to see the full identity flow. The cutover work itself is roughly: stand up Cognito (or Entra), implement the authorization code flow in the gateway, register the agent as a client with its own credentials, add JWT validation middleware to the MCP server, update the eval harness to mint test tokens.

**Cost estimate:** 15–20 hours focused work.

---

## 2. Hybrid retrieval (BM25 + vector)

**Status in v1:** Pure vector retrieval via pgvector, with cross-encoder reranking on top-25 candidates.

**Why deferred:** The current chunking and embedding choices target recall@10 ≥ 0.85, which the eval set will validate. If we hit that, hybrid retrieval is a premature optimization. If we don't, hybrid is the standard fix and should be the first thing attempted — not chunking or embedding model swaps, which are much higher-disruption changes.

**Trigger to reopen:** Recall@10 stalls below 0.75 on the medium-difficulty band of the golden set, OR specific failure modes appear where retrieval misses on rare proper nouns, guidance IDs, or specific regulatory terminology (BM25's strength). The terminology-mismatch failure documented in `postmortems/02-recall-cliff.md` was partly addressed by query rephrasing, but a recurrence on a new query type would be the second signal.

**Implementation path:** Postgres supports `tsvector` natively, so the BM25 side adds no new infrastructure. Fuse with Reciprocal Rank Fusion (RRF) rather than weighted scores — RRF is parameter-free and consistently strong. Expect 1–2 days of work including re-evaluation against the golden set.

---

## 3. Multi-tenant isolation

**Status in v1:** Single-tenant. All data shares one Postgres schema, one MCP server instance, one Langfuse project.

**Why deferred:** This is a portfolio project, not a SaaS product. Multi-tenancy adds significant complexity — tenant-scoped Postgres roles, row-level security policies, separate Langfuse projects per tenant, audit log partitioning — for zero v1 value.

**Trigger to reopen:** Pivoting to a hosted service, or onboarding a second user with data that must not be visible to the first. Even at that point, the lighter-weight option is one Postgres database per tenant rather than RLS within a shared database — simpler isolation guarantees, simpler audit story.

**What to think about before reopening:** Multi-tenancy intersects with the OAuth design (item 1) — tenant identity should come from the JWT claims, not from request parameters. Doing OAuth first, then multi-tenant, is much cleaner than the other order.

---

## 4. Streaming responses

**Status in v1:** The gateway returns the complete response when the LangGraph state machine reaches a terminal state.

**Why deferred:** Streaming intermediate agent output (researcher findings, analyst draft, critic feedback) is a meaningful UX improvement but adds complexity in three places: the FastAPI endpoint (server-sent events), the LangGraph invocation (`astream_events` instead of `invoke`), and the client (consuming a stream rather than awaiting a response). For the demo use case — analyst pastes a query, waits 30–60 seconds, reviews the structured output — the wait is acceptable and the structured terminal output is more reviewable than a streamed one.

**Trigger to reopen:** A UI (item 6) that needs perceived responsiveness. Streaming without a UI is a backend feature with no user-visible benefit.

**Note on partial streaming:** A middle option is to stream *status updates* ("researcher found 8 candidate passages, reranking now...") without streaming the answer itself. This is genuinely useful for long-running queries and is much simpler than full answer streaming.

---

## 5. Production monitoring (SLOs, alerting, on-call)

**Status in v1:** Langfuse traces for every query. No PagerDuty, no SLO dashboard, no synthetic monitoring.

**Why deferred:** SLO definition is a product question, not an engineering one. Setting SLOs for a system with no users produces fake numbers that survive into production and warp later decisions. The job description's "you don't disappear when it breaks at 3am of a customer's quarter-end run" is real, but you can't define meaningful SLOs without knowing what the customer's quarter-end run looks like.

**Trigger to reopen:** First real user or first production deployment with a stakeholder.

**Sketch of the v2 monitoring stack:** Langfuse remains for trace-level observability. CloudWatch (if AWS) or Azure Monitor for infrastructure metrics (ECS task health, RDS connections, ALB error rates). A synthetic check that runs one easy golden-set question every 5 minutes and alerts on failure or latency regression. Initial SLOs to propose: 99% of queries complete in under 90s; citation validity score ≥0.95 on rolling 24-hour synthetic checks.

---

## 6. User interface

**Status in v1:** No UI. The demo is a Loom video; live usage is via curl or a Python client.

**Why deferred:** A UI is the highest-effort, lowest-signal addition for a portfolio project. The audience for this repo is engineers, not analysts. A working API + good demo video proves the architecture; a half-finished React app proves the candidate spent the wrong week on frontend work.

**Trigger to reopen:** Only if pivoting to user testing with actual compliance analysts. At that point the UI is the project, not a feature of it.

**If reopened:** The right shape is probably a thin Streamlit or Next.js page that POSTs to the existing API and renders the structured response (analysis, citations with hover-to-source, confidence indicators, Langfuse trace link). The API does not change.

---

## 7. Guidance currency check

**Status in v1:** The `list_recent_guidances` MCP tool exists but is not invoked by any agent. Analyses make no statement about whether the guidances they cite are still current.

**Why deferred:** Building it without ground truth data is guesswork. Adding it now risks "fixing" a problem we haven't measured.

**Trigger to reopen:** Any eval question where the correct answer hinges on guidance supersession, or any user-reported issue where an analysis cited a superseded guidance.

**Implementation path:** Add a "currency check" step after the critic — given the cited guidance IDs, look up their status (active, superseded, withdrawn) and append a section to the response. FDA publishes guidance status in the document database; the lookup is straightforward once the schema is decided.

---

## 8. MCP server as a separately scaling service

**Status in v1:** The MCP server runs in-process alongside the agent (stdio transport) for local dev, and as a sidecar in the same Fargate task for the cloud demo.

**Why deferred:** In-process is dramatically simpler to debug and adds no network failure modes. The "scale independently" benefit only matters at load levels far beyond v1.

**Trigger to reopen:** Sustained QPS that makes the agent and the MCP server resource-compete on the same task, OR the MCP server gaining tools that other services (not just this agent) want to call. The second is more likely to come first.

**Implementation path:** Split the MCP server into its own Fargate service behind an internal ALB, switch the agent's MCP client to HTTP transport, add a Bedrock Guardrails (or Azure Content Safety) layer at the MCP boundary. This is also the natural moment to add per-tool rate limiting.

---

## 9. Fine-tuned or domain-adapted embeddings

**Status in v1:** Voyage 3 off-the-shelf.

**Why deferred:** General-purpose embeddings perform well on regulatory English; the cost of fine-tuning, plus the cost of re-embedding the corpus on every model iteration, isn't justified at this corpus size (~50k chunks). Fine-tuning also adds a model-versioning problem that v1 doesn't need.

**Trigger to reopen:** Corpus grows past ~1M chunks (where the cost of re-embedding starts mattering less than the recall lift), OR eval shows recall failures concentrated on domain-specific terminology that hybrid retrieval (item 2) didn't fix.

**Alternative to consider first:** Domain-specific *reranking* (fine-tuning the reranker on a small set of regulatory query-passage pairs) is much cheaper than fine-tuning the embedding model and typically delivers a similar or larger precision lift. Try that before retraining embeddings.

---

## 10. Caching layer

**Status in v1:** No caching. Every query hits the model end-to-end.

**Why deferred:** The query distribution is unknown. Caching without knowing the hit rate just adds invalidation problems. Anthropic prompt caching is already a meaningful cost win and is enabled at the model layer without a separate cache.

**Trigger to reopen:** Observed query repetition in production traces, particularly on the researcher's retrieval queries (which are derived from user queries and may exhibit more repetition than user queries themselves).

**Implementation path:** Redis with a short TTL (1 hour) on retrieval results keyed by normalized query, and a longer TTL (24 hours) on full analyses keyed by `(query, product_context)` tuples. Invalidation on corpus updates is the hard part; the easy version is a global cache flush on every ingest run.

---

## How to use this document

When closing out v1 and planning v2: read this top to bottom. The triggers section for each item is the part that matters — most of these items should *not* be reopened on a calendar schedule, only when their specific trigger fires. Reopening work without a trigger is how scope creep starts.

When a trigger does fire: copy the relevant section into a new design doc, expand the "implementation path" into actual tasks, and link back here so the historical context isn't lost.

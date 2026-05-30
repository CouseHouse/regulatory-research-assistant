# Regulatory Research Assistant — System Specification

**Author:** [Your name]
**Status:** Draft v0.1
**Last updated:** [Date]

> **For Claude Code:** This is the source of truth for design decisions. Each §4 component decision has the same shape: **Chosen:** X. **Because:** Y. **Rejected:** Z because W. **Reopen if:** condition. Cite the relevant §X.Y when responding to design questions; propose updates to this doc before changing the implementation.

---

## 1. Problem statement

Compliance analysts at medical device and pharmaceutical companies regularly answer questions of the form: *"Given a proposed change to our product, what existing FDA guidance applies, and does it suggest we need a new submission?"*

This requires reading across dozens of FDA guidance documents — each 10–80 pages — identifying applicable passages, and synthesizing a defensible position with citations. Today this is manual work taking hours to days per question. The cost of getting it wrong is real: an unnecessary submission wastes 6–12 months of regulatory cycle time, and a missed required submission risks enforcement action.

This system produces a first-draft analysis with verified citations to source guidance documents, suitable for an analyst to review, refine, and stand behind. It does **not** replace the analyst, and the output is explicitly framed as a starting point, not a regulatory determination.

### 1.1 Non-goals

- Producing final regulatory determinations or legal advice
- Indexing non-public material (proprietary submissions, internal SOPs)
- Real-time monitoring of new guidance publications
- Multi-language support — US FDA guidance only in v1

---

## 2. Users and primary scenarios

**Primary user:** A compliance analyst, ~3–10 years of experience, comfortable with regulatory language and skeptical of AI output by default.

**Primary scenario:** Analyst pastes a 1–3 sentence description of a proposed product change and an existing product context. System returns a structured analysis: applicable guidances, key passages with citations, a draft position statement, and confidence signals.

**Secondary scenario:** Analyst asks a comparative or definitional question ("What's the FDA's current position on AI/ML-enabled SaMD modification protocols?") and receives a synthesized answer with citations.

---

## 3. Architecture overview

```
┌────────────────────────────────────────────────────┐
│  FastAPI gateway                                   │
│  POST /query  → returns structured analysis        │
│  Auth: API key (v1), OAuth 2.0 in production       │
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
│  Custom MCP server (stdio + HTTP transports)       │
│  Tools:                                            │
│    - search_corpus(query, k, filters)              │
│    - fetch_guidance(guidance_id, section?)         │
│    - check_citation(claim, guidance_id, span)      │
│    - list_recent_guidances(since_date)             │
└────────────────────────────────────────────────────┘
                    │
                    ▼
       ┌─────────────────────────┐
       │  Postgres + pgvector    │
       │  Langfuse (traces)      │
       └─────────────────────────┘
```

### 3.1 Why multi-agent (and when it would be wrong)

**Chosen:** Planner-worker-critic with bounded revision (sometimes called reflection).

**Because:** The three roles have genuinely different objectives: the researcher optimizes for recall, the analyst optimizes for synthesis quality, the critic optimizes for grounding. Collapsing them into one agent dilutes attention across competing objectives — the model picks the wrong tool or skips citation verification under context pressure.

**Rejected:**
- *Single-agent loop with tool calls* — would handle simpler cases but degrades on multi-objective tasks. Acceptable baseline; not the right ceiling.
- *Swarm (peer agents negotiating)* — overkill; produces non-deterministic behavior that's hard to evaluate.
- *Hierarchical delegation with sub-supervisors* — matters at 6+ agents; ceremony at 3.

**Reopen if:** Eval shows critic adds <2% citation_validity over single-agent baseline (then critic is theater); or system grows past 6 agents (then hierarchy starts paying off).

---

## 4. Component decisions

Each subsection follows the same shape. Anchors are stable; Claude Code and reviewers can cite them.

### 4.1 Orchestration framework: LangGraph

**Chosen:** LangGraph for the state machine; `langchain-anthropic` only as a thin model adapter where needed. Direct Anthropic SDK calls for roles where prompt caching, citations, or extended thinking matter.

**Because:** LangGraph is an explicit state machine (vs. CrewAI's role abstraction that hides control flow), has first-class human-in-the-loop checkpoints and durable state, and deploys natively to both AWS Bedrock AgentCore and Azure AI Foundry without rewrites. Cross-cloud portability is a hedge against the customer-specific cloud choice the JD anticipates.

**Rejected:**
- *CrewAI* — faster to prototype but role-based abstraction makes state inspection harder when things go wrong.
- *AutoGen* — research-y feel, weaker production tooling at v1 time.
- *Raw LangChain without LangGraph* — lacks conditional routing primitives this design needs.
- *Provider-portable abstraction (LangChain everywhere)* — would lose prompt caching, native tool use ergonomics, Citations API, and extended thinking. Cost > benefit for single-vendor v1. See §4.2 for model-provider portability framing.

**Reopen if:** Customer requires a model provider Anthropic doesn't offer; or LangGraph's persistence model breaks under load.

### 4.2 Models: Claude Sonnet + Haiku, role-matched

**Chosen:** Claude Sonnet for planner, analyst, critic. Claude Haiku for researcher, LLM-as-judge in evals.

**Because:** Role-to-model mapping reflects each role's actual difficulty.
- *Planner:* decomposes intent → strong reasoning → Sonnet.
- *Analyst:* synthesizes across passages → reasoning + writing → Sonnet.
- *Critic:* verifies claims against source → reasoning → Sonnet.
- *Researcher:* rephrases queries for retrieval → simple → Haiku.
- *Judge in evals:* cheap, fast, runs on every change → Haiku.

**Cost model:** Typical query is ~4 Sonnet calls + ~3 Haiku calls. Rough cost: under $0.05 per query during development, under $0.02 at steady state once prompt caching is enabled. Detail in `docs/cost-model.md`.

**Rejected:**
- *Sonnet everywhere* — doubles cost with no measured quality gain on this task.
- *Haiku everywhere* — produced visibly worse synthesis in early testing; analyst hallucinated cross-references the source didn't support.
- *Multi-provider portability* — explicitly rejected; see §4.1.

**Reopen if:** Sonnet pricing changes >2x relative to Haiku (rebalance); or a smaller Claude model with Sonnet-equivalent reasoning ships (downgrade planner/critic).

### 4.3 Vector store: pgvector in Postgres

**Chosen:** pgvector inside the same Postgres instance that holds application state.

**Because:** Postgres pulls double duty as application state store (LangGraph checkpoints, audit log) AND vector store. One operational footprint, one backup story, one set of credentials. pgvector with HNSW indexing handles corpora into the millions of chunks at sub-100ms recall — well beyond v1 needs (~50k chunks expected).

**Rejected:**
- *Pinecone* — fast to set up, but adds SaaS dependency, separate auth boundary, ongoing cost for a corpus this small.
- *OpenSearch* — right answer at much higher scale or when hybrid (BM25 + vector) retrieval is required from day one. Revisit at >1M chunks or pure-vector recall stalls.
- *Azure AI Search* — natural pick if deploying to Foundry; portability matters more than the specific store.

**Reopen if:** Corpus grows past 1M chunks; recall@10 stalls below 0.75 after chunking tuning; OR customer cloud is Azure and they prefer AI Search for ops reasons.

### 4.4 Chunking: recursive character splitter, 512 tokens, 50-token overlap

**Chosen:** Recursive character splitter, 512-token chunks, 50-token overlap.

**Because:** FDA guidance documents have strong structural cues — numbered sections, bold headings — that the recursive splitter exploits naturally by preferring paragraph and section boundaries before falling back to character splits. 512 tokens preserves a typical regulatory paragraph intact; small enough that retrieved chunks stay focused. 50-token overlap (10%) hedges against splitting a sentence mid-claim.

**Rejected:**
- *Semantic chunking (clustering sentences by embedding similarity)* — theoretically appealing but recent benchmarks (Vecta Feb 2026; NAACL 2025 Findings) show fixed-size recursive matches or beats it on retrieval recall while being 10x cheaper to compute and trivially reproducible.

**Validation:** Recall@10 ≥ 0.85 on the golden set after week 1.

**Reopen if:** Recall@10 stalls below 0.75. First lever to try then is chunking strategy, not embedding model.

### 4.5 Embedding model: Voyage 3

**Chosen:** Voyage 3 (1024 dimensions, cosine similarity).

**Because:** Voyage 3 currently leads MTEB retrieval benchmarks for general English at a price point under OpenAI's `text-embedding-3-small`. Anthropic recommends it for use with Claude. Best price-performance for regulatory/legal-adjacent text.

**Rejected:**
- *OpenAI text-embedding-3-large* — performs comparably but adds an OpenAI dependency to an otherwise Anthropic-only stack.
- *Domain-specific embeddings (BGE-large fine-tuned)* — would beat both on domain-specific evals but the fine-tuning + re-embedding cost isn't justified at this corpus size.

**Reopen if:** Corpus grows past 1M chunks (re-embedding cost amortizes); OR eval shows recall failures concentrated on domain-specific terminology that hybrid retrieval doesn't fix.

### 4.6 Reranker: Voyage rerank-2

**Chosen:** Cross-encoder rerank on top-25 retrieval candidates, narrowing to top-5 for analyst context.

**Because:** Cross-encoder reranking moves precision@5 by 5–15 points in published benchmarks. Latency cost (~200ms) is acceptable for non-real-time agents.

**Rejected:**
- *Skip reranker* — published precision lift is large enough that latency is almost always worth it for non-real-time use cases.

**Reopen if:** Latency budget tightens (real-time UI); OR a non-Voyage reranker shows materially better precision on the eval set.

### 4.7 MCP server: Python SDK, stdio + HTTP transports

**Chosen:** Custom MCP server using the Anthropic Python SDK, exposing four tools: `search_corpus`, `fetch_guidance`, `check_citation`, `list_recent_guidances`. Runs in-process with the agent (stdio) in v1; split to its own service in production.

**Because:** MCP is the JD-named pattern for tool integration. Exposing corpus tools via MCP (rather than direct Python function calls) makes them reusable by any MCP-compatible client (Claude Desktop, other agents, the eval harness). Dev-time benefit: tools debuggable by hand through Claude Desktop without running the full agent stack.

**Distinctive design choice — `check_citation`:** The researcher and analyst can hallucinate citations; the critic uses `check_citation` to verify that each claimed citation actually appears in the cited guidance, with the cited text returned for the critic to score against the claim. This is a real reliability pattern from regulated-domain RAG, not boilerplate.

**Rejected:**
- *Direct Python function calls* — works but loses reusability and the dev-time debugging benefit.
- *Wrapping a generic search API in MCP* — would satisfy the requirement but adds no project distinctiveness. `check_citation` is the differentiator.

**Reopen if:** Sustained load makes in-process MCP a bottleneck (see §7.3); OR MCP server gains tools other services want to call.

### 4.8 Observability: Langfuse, self-hosted

**Chosen:** Langfuse v3, self-hosted via docker-compose locally, on the same Fargate cluster in cloud.

**Because:** Langfuse is open-source and self-hostable — a hard requirement for regulated-vertical customers who often can't send traces to third-party SaaS. Trace model handles nested agent calls and tool calls cleanly. LLM-as-judge eval feature ties evals to traces without extra wiring.

**Rejected:**
- *LangSmith* — smoothest option for LangChain users but SaaS-only; locks evals to LangChain primitives.
- *Braintrust* — stronger CI/CD gating but SaaS and pricier.

**Reopen if:** Self-hosting overhead exceeds value (unlikely; the operational cost is small); OR customer mandates a specific observability vendor.

### 4.9 Identity: API key (v1), OAuth 2.0 designed for production

**Chosen for v1:** Single API key in AWS Secrets Manager, validated by FastAPI middleware.

**Designed for production:** OAuth 2.0 authorization code flow against Cognito (or Entra), with the agent acting as a non-human identity with its own client credentials, JWT-validated by the MCP server (as an OAuth resource server) before any tool executes. Full design in `docs/identity-design.md`.

**Because (the deferral):** OAuth implementation is well-understood (15–20 hours of mechanical work). The design is the harder artifact and demonstrates the agent-as-NHI pattern the JD calls out. Implementation is deferred to keep v1 focused on AI engineering judgment.

**Rejected:**
- *No auth* — disqualifying for any non-toy deployment.
- *Build OAuth in v1* — opportunity cost vs. eval depth and agent quality.

**Reopen if:** Any real user other than the author; deployment to a shared environment; customer evaluation where the demo audience expects to see the full identity flow.

### 4.10 Deployment: ECS Fargate, Terraform-managed

**Chosen:** ECS Fargate, Terraform-managed (VPC, ALB, RDS Postgres, Secrets Manager).

**Because:** Fargate gives the "shipped to a real cluster behind a real load balancer" signal the JD asks for without the complexity of running a Kubernetes control plane. Multi-agent runs are 20–90 seconds, which exceeds Lambda's friendly range. Fargate's per-second billing makes "deploy, demo, destroy" a $5 exercise.

**Rejected:**
- *EKS* — more impressive on paper, but a half-finished EKS deploy is much worse signal than a clean Fargate one. K8s is mentioned in identity-design.md as the production alternative.
- *App Runner* — genuinely simpler, would be fine; Fargate's small extra complexity is worth it because Terraform-managed networking is a more portable skill.
- *Lambda* — exceeds the 15-minute timeout for multi-agent runs.

**Reopen if:** Demonstrating K8s fluency becomes a higher priority than ship-the-demo (probably for v2 or a different JD).

---

## 5. Data flow for a single query

1. Client `POST`s `{query, product_context}` to FastAPI with API key.
2. FastAPI creates a LangGraph session, persists initial state to Postgres.
3. **Planner (Sonnet)** decomposes the query into 2–4 retrieval sub-questions and produces an outline of the expected analysis structure.
4. **Researcher (Haiku)** loops over sub-questions, calling `search_corpus` via MCP. Returns ranked passages with guidance IDs and span offsets.
5. **Analyst (Sonnet)** receives the planner's outline and the researcher's passages. Drafts the analysis with inline citations of the form `[guidance_id:span]`.
6. **Critic (Sonnet)** receives the draft and the source passages. For each citation, calls `check_citation` to verify the cited span actually appears and supports the claim. Emits one of: `approve`, `revise` (with specific issues), or `escalate` (if claims can't be grounded after revision).
7. If `revise` and revision count < 2, return to analyst with critic notes. Otherwise return current draft with grounding scores.
8. Final structured response includes: analysis text, citations with verification status, confidence score, Langfuse trace URL.

---

## 6. Evaluation strategy

This is the section that makes or breaks the project, per the JD ("evals as a deliverable not a nice to have"). The eval harness is treated as a first-class component, not a script.

### 6.1 Golden dataset

30 questions across three difficulty bands:
- **10 easy:** Single-guidance lookup with known answers.
- **15 medium:** Synthesis across 2–4 guidances with a defensible position.
- **5 hard:** Edge cases where the correct answer is "the existing guidance does not clearly address this" — tests that the system refuses to hallucinate rather than overconfidently answering.

Each question annotated with: expected key facts, expected guidance IDs that should be cited, difficulty band.

### 6.2 Scorers

Three independent scorers per response:

| Scorer | Method | Why |
|---|---|---|
| Citation validity | Deterministic string match | Catches hallucinated citations cleanly; no drift between runs |
| Key fact coverage | LLM-as-judge (Haiku) | Cheap, fast, runs on every change |
| Position quality | LLM-as-judge (Sonnet, **with retrieved passages in context**) | Source context prevents reward-hacking by confident-sounding nonsense |

### 6.3 Regression gates

| Scorer | Threshold | Action on miss |
|---|---|---|
| Citation validity | ≥ 0.95 | **Fail the build** |
| Key fact coverage | ≥ 0.80 | Warn |
| Position quality | ≥ 4.0 / 5.0 | Warn |

Failures block; warnings require an explicit override note in the PR.

---

## 7. Risks and open questions

### 7.1 Critic loop divergence

**Risk:** Critic and analyst disagree on every iteration; loop never terminates.

**Mitigation:** Hard cap at 2 revisions (`max_critic_revisions` in config). Cap-hit rate measured in evals.

### 7.2 Retrieval recall ceiling

**Risk:** Recall@10 stalls below 0.75 after chunking experiments. Pure vector retrieval insufficient.

**Mitigation:** Hybrid retrieval (BM25 + vector via Postgres tsvector + pgvector) is the next lever. See `docs/future-work.md` §2.

### 7.3 MCP server as single point of failure

**Risk:** v1 runs MCP in-process; outage takes the agent with it.

**Mitigation:** Production design splits MCP to its own service. See `docs/future-work.md` §8.

### 7.4 Guidance drift

**Risk:** FDA guidances get superseded. v1 has no detection.

**Mitigation:** `list_recent_guidances` tool exists as a building block for a future "is this analysis still current" check. See `docs/future-work.md` §7.

---

## 8. Out of scope for v1

Tracked in detail in `docs/future-work.md`. Briefly:

- Real OAuth 2.0 implementation (designed in `docs/identity-design.md`)
- Hybrid retrieval (BM25 + vector)
- Multi-tenant isolation
- Streaming responses
- A user interface (Loom demo only)
- Production monitoring beyond Langfuse traces
- Guidance currency check
- MCP server as separately-scaling service
- Fine-tuned domain embeddings
- Response caching

Each has a §-numbered treatment in `docs/future-work.md` with reopening triggers.

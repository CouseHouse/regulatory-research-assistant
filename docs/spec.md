# Regulatory Research Assistant — System Specification

**Author:** [Your name]
**Status:** Draft v0.1
**Last updated:** [Date]

---

## 1. Problem statement

Compliance analysts at medical device and pharmaceutical companies regularly need to answer questions of the form: *"Given a proposed change to our product, what existing FDA guidance applies, and does it suggest we need a new submission?"*

This requires reading across dozens of FDA guidance documents — each 10–80 pages — identifying applicable passages, and synthesizing a defensible position with citations. Today this is manual work taking hours to days per question. The cost of getting it wrong is real: an unnecessary submission wastes 6–12 months of regulatory cycle time, and a missed required submission risks enforcement action.

This system produces a first-draft analysis with verified citations to source guidance documents, suitable for an analyst to review, refine, and stand behind. It does **not** replace the analyst, and the output is explicitly framed as a starting point, not a regulatory determination.

### Non-goals

- Producing final regulatory determinations or legal advice
- Indexing non-public material (proprietary submissions, internal SOPs)
- Real-time monitoring of new guidance publications (future work)
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

### Why multi-agent (and when it would be wrong)

A single-agent loop with tool calls would handle the simpler cases. Multi-agent is justified here because the three roles have genuinely different objectives: the researcher optimizes for recall, the analyst optimizes for synthesis quality, and the critic optimizes for grounding. Collapsing them into one agent has been shown empirically to dilute attention across competing objectives — the model picks the wrong tool or skips citation verification under context pressure.

The pattern is **planner-worker-critic with bounded revision**, sometimes called reflection. It's the simplest multi-agent pattern that actually pays for itself for this use case.

**What I rejected:** A swarm (peer agents negotiating) is overkill and produces non-deterministic behavior that's hard to evaluate. Hierarchical delegation with sub-supervisors would matter at 6+ agents; with 3 it's just ceremony.

---

## 4. Component decisions, with rationale

### 4.1 Orchestration framework: LangGraph

**Chosen because** it's an explicit state machine (vs. CrewAI's role abstraction, which hides control flow), has first-class support for human-in-the-loop checkpoints and durable state — both of which matter for any future production version where an analyst reviews intermediate output — and deploys natively to both AWS Bedrock AgentCore and Azure AI Foundry without rewrites. Cross-cloud portability is a hedge against the customer-specific cloud choice the job description anticipates.

**What I rejected:** CrewAI is faster to prototype but the role-based abstraction makes state inspection harder when things go wrong. AutoGen has a research-y feel and weaker production tooling. Raw LangChain (no LangGraph) lacks the conditional routing primitives this design needs.

### 4.2 Models: Claude Sonnet (planner, analyst, critic), Claude Haiku (researcher, judge)

**Chosen because** the role-to-model mapping reflects each role's actual difficulty. The planner decomposes intent and needs strong reasoning. The analyst synthesizes across retrieved passages and needs both reasoning and writing quality. The critic must verify claims against source text — also a reasoning task. The researcher's job is *just* "rephrase the user query into targeted retrieval queries" — Haiku is plenty. Using Haiku for the LLM-as-judge in evals keeps the eval loop cheap enough to run on every change.

**Cost model:** A typical query is ~4 Sonnet calls and ~3 Haiku calls. Rough cost at current pricing: under $0.05 per query during development, under $0.02 at steady state once prompt caching is enabled. See `docs/cost-model.md` (TODO).

**What I rejected:** Using Sonnet everywhere would roughly double cost with no measured quality gain on this task. Using Haiku everywhere produced visibly worse synthesis in early testing — the analyst step hallucinated cross-references the source didn't support.

### 4.3 Vector store: pgvector in Postgres

**Chosen because** Postgres pulls double duty as the application state store (LangGraph checkpoints, audit log) and the vector store. One operational footprint, one backup story, one set of credentials. pgvector with HNSW indexing handles corpora into the millions of chunks at sub-100ms recall, which is well beyond v1 needs (~50k chunks expected).

**What I rejected:** Pinecone is fast to set up but adds a SaaS dependency, a separate auth boundary, and ongoing cost for a corpus this small. OpenSearch is the right answer at much higher scale or when hybrid (BM25 + vector) retrieval is required from day one — I'd revisit at >1M chunks or if pure-vector recall stalls below target. Azure AI Search would be the natural pick if this were deploying to Foundry; portability matters more than the specific store.

### 4.4 Chunking: recursive character splitter, 512 tokens, 50-token overlap

**Chosen because** FDA guidance documents have strong structural cues — numbered sections, bold headings — that the recursive splitter exploits naturally by preferring paragraph and section boundaries before falling back to character splits. 512 tokens is large enough to preserve a typical regulatory paragraph intact, small enough that retrieved chunks stay focused. 50-token overlap (10%) is the standard hedge against splitting a sentence mid-claim.

**What I rejected:** Semantic chunking (clustering sentences by embedding similarity) is theoretically appealing but recent benchmarks (Vecta Feb 2026; NAACL 2025 Findings) show fixed-size recursive splitting matches or beats it on retrieval recall while being 10x cheaper to compute and trivially reproducible. I revisit this only if eval shows a clear failure mode that semantic chunking would solve.

**How I'll measure this was right:** Recall@10 ≥ 0.85 on the golden set after week 1. If it stalls below 0.75, the first thing to change is chunking strategy, not embedding model.

### 4.5 Embedding model: Voyage 3

**Chosen because** Voyage 3 currently leads the MTEB retrieval benchmarks for general English text at a price point under OpenAI's `text-embedding-3-small`, and Anthropic recommends it for use with Claude. The price-performance is currently best-in-class for the regulatory/legal-adjacent domain.

**What I rejected:** OpenAI `text-embedding-3-large` performs comparably but adds an OpenAI dependency to an otherwise Anthropic-only stack. Domain-specific embeddings (BGE-large fine-tuned, etc.) would beat both on a domain-specific eval but the cost of fine-tuning and re-embedding the corpus isn't justified at this corpus size. I'd revisit at 1M+ chunks.

### 4.6 Reranker: Voyage rerank-2

**Chosen because** retrieval pulls top-25, reranker reduces to top-5 for the analyst's context window. Cross-encoder reranking consistently moves precision@5 by 5–15 points in published benchmarks and the latency cost is acceptable (~200ms added per query).

**What I rejected:** Skipping the reranker. The published precision lift is large enough that the latency is almost always worth it for non-real-time agents.

### 4.7 MCP server: Python SDK, both stdio and HTTP transports

**Chosen because** MCP is the JD-named pattern for tool integration, and exposing the corpus tools via MCP — rather than as direct Python function calls — makes them reusable by any MCP-compatible client (Claude Desktop, other agents, the eval harness). The dev-time benefit is real: I can debug tools by hand through Claude Desktop without running the full agent stack.

The `check_citation` tool is the part of this design I'm most proud of. The researcher and analyst can hallucinate citations; the critic uses `check_citation` to verify that each claimed citation actually appears in the cited guidance, with the cited text returned for the critic to score against the claim. This is a real reliability pattern from regulated-domain RAG, not a toy.

### 4.8 Observability: Langfuse, self-hosted

**Chosen because** Langfuse is open-source and self-hostable — a hard requirement for the regulated-vertical customers the JD targets, who often can't send traces to third-party SaaS. The trace model handles nested agent calls and tool calls cleanly, and the LLM-as-judge eval feature ties evals to traces without extra wiring.

**What I rejected:** LangSmith is the smoothest option for LangChain users but is SaaS-only and locks evals to LangChain primitives. Braintrust has stronger CI/CD gating but is also SaaS and pricier. For a portfolio project where the audience may be evaluating regulated-industry suitability, self-hosting wins.

### 4.9 Identity: API key in v1, OAuth 2.0 designed for production

The v1 implementation uses a single API key in AWS Secrets Manager, verified by FastAPI middleware. This is **not** production-grade.

The production design — see `docs/identity-design.md` — uses OAuth 2.0 authorization code flow against Cognito (or Entra), with the agent acting as a non-human identity with its own client credentials, JWT-validated by the MCP server (as an OAuth resource server) before any tool executes. This implements the "agent-as-NHI" pattern called out in the JD.

The decision to design but not implement this for v1 is deliberate. OAuth implementation is well-understood and adds 15–20 hours that are better spent on the parts of this system that demonstrate AI engineering judgment.

### 4.10 Deployment: ECS Fargate, Terraform-managed

**Chosen because** Fargate gives the "shipped to a real cluster behind a real load balancer" signal the JD asks for, without the complexity of running a Kubernetes control plane. Multi-agent runs are 20–90 seconds, which exceeds Lambda's friendly range. Fargate's per-second billing also makes "deploy, demo, destroy" a $5 exercise.

**What I rejected:** EKS would be more impressive on paper, but a half-finished EKS deploy is much worse signal than a clean Fargate one. App Runner is genuinely simpler and would be fine; Fargate's small extra complexity is worth it because Terraform-managed networking is a portable skill.

---

## 5. Data flow for a single query

1. Client POSTs `{query, product_context}` to FastAPI with API key.
2. FastAPI creates a LangGraph session, persists initial state to Postgres.
3. **Planner (Sonnet)** decomposes the query into 2–4 retrieval sub-questions and produces an outline of the expected analysis structure.
4. **Researcher (Haiku)** loops over sub-questions, calling `search_corpus` via MCP. Returns ranked passages with guidance IDs and span offsets.
5. **Analyst (Sonnet)** receives the planner's outline and the researcher's passages. Drafts the analysis with inline citations of the form `[guidance_id:span]`.
6. **Critic (Sonnet)** receives the draft and the source passages. For each citation, calls `check_citation` to verify the cited span actually appears and supports the claim. Emits one of: `approve`, `revise` (with specific issues), or `escalate` (if claims can't be grounded after revision).
7. If `revise` and revision count < 2, return to analyst with critic notes. Otherwise return current draft with grounding scores.
8. Final structured response includes: analysis text, citations with verification status, confidence score, and a Langfuse trace URL.

---

## 6. Evaluation strategy

This is the section that makes or breaks the project, per the JD ("evals as a deliverable not a nice to have"). The eval harness is treated as a first-class component, not a script.

### 6.1 Golden dataset

30 questions across three difficulty bands:
- **10 easy:** Single-guidance lookup with known answers (e.g., "What does the FDA guidance on cybersecurity for medical devices recommend for SBOM disclosure?")
- **15 medium:** Synthesis across 2–4 guidances with a defensible position
- **5 hard:** Edge cases where the correct answer is "the existing guidance does not clearly address this" — testing that the system refuses to hallucinate rather than overconfidently answering

Each question annotated with: expected key facts, expected guidance IDs that should be cited, and a difficulty band.

### 6.2 Scorers

Three independent scorers per response:
- **Citation validity (deterministic):** Every cited guidance ID resolves to a real document; every cited span actually appears in that document. Pure string match. Catches hallucinated citations cleanly.
- **Key fact coverage (LLM-as-judge, Haiku):** Given the response and the expected key facts, what fraction of expected facts are present and correctly attributed?
- **Position quality (LLM-as-judge, Sonnet, with retrieved passages in context):** Holistic quality score with the source material visible to the judge to prevent reward-hacking by confident-sounding nonsense.

### 6.3 Regression gates

- Citation validity < 0.95 → fail the build
- Key fact coverage < 0.80 → warn
- Position quality < 4.0/5.0 → warn

Failures are blocking; warnings require an explicit override note in the PR.

---

## 7. Risks and open questions

- **Critic loop divergence:** What if critic and analyst disagree on every iteration? Hard cap at 2 revisions, return with explicit "low confidence" flag. Will measure rate of cap-hits in eval.
- **Retrieval recall ceiling:** If recall@10 stalls below 0.75 after chunking experiments, the corpus likely needs hybrid retrieval (BM25 + vector). Hold this as a known fallback.
- **MCP server as a single point of failure:** v1 runs MCP server in the same process as the agent. Production design splits them, with the MCP server scaling independently behind its own load balancer.
- **Guidance drift:** FDA guidances get superseded. v1 has no detection for this. `list_recent_guidances` exists as a building block for a future "is this analysis still current" check.

---

## 8. Out of scope for v1 (and explicitly tracked)

- Real OAuth 2.0 implementation (designed in `docs/identity-design.md`)
- Hybrid retrieval (BM25 + vector)
- Multi-tenant isolation (single-tenant assumed)
- Streaming responses (returns complete response only)
- A user interface (Loom demo only; curl/Python client for live use)
- Production monitoring beyond Langfuse traces (no PagerDuty, no SLOs)

Each of these has a one-paragraph treatment in `docs/future-work.md`.

# Day 10 — Design docs

## Goal

Finish the two design docs that demonstrate senior thinking, plus the architecture diagram for the README.

## Deliverables

### `docs/identity-design.md` (the heavy one)

Full OAuth 2.0 design — what gets implemented when the "real user" trigger fires (future-work §1). Sections:

1. **Threat model:** what we're protecting against (token theft, replay, agent impersonation, scope creep)
2. **Identity providers:** Cognito (AWS path) and Entra (Azure path), with the trade-offs
3. **Sequence diagrams** (Mermaid) for:
   - User → Gateway: authorization code flow
   - Agent → MCP server: agent-as-NHI with client credentials
   - Token refresh
4. **JWT validation:** what claims are required, what the resource server checks
5. **Tool authorization:** how scopes map to MCP tool permissions
6. **Audit log:** what gets logged and where
7. **Failure modes:** revoked tokens, expired sessions, IdP outage

Length: 2-3 pages. The sequence diagrams are the high-signal part.

### `docs/cost-model.md` (the math one)

Token math and infrastructure math for the system. Sections:

1. **Per-query cost breakdown:**
   - Planner: ~N1 input tokens × $X/MTok + ~N2 output tokens × $Y/MTok = $A
   - Researcher: similar
   - Analyst: similar
   - Critic (× 1-3 iterations): similar
   - Total: $T per query at p50
2. **Prompt caching impact:** which agents have stable system prompts; expected cache hit rate; cost reduction
3. **Voyage embedding/rerank cost:** per query (small) and per ingest run (one-time)
4. **Infrastructure cost (steady state):**
   - Fargate: $X/hour
   - RDS: $X/hour
   - ALB + NAT: $X/hour
   - Langfuse self-host: $X/hour
   - Total: ~$Y/month at 24/7
5. **Cost-control levers ranked by impact:**
   - Prompt caching (biggest)
   - Model downsizing where quality holds (Haiku for researcher)
   - Critic loop cap (already at 2)
   - **Critic `source_text` truncation** — Day-5 trace data showed ~27% critic input bloat from oversized `source_text` passages; truncating to the retrieved chunk text is the cheapest lever after caching
   - **Analyst prompt-cache verification** — confirm cache hit rate matches the expected rate given the stable system prompt; a miss means the cache is being busted unexpectedly (check for dynamic content in the system prompt)
   - Retrieval result caching (future-work §10)

Length: 2 pages with tables.

### Architecture diagram for README

Mermaid diagram replacing the ASCII art. Components:
- FastAPI gateway
- LangGraph orchestrator (showing the 4 agents)
- MCP server (showing the 4 tools)
- Postgres + pgvector
- Langfuse
- AWS infrastructure box around it

Save as inline Mermaid in `README.md`; GitHub renders Mermaid natively now.

## Decisions to make

1. For identity-design: AWS OR Azure path, or both? Both is more impressive but doubles the document length.
2. For cost-model: use real numbers from your dev runs, or analytical estimates? Real numbers are stronger.
3. Architecture diagram: minimal (just the boxes) or detailed (showing data flow)? Minimal is more readable.

## Stop conditions

- Both docs render correctly in GitHub (especially the Mermaid diagrams)
- README architecture diagram shows the system at a glance
- Cross-references in the README link to both docs

## Don't do yet

- Postmortems (day 11)
- Final README polish (day 12)

## Definition of done

Click through both docs in the GitHub UI. The sequence diagrams render. The numbers in the cost model match reality (your dev-log notes from earlier days).

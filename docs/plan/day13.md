# Day 13 — Loom demo

## Goal

A 6-8 minute video walkthrough. Interviewers will watch this BEFORE reading the code. The video is the deliverable.

## Setup

- Loom account (free tier works)
- Quiet room, decent mic (laptop mic is fine if it's not a coffee shop)
- Tabs pre-loaded: GitHub repo, Langfuse dashboard, terminal, AWS console (if doing cloud bit)
- Run through your terminal commands ONCE before recording so you're not fumbling

## Script outline (8 minutes total)

### 0:00-0:30 — Hook
"This is a multi-agent RAG system that helps compliance analysts draft first-pass FDA submission positions. It's a portfolio project demonstrating production patterns for agentic AI: LangGraph orchestration, a custom MCP server with citation verification, and evaluation as a first-class deliverable. Quick tour."

### 0:30-1:30 — Problem framing
- What compliance analysts actually do today (manual work, hours-to-days)
- Why this is a real problem (cost of getting it wrong)
- What the system produces (draft analysis + verified citations + confidence signals)
- What it explicitly doesn't do (no final determinations)

### 1:30-3:00 — Architecture walkthrough
- Show the README architecture diagram
- Walk through: gateway → orchestrator (4 agents) → MCP server → pgvector + Langfuse
- Pause on the multi-agent decomposition: "These three roles have different objectives — researcher optimizes for recall, analyst for synthesis quality, critic for grounding. Collapsing them dilutes attention."
- Pause on `check_citation`: "This is the project's distinctive piece. The critic doesn't trust the analyst; every claim gets verified against source text."

### 3:00-4:30 — Live query
- Switch to terminal
- Curl a real query (have it pre-typed and ready to paste)
- While it runs (~30s), say what's happening: "Planner is decomposing into sub-questions... researcher is retrieving and reranking... analyst is drafting... critic is verifying each citation against the source."
- Show the response — read out one citation
- Switch to Langfuse, show the trace tree: all four agents nested, MCP tool calls visible
- Click into the critic span, show it called `check_citation` three times

### 4:30-5:30 — Evaluation
- Switch back to terminal
- `cat evals/results/latest.md` or open it in browser
- Walk through the three scorers: "Citation validity is deterministic — string match against the source. Key fact coverage uses Haiku as judge. Position quality uses Sonnet WITH the source passages in context — that's the anti-reward-hacking design."
- Show the per-difficulty-band table: "The 5 hard questions test refusal-to-hallucinate. That's the regulated-vertical feature."
- Show your CI workflow file or a PR with the eval gate blocking a merge

### 5:30-6:30 — What broke (pick ONE postmortem to walk through)
- Open the most compelling postmortem
- <!-- TODO (record at Day 14): replace this placeholder with the real Day-7 story.
     Real story: matcher preprocessing fixes (smart-quote normalization + PDF line-number stripping)
     lifted quote verification from 309/446 → 386/446 at τ=0.85. recall@10=1.00 on the golden set
     after matcher fixes. The investigation showed delta=0 across corpus arms — the problem was the
     matcher, not the corpus or the chunker. The fabricated "recall 0.71 → 0.88 / embedding swap"
     narrative below is PLACEHOLDER ONLY and must NOT appear in the recorded Loom. -->
- **[PLACEHOLDER — replace before recording]** Quick summary: "Eval showed quote verification failing on 30% of cases. First hypothesis was corpus cleaning / rechunking. Ran a $0 text-only smoke across both corpus arms — delta was zero, not the corpus. Real fix was normalizing curly-quotes and stripping PDF-embedded line numbers in the matcher — verification went from 309/446 to 386/446 at the same τ."
- Why this matters: "RAG debugging is mostly retrieval debugging, and you don't know what's broken until you measure it — the corpus-arm delta was the key diagnostic."

### 6:30-7:30 — Production posture
- "Day 8-9 was a cloud deploy: Terraform brought up VPC, ECS Fargate, RDS Postgres with pgvector. Round-trip under $5 to deploy and destroy."
- Show terraform plan output OR a screenshot from the live deploy
- Mention identity-design.md: "Production identity is OAuth 2.0 with agent-as-NHI; designed end-to-end, deferred implementation to keep v1 focused on AI engineering."

### 7:30-8:00 — What's next
- Point at future-work.md: "Each deferred item has a specific trigger that would reopen it. Most likely next: hybrid retrieval if recall stalls, OAuth implementation if a real user lands."
- "Code is at github.com/[you]/regulatory-research-assistant. Thanks for watching."

## Tips that actually matter

- **Record in one take after rehearsing**, don't try to edit. Loom doesn't make editing easy and the rough-edged version reads as authentic, not slick.
- **Talk slower than feels natural**. New listeners need processing time on technical content.
- **Show, don't tell**. Every claim should have a visible artifact on screen.
- **No "um, so, basically".** Re-record if you start with filler.
- **Pre-write the first 10 seconds verbatim.** Hook is the hardest part to land cold.

## After recording

- Loom auto-transcribes; check the title and description
- Set thumbnail (Loom lets you pick a frame; pick the architecture diagram frame)
- Visibility: anyone with the link
- Update README: replace the placeholder Loom URL with the real one
- Commit: `docs: add demo video link`

## Stop conditions

- One Loom video, under 9 minutes, that you'd be okay with a senior engineer watching
- README updated with real URL
- Thumbnail set

## Anti-patterns

- **Recording the whole project building.** You're showing the finished thing, not narrating the journey.
- **Reading slides.** This is a screen-share with live terminal, not a presentation.
- **Apologizing for issues.** "Sorry, this is a bit slow" → cut it. The viewer doesn't know what "normal" is.
- **Over-engineering the production polish.** Loom is informal by design; lean into that.

## If you're under time pressure

A 4-minute "minimum viable demo" that hits architecture → live query → eval table → close is fine. Better short and tight than long and meandering.

## Definition of done

You watched the playback once and didn't cringe at more than two moments. Ship it.

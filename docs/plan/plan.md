# Project plan

> **For Claude Code:** This is the 15-day build plan. Each day's detail is in `docs/plan/dayNN.md`. At session start, read `docs/dev-log.md` first to find current day, then read the matching `dayNN.md` for today's specifics. Do not start a later day's work before the current day's stop conditions are met.

## Status

Current day: **see `docs/dev-log.md` for what's actually complete.**

The day numbers below are calendar position, not completion status. The dev log is authoritative.

## Overview

| Day | Theme | Primary deliverable | Detail |
|---|---|---|---|
| 1 | Foundation | Scaffold, docker stack, config, eval harness skeleton | [day01.md](plan/day01.md) |
| 2 | Ingestion | `ingest.py`: corpus → chunks → embeddings → Postgres | [day02.md](plan/day02.md) |
| 3 | Basic RAG | `api.py` + retrieval-only endpoint, no agents yet | [day03.md](plan/day03.md) |
| 4 | Multi-agent | LangGraph orchestrator with 4 agents | [day04.md](plan/day04.md) |
| 5 | MCP tools | Custom MCP server with `check_citation` | [day05.md](plan/day05.md) |
| 6 | Evals | 30 golden questions, harness wired, baseline run | [day06.md](plan/day06.md) |
| 7 | Fix #1 | Address worst weakness from evals → postmortem 1 | [day07.md](plan/day07.md) |
| 8 | IaC | Terraform: VPC, ALB, RDS, ECS task definition | [day08.md](plan/day08.md) |
| 9 | Deploy | Cloud deploy + smoke test + tear down | [day09.md](plan/day09.md) |
| 10 | **Session tracking** | Persist + surface `session_id`; activate `app.query_audit` (ADR 0015) | [day10-session-tracking.md](plan/day10-session-tracking.md) |
| 11 | Design docs | OAuth + cost model + architecture diagram | [day10.md](plan/day10.md) |
| 12 | Postmortems | Three "what broke" writeups | [day11.md](plan/day11.md) |
| 13 | Polish | README rewrite, eval table, repo hygiene | [day12.md](plan/day12.md) |
| 14 | Loom demo | Record 6–8 minute video | [day13.md](plan/day13.md) |
| 15 | Buffer | Whatever broke, last polish, final push | [day14.md](plan/day14.md) |

> **Renumber note (ADR 0015, 2026-06-02):** Day 10 "Session tracking" was inserted
> after the cloud demo; the original Day 10–14 shifted to Day 11–15. The shifted
> skeleton detail files keep their **original numeric names** (e.g. Day 11 "Design
> docs" → `day10.md`) to avoid a churny rename across the cross-referenced day
> plans — the **Day column above is authoritative** ("day numbers are calendar
> position"). Some "(day N)" mentions inside the older skeleton files may still use
> pre-insertion numbers.

## Cut-list if running behind

Drop in this order (least to most critical):

1. **Day 14 stretches** — talk about cloud deploy in the video instead of recording it live
2. **Day 11 cost-model.md** — a paragraph in the README will do
3. **Day 9 eval-against-cloud** — smoke test alone is fine
4. **Day 8–9 entirely** — Terraform on a local-only branch with screenshots, no actual deploy

## Never cut

- Day 6 evals — the JD's headline ask
- Day 5 MCP `check_citation` — the project's distinctive piece
- Day 12 postmortems — interview gold
- Day 14 Loom — the demo IS the deliverable

## Day-start ritual

Every session begins with:

```
Read CLAUDE.md, docs/dev-log.md, and docs/plan/dayNN.md (the current day).
Tell me:
  1. What's actually complete from yesterday
  2. What today's deliverable is
  3. What prerequisite to flag if it's missing
```

Then we work the day. End of day, append to dev-log.md what got done, what didn't, what surprised.

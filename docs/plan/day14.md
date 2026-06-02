# Day 14 — Buffer

## Goal

Whatever didn't get finished. The buffer was always part of the plan.

## Likely uses for today

Pick whichever applies. In rough priority order:

### Catch-up

Most likely. Some earlier day ran long. Finish it before doing anything else.

### Polish that bothered you

The README intro that felt weak. The postmortem that needs another pass. The architecture diagram that's not quite right. The dev-log that's missing entries for days 5-7.

These are NOT essential, but if you have the time and the energy, this is when senior-engineer-level finish happens.

### Real eval improvements

If the eval scores are okay but not impressive, today is your chance to push one more metric up. Apply day 7's process again: pick one weakness, make one change, measure.

### Reach-out preparation

- Update your LinkedIn with the project + Loom link
- Write a short post about it (LinkedIn or Twitter/X) — interviewers find these
- Update your resume bullet for this work (try to compress to 2-3 lines, lead with measurable outcomes)
- Identify 5 companies/roles where this work is directly relevant (start with the JD that motivated this project)

### Demo dry-run with a friend

If you know another engineer, share the Loom and ask them to ask you the hardest question they can think of after watching. Their question is probably one an interviewer will also ask.

## What NOT to do

- **Add a new feature.** You'll break something. Trust the project.
- **Rewrite a doc from scratch.** Edit it; don't restart.
- **Switch the framework or major decision.** This is the day for finish, not rebuild.
- **Submit applications before recording the Loom is done.** The video is the deliverable.

## Stop conditions

- Whatever was hanging is now done
- The repo can be linked in a job application with no caveats
- You can answer "tell me about a project you've worked on recently" with a 2-minute version of this one

## Final pre-flight check

Before you call the project done, walk through this list:

- [ ] README has real eval numbers, not placeholders
- [ ] README has a working Loom URL
- [ ] README has links to spec, future-work, identity-design, cost-model, three postmortems — all working
- [ ] All `[Your name]` placeholders replaced
- [ ] `git log --oneline` shows a clean history
- [ ] Latest commit is on `main`, pushed to GitHub
- [ ] Repo is public (if you intended public)
- [ ] Tag `v0.1` exists and pushed
- [ ] No secrets in git history (`git log --all -p | grep -iE "sk-ant|pk-lf|sk-lf"` returns nothing)
- [ ] `.env` is NOT in the repo
- [ ] Tests pass on a clean checkout (try `git clone` to a new directory and `uv sync && uv run pytest`)
- [ ] One curl example in the README actually works against a local run

If everything is checked, congratulations — you have a senior-level portfolio project.

## What this project gives you

Resume bullet:
> Built a multi-agent RAG system over FDA guidance documents using LangGraph and a custom MCP server. Evaluation harness with deterministic citation-validity scoring and LLM-as-judge metrics; CI-gated regression. Deployed to AWS via Terraform. Three published postmortems with before/after measurements.

Interview talking points:
- The planner-worker-critic decomposition and when it would be wrong
- Why `check_citation` is a real reliability pattern, not boilerplate
- The reward-hacking failure in LLM-as-judge and how passages-in-context fixed it
- Why pgvector does double duty as state store and vector store
- The OAuth 2.0 design even though it's not implemented (shows you can defend deferrals)
- Why LangGraph over CrewAI/AutoGen, and why direct Anthropic SDK over LangChain

You're done.

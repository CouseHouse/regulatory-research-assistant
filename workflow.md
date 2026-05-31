# Daily workflow

The rhythm for every day of this project. Print it, pin it, follow it. When you deviate from it, do so consciously.

---

## Start of day

```bash
cd ~/projects/regulatory-research-assistant
git status                           # confirm clean main
git checkout -b dayN-<slug>          # fresh branch per day
docker compose ps                    # confirm stack is up
```

**First prompt to Claude Code (Opus):**

```
We're starting Day N. Read in order:
  1. CLAUDE.md
  2. docs/dev-log.md (especially yesterday)
  3. docs/plan/dayNN.md
  4. docs/decisions/index.md and any active ADRs relevant to today

Tell me:
  a) What's actually done from yesterday vs. flagged open
  b) Today's primary deliverable and stop conditions
  c) The 3-5 architectural decisions today forces, each flagged
     ADR-worthy or not
  d) Anything blocking today's work

Don't write code or ADRs yet. I want to review your understanding.
```

---

## Phase 1 — Design (Opus, ~30 min)

1. Read Claude's analysis. Check it against your understanding.
2. If shallow or generic, push back: "Be specific about X."
3. For each ADR-worthy decision, decide draft now or defer.
4. Ask Claude to draft ADRs:
   ```
   Draft ADRs for the decisions flagged ADR-worthy. Use
   docs/decisions/0000-template.md. Number sequentially from
   the next available NNNN. Update docs/decisions/index.md.
   Don't implement yet.
   ```
5. Review each ADR for: definitive Decision, real Alternatives reasons, specific Reopen-if triggers.
6. Commit ADRs separately:
   ```bash
   git add docs/decisions/
   git commit -m "docs: ADRs NNNN-NNNN for Day N design decisions"
   ```

---

## Phase 2 — Implement (Sonnet, 1-3 hours)

Switch model: `/model sonnet`

```
ADRs approved and committed. Implement Day N per the plan.

Constraints from CLAUDE.md:
- All config via rra.config.settings (no os.getenv)
- Strict mypy must stay clean
- Conventional commits, one logical change per commit
- Don't add deps without asking

Stop conditions:
- Tests pass: uv run pytest
- mypy clean
- Manual smoke test of [today's deliverable]
- Dev-log entry appended (same shape as previous days)
```

Let it run. Don't watch every step. Check in if it asks for input.

---

## Phase 3 — Review (you, ~20 min)

Don't trust the summary. Trust the artifacts.

```bash
git log --oneline -10                # commit count + messages reasonable?
git diff HEAD~N..HEAD                # read the actual changes
uv run pytest                        # tests pass?
uv run mypy src                      # mypy clean?
```

Check specifically:
- Did Claude touch files outside the scope? (Bad)
- Are conventional commit prefixes correct? (`feat:`, `fix:`, etc.)
- Does the dev-log entry honestly capture decisions, deferrals, surprises?
- Any wrong technical justifications in the dev-log? Edit them.

If anything's wrong, push back with specifics. Don't approve sloppy work.

---

## Phase 4 — Smoke test (you, 10-20 min)

Run the actual deliverable end-to-end against real data. Whatever today produced, exercise it.

```bash
# Example for any day that touches retrieval or queries
uv run python -m rra.<module>    # or curl / pytest -m integration
```

If it works, capture numbers in the dev-log. If it doesn't, debug. Real-data failures are postmortem material.

---

## End of day

```bash
# Confirm dev-log is updated with what landed vs. what was planned
nano docs/dev-log.md

# Final commit if dev-log was edited
git add docs/dev-log.md
git commit -m "docs: end-of-day-N notes"

# Merge to main
git checkout main
git merge dayN-<slug> --ff-only
git push
git branch -d dayN-<slug>

# Tag the milestone
git tag dayN-complete -m "Day N closed: <one-line summary>"
git push --tags
```

---

## When something doesn't work

1. **Read the error.** Don't paraphrase it to Claude — copy-paste the exact traceback.
2. **Diagnose before fixing.** Ask: is this a code bug, a schema drift, a config issue, or external (rate limit, network)?
3. **Fix the root cause, not the symptom.** Workarounds become next week's bugs.
4. **Update all affected artifacts.** Schema bug → fix code AND `init-db/01-init.sql`. Decision change → new ADR superseding old.
5. **Log it.** Every real bug is postmortem material for Day 11.

---

## When Claude proposes something off-spec

Standard response pattern:

```
That conflicts with ADR NNNN / spec.md §X.Y, which chose Z because W.
Either:
  a) The original reasoning no longer holds — draft a superseding ADR
     explaining what changed
  b) There's a path consistent with the existing decision — propose it

Don't silently deviate.
```

---

## Cost-control habits

- **Default to Sonnet.** Switch to Opus only for design conversations.
- **Pattern:** Opus to plan → review → Sonnet to implement → review → Opus to audit (optional).
- **Cap Anthropic spend at $30/day** via console budget alert.
- **Cap AWS at $5** when cloud work starts.
- **`uv run` over `pip install`.** Always.

---

## What to do when you're stuck

In order:
1. Read the relevant ADR or spec section. The answer is often there.
2. Read your own dev-log. You may have already solved this.
3. Ask Claude Code specifically: "Look at X, Y, Z. What am I missing?"
4. If still stuck after 30 min, write the question down, sleep on it.
5. Don't burn budget on Opus debugging at 11pm. Tomorrow-you is sharper.

---

## Never skip

- Dev-log entries (the daily story is the project's evidence)
- ADR cross-checks before architectural changes (no silent drift)
- Smoke tests against real data (mocked tests miss 80% of real bugs)
- Conventional commit messages (interviewers read git log)
- The buffer day (Day 14) — you will need it

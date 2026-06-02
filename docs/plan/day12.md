# Day 12 — Polish

## Goal

The repo looks good when an interviewer lands on the GitHub page.

## Deliverables

### README rewrite
- Replace all placeholders (`[Your name]`, `you@example.com`, the Loom URL placeholder will get filled tomorrow)
- Replace plausible-target eval numbers with real ones from `evals/results/latest.md`
- Architecture diagram from day 10 inlined as Mermaid
- "What broke" section linking to the three postmortems
- One-sentence cost statement: "Deployed to AWS for under $5 round-trip"
- TL;DR at the very top — 2 sentences that explain what the project is

### Repo hygiene
- `ruff check .` clean across the codebase
- `mypy src` clean across the codebase
- `pytest` all tests pass
- `git log --oneline` — commit messages readable end-to-end
- No commented-out dead code in `src/`
- All `TODO(dayN)` markers either resolved or moved to GitHub issues

### GitHub presentation
- Repo description set (`gh repo edit --description "..."`)
- Topics added (`gh repo edit --add-topic ...`)
- Tag the v0.1 release: `git tag v0.1 && git push --tags`
- Star your own repo (it's a small thing, but GitHub's recommendation algorithm cares)
- Pin the repo on your GitHub profile

### Final cross-link check
Walk through the README from top to bottom, click every link:
- spec.md, future-work.md, identity-design.md, cost-model.md
- All three postmortems
- Loom URL placeholder (will be live tomorrow)
- Any image/diagram links

## Decisions to make

1. Public or private repo? At v0.1 you probably want public; you can pin it. If anything's embarrassing (broken postmortem, half-finished doc), private until day 14.
2. Add a LICENSE file? MIT is the lowest-friction. The pyproject.toml already declares it.
3. Add a CONTRIBUTING.md? Probably overkill for a portfolio project. Skip.

## Stop conditions

- README looks clean on GitHub (not just locally)
- All tests, ruff, mypy green
- Repo has description, topics, v0.1 tag
- No placeholder text visible anywhere except the Loom URL

## Anti-patterns to avoid

- **Last-minute features.** You've been disciplined for 11 days. Don't add features today.
- **Cosmetic perfectionism on docs.** The content is the value. A perfect-looking README on a half-finished project doesn't fool interviewers.
- **Reorganizing for the sake of it.** If the structure has worked for 11 days, leave it.

## Don't do yet

- Loom (day 13)
- Switching the project to a different framework "for the demo"
- Submitting to job applications — wait until day 14 is done

## Definition of done

Visit your repo URL in a fresh browser tab (or incognito) and pretend you've never seen it. Can you tell what it does and why it's good in 30 seconds? If yes, day 12 is done.

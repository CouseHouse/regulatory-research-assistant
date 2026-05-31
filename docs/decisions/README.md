# Architecture Decision Records (ADRs)

One file per real decision. Short, focused, retrievable. Based on the Nygard/Cockburn pattern.

## Core principles

1. **Never edit an Accepted ADR's Decision section.** Decisions are immutable artifacts. When facts change, write a new ADR.
2. **Never delete an ADR.** Even superseded ones stay. The history of *why* a decision was made and *why* it changed is the whole value.
3. **Supersession is bidirectional.** When 0014 replaces 0007, both files get updated — 0007 points forward to 0014, 0014 points back to 0007. The lineage is explicit and walkable in either direction.

## Naming

`NNNN-short-slug.md` where NNNN is zero-padded and monotonically increasing. Slugs name what was CHOSEN, not what was rejected.

```
0001-langgraph-for-orchestration.md
0002-pgvector-in-postgres.md
0007-no-redis.md
0014-redis-after-all.md       # supersedes 0007
```

Numbers are never reused. If you abandon a draft, leave a gap or mark it Rejected.

## When to write one

Write an ADR when:
- A choice has multiple defensible alternatives
- The choice constrains future work
- A future person (or Claude Code) might reasonably propose the rejected alternative

Don't write one for:
- Trivial style preferences (CLAUDE.md or pyproject.toml)
- Decisions already in `spec.md` (link to the section instead)
- Things reversible in under an hour

## Format

Every ADR follows the template in `0000-template.md`. Required fields:

```
**Status:** Active | Proposed | Superseded by NNNN | Deprecated | Rejected
**Date:** YYYY-MM-DD
**Owner:** [name]
**Supersedes:** NNNN  (only if this ADR replaces an earlier one; otherwise omit)
```

Sections: Context, Decision, Alternatives considered, Consequences, Related.

Stay under 100 lines. If it's longer, it's a design doc, not an ADR.

## Status values

- **Proposed** — drafted, not yet committed to. Convertible to Active by explicit acceptance.
- **Active** — in force. This is the default state for ADRs Claude Code should consult.
- **Superseded by NNNN** — replaced by a later ADR. Still readable for historical context, but not the current truth.
- **Deprecated** — the decision no longer applies but wasn't replaced (e.g., the system component was removed). Kept for history.
- **Rejected** — written, debated, decided against. Kept so the rejection is documented and the same proposal doesn't re-emerge.

## The supersede chain

When a decision changes, do this exactly:

1. **Write the new ADR with the next available number** (e.g., 0014).
2. **In the new ADR's header**, add `**Supersedes:** 0007`.
3. **In the new ADR's body**, add a "What changed" section explaining why 0007's reasoning no longer holds.
4. **Edit ONLY the header of the old ADR (0007)**. Change `Status: Active` to `Status: Superseded by 0014`. Do not change the body — the original Decision and Context must remain readable.
5. **Update `index.md`** to reflect the new state.

Result:

```
decisions/0007-no-redis.md
  Status: Superseded by 0014
  [original body intact]

decisions/0014-redis-after-all.md
  Status: Active
  Supersedes: 0007
  [new context, new decision, what changed]
```

A future reader can walk 0007 → 0014 to see the lineage, or 0014 → 0007 to see what was rejected and why the rejection was overturned.

## How Claude Code uses these

CLAUDE.md instructs Claude to:

1. **Check `decisions/` before proposing any architectural alternative**, especially anything touching: orchestration framework, model provider, vector store, identity layer, observability, deployment target, or dependency additions.
2. **Filter on `Status: Active` by default.** Don't surface superseded decisions as current.
3. **Read superseded ADRs only when explicitly asked for history**, OR when a proposed change would re-introduce an alternative that was previously rejected (to see whether the rejection reasoning still holds).
4. **When proposing a new decision that conflicts with an Active ADR**, surface the ADR and propose superseding it explicitly. Never silently contradict an Active ADR.

## Index

See `index.md` for a flat list with status and one-line summary. Update it whenever you add or supersede.

# 0018 — Bootstrap image bakes the cached corpus (FDA blocks the Fargate IP)

**Status:** Active
**Date:** 2026-06-08
**Owner:** Kyle Couse
**Supersedes:** 0017

## Context

The first real cloud bootstrap under ADR 0017 failed: all 50 FDA PDF downloads 4xx-failed
*instantly* (non-retryable, ~25 ms each) from the Fargate datacenter IP — Akamai blocks datacenter
ranges, and the same URLs return `200` from a residential IP. ADR 0017's "re-download in-VPC via NAT"
path had **never actually run** before deploy: local ingest short-circuited on `dest.exists()`
(`src/rra/ingest.py:188`) because `data/corpus/` was already populated, so the live fetch was untested.

## Decision

We will bake the cached corpus PDFs (`data/corpus/*.pdf`) into the bootstrap image so `rra.ingest`
serves them from the on-disk cache and performs no FDA download at deploy time.

## Alternatives considered

- **Spoof a browser User-Agent / headers** — Rejected. The block is IP-based (datacenter range), not
  UA-based: `200` from a residential IP, 4xx from Fargate regardless of UA. Any UA fix is unreliable.
- **Egress via a residential/forward proxy to dodge the IP block** — Rejected. Heavy, fragile, and a
  poor look for a regulated-vertical narrative (deliberately evading a `.gov` CDN's controls).
- **Mirror the corpus to S3 and pull at bootstrap** — Rejected *for now*. More infrastructure than a
  one-off demo needs; the PDFs already exist locally and are immutable published guidance. This is the
  natural reopen path if the corpus outgrows the repo (see Reopen).

## Consequences

**Enables:**
- A deterministic, offline-capable bootstrap with no deploy-time dependency on FDA's CDN — the failure
  mode that just bit us cannot recur.
- A reproducible corpus: what ships in the image is exactly what gets ingested.

**Constrains:**
- The bootstrap image carries ~45 MB of PDFs, and the *serving* build context grows ~45 MB (the
  serving image is unaffected — it COPYs no `data/`).
- The committed/cached `data/corpus/` becomes the source of truth for ingestion. PDFs are gitignored,
  so a **fresh clone (e.g. CI) must repopulate `data/corpus/` before building the bootstrap image** —
  from a residential network or, eventually, S3. Noted as a build prerequisite.

**Reopen if:**
- The corpus moves to object storage (S3) — pull from there at bootstrap instead of baking.
- FDA stops blocking datacenter IPs AND live freshness becomes desirable — revisit re-download.

## What changed (supersedes ADR 0017)

ADR 0017 assumed the bootstrap could re-download PDFs from FDA in-VPC via NAT, baking only the 37 KB
manifest. The first cloud run disproved that assumption — FDA/Akamai blocks the Fargate IP (50/50
instant 4xx) — and revealed the assumption had never been exercised (local runs used the cached PDFs).
**0017's core decision is unchanged and still valid**: bootstrap via a one-off in-VPC ingest task on
the existing `ecs_tasks` SG, reusing NAT egress (still needed for Voyage) and Secrets Manager. Only the
corpus-sourcing detail flips: **baked-in cache instead of runtime re-download.** Everything else in
0017 (the task def, SG reuse, ANTHROPIC+VOYAGE+DB-password injection, the run-task runbook) stands.

## Related

- dev-log 2026-06-08 (the failed run + diagnosis: `200` residential vs 4xx Fargate).
- ADR 0017 (superseded) — the bootstrap mechanism this refines.
- `.dockerignore` (no longer excludes `data/corpus/*.pdf`), `Dockerfile` `bootstrap` target.

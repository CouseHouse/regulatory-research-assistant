# 0021 — Identity/NHI port: deny-by-default tool scoping at the transport chokepoint

**Status:** Accepted
**Date:** 2026-06-11
**Owner:** Kyle Couse (drafted by Claude in the ports/adapters/security refactor)

## Context

The system had one identity: a single `X-API-Key` checked with a
timing-attack-prone `!=` at the API edge, and nothing below it — any agent
could call any tool. The refactor's security mandate requires least-privilege
non-human identities (NHI) whose enforcement promotes with the agent across
profiles, and a single place where tool access is decided.

## Decision

Identity is a first-class port (`src/rra/ports/identity.py`): a frozen
`Principal` (name, kind, frozenset scopes), `verify_api_caller` using
`secrets.compare_digest`, `agent_principal(role)` failing closed on unknown
roles, and `authorize_tool` that is deny-by-default. Enforcement lives at the
tool-transport chokepoint (ADR 0020): every `call_tool` requires a `Principal`
and authorizes **before** tool-existence lookup, so probing tool names
requires a scope. Local scopes equal observed usage exactly: researcher →
{search_corpus}, critic → {check_citation}, planner/analyst → {} —
so behavior is unchanged while enforcement is real going forward.

## Trust model — read this before relying on it

The local adapter is **advisory intra-process scoping, not a forgery-resistant
boundary** (Security Critic review, phase 2b). `authorize_tool` trusts the
caller-supplied `Principal`; any code in the process can request another
role's principal or construct one. That is acceptable at this trust level
because all agent nodes are first-party code in one process — the port's job
locally is least-privilege scoping of honest agents plus establishing the
chokepoint. The *enforced* boundary in the local profile is the HTTP API key.
Non-forgeable per-agent identity is exactly what the cloud adapters bind
behind this same port (AgentCore Identity / Entra Agent ID / GCP workload
identity), which is why the port exists before the enforcement is cryptographic.

Threat-model note (carried from review): the `tool` argument to `call_tool`
must remain a caller-side literal — never user- or retrieval-derived — so the
denial log stays free of injected content.

## Alternatives considered

- **Keep the single API key, add scoping later with the clouds** — Rejected:
  the chokepoint and scope registry must exist before the cloud adapters or
  the agents' tool paths get rebuilt twice.
- **Per-agent API keys / JWTs locally** — Rejected: cryptographic ceremony
  inside one process adds operational surface without changing the trust
  model (the signer would live in the same process).
- **Authorization inside each tool function** — Rejected: N enforcement
  points that drift; the transport chokepoint is single and grep-able.

## Consequences

**Enables:** least-privilege NHI per agent; the security harness can
demonstrate scope-violation denial; cloud identity services drop in behind a
stable port.

**Constrains:** new tools/agents must register scopes explicitly (deny by
default); every agent tool call goes through `call_tool` — direct tool imports
in agent code are a review-blocking defect. Exemptions (documented): the MCP
server (`mcp_server/server.py`) is an exposure adapter calling tools.py
directly — its callers are governed by the MCP transport's own auth story when
remote transport lands; the eval harness (`evals/`) is trusted local tooling.

**Reopen if:** agents move out-of-process (the advisory model breaks — the
port contract holds, adapters must then verify, not trust, principals), or a
cloud identity service cannot express per-tool scopes.

## Related

ADR 0019 (profiles), 0020 (ports; the chokepoint), 0022 (guardrails);
docs/identity-design.md (the v1 single-key design this extends).

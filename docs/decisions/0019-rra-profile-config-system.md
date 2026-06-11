# 0019 — RRA_PROFILE config/profile system

**Status:** Accepted
**Date:** 2026-06-11
**Owner:** Kyle Couse

## Context

The ports/adapters refactor (see `docs/refactor/00-master-plan.md`) requires
the system to run identically across four environments — `local`, `aws`,
`azure`, `gcp` — with zero code changes.  Config and adapters are the only
things that change between environments.  Before this ADR, the system had a
single flat `Settings` class with no environment concept; there was no way to
promote from local development to a cloud profile without editing code or
maintaining parallel `.env` files that inevitably drift.

## Decision

We will add an `RRA_PROFILE` field (`Literal["local","aws","azure","gcp"]`,
default `"local"`) to `Settings` and a module-level `PROFILE_DEFAULTS` dict
that injects per-profile config values for fields not explicitly supplied via
the environment.  Strict source precedence is: explicit env var > `.env` file
> profile default > field default.  A value already present in the environment
is never overwritten by a profile default.  Adapter phases (Phase 2+) wire
adapters behind this field; this phase only registers the field and applies
per-profile config defaults.

## Alternatives considered

- **Separate `.env` files per environment** (e.g. `.env.aws`) — Rejected
  because per-env files drift against each other silently; the "same code,
  different config" guarantee requires a single code path, not N files.
- **Build-time config baking** — Rejected because it breaks the
  one-immutable-artifact promotion model: the same Docker image must run
  locally and in any cloud environment without rebuilding.
- **Environment-specific Settings subclasses** — Rejected because it
  scatters config into N classes and makes the precedence rule invisible; a
  single class with a validator is easier to audit and test.

## Consequences

**Enables:**
- Promoting a single container image from `local` → cloud profile by changing
  only env vars (or a secrets manager injection), with no code changes.
- Each adapter phase can expand `PROFILE_DEFAULTS` in its own commit rather
  than scattering profile knowledge across adapter files.
- A clear, testable precedence guarantee: `PROFILE_DEFAULTS` can never
  override what an operator explicitly set.

**Constrains:**
- `PROFILE_DEFAULTS` must never contain secret values (enforced by test
  `test_no_secret_in_profile_defaults`).  Secrets must always come from the
  operator (env var, `.env`, or a secrets manager).
- Future adapter phases must use `RRA_PROFILE` for adapter selection; they
  must not introduce parallel environment-detection mechanisms.
- Any new cloud profile (`staging`, etc.) requires both a new
  `Literal` variant in the `rra_profile` field and a new entry in
  `PROFILE_DEFAULTS`.

**Reopen if:**
- A cloud provider requires a fundamentally different config-loading mechanism
  (e.g. a provider SDK that must be called before pydantic-settings runs).

## Security implications

- **Explicit-env-beats-profile precedence** means a compromised env var can
  override a profile default.  This is mitigated by secrets-manager injection
  in cloud profiles: env vars are injected from a controlled secrets source
  (AWS Secrets Manager, Azure Key Vault, GCP Secret Manager), not from
  operator-supplied files.
- **No secret defaults in `PROFILE_DEFAULTS`, ever.**  A hardcoded credential
  default would be silently accepted if the env var were missing — exactly the
  fail-fast problem the required-no-default fields in `Settings` were designed
  to prevent.  The constraint is enforced by `test_no_secret_in_profile_defaults`.
- **SecretStr masking is pinned by tests.**  `test_model_dump_json_masks_secrets`
  and `test_model_dump_secrets_are_wrapped` ensure that a future field-type
  change from `SecretStr` to plain `str` fails loudly rather than silently
  exposing credentials in dumps or traces.
- **`pg_dsn` is the known exception.**  It is a `@computed_field` that
  necessarily embeds the postgres password in the connection string.  It is
  NOT a `SecretStr` (connection pool libraries expect a plain string).  Tests
  document and pin this: `test_pg_dsn_contains_password` and
  `test_pg_dsn_is_only_computed_field_with_password`.

## Related

- `docs/refactor/00-master-plan.md` — refactor north star and phased plan
- `src/rra/config.py` — implementation
- `tests/test_no_secret_leak.py` — security test suite pinning the above
- ADR 0001, 0003 — orchestration and SDK choices that constrain provider
  selection in cloud profiles

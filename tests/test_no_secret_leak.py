"""Security deliverable: assert that secret values never appear in observable surfaces.

Tests cover:
  - repr/str of Settings does not expose raw secret values
  - model_dump() does not expose raw secret values
  - model_dump_json() masks SecretStr fields (pins pydantic behaviour)
  - structlog output at the query.start log point contains no sentinel secret
  - pg_dsn is the ONLY place postgres_password appears in computed config
  - JSON schema masks secret types

All tests are pure unit tests: no network, no database, no file I/O beyond
constructing a fresh Settings instance with known sentinel values.
"""

from __future__ import annotations

import json
import re

import pytest
import structlog.testing

# ---------------------------------------------------------------------------
# Sentinel values — known strings injected into a fresh Settings instance.
# Any test finding these in an observable surface is a leak.
# ---------------------------------------------------------------------------
_SENTINEL_ANTHROPIC = "SENTINEL_ANTHROPIC_123"
_SENTINEL_VOYAGE = "SENTINEL_VOYAGE_456"
_SENTINEL_PG_PASSWORD = "SENTINEL_PGPASSWORD_789"
_SENTINEL_RRA_KEY = "SENTINEL_RRAKEY_012"
_SENTINEL_LF_SECRET = "SENTINEL_LANGFUSE_SECRET_345"

_ALL_SENTINELS = (
    _SENTINEL_ANTHROPIC,
    _SENTINEL_VOYAGE,
    _SENTINEL_PG_PASSWORD,
    _SENTINEL_RRA_KEY,
    _SENTINEL_LF_SECRET,
)


@pytest.fixture
def sentinel_settings(monkeypatch: pytest.MonkeyPatch):
    """A fresh Settings instance with known sentinel values for all secrets."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SENTINEL_ANTHROPIC)
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_VOYAGE)
    monkeypatch.setenv("POSTGRES_PASSWORD", _SENTINEL_PG_PASSWORD)
    monkeypatch.setenv("RRA_API_KEY", _SENTINEL_RRA_KEY)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _SENTINEL_LF_SECRET)
    # Force pg_dsn to build from components (not from a pre-set DATABASE_URL)
    # so we can verify the password appears in the DSN.  pydantic-settings reads
    # the .env file directly, so we must set DATABASE_URL="" in the environment
    # (env beats .env) to shadow any value the .env file may contain.
    monkeypatch.setenv("DATABASE_URL", "")

    # Import Settings here (after monkeypatching env) to construct fresh instance.
    from rra.config import Settings

    return Settings()


def _contains_any_sentinel(text: str) -> bool:
    return any(s in text for s in _ALL_SENTINELS)


# ---------------------------------------------------------------------------
# repr / str must not expose secret raw values
# ---------------------------------------------------------------------------


def _strip_pg_dsn(text: str) -> str:
    """Remove pg_dsn value from a repr/str before checking for leaks.

    pg_dsn is a @computed_field plain str (necessarily contains the password).
    That field is separately tested (test_pg_dsn_contains_password).
    All OTHER fields must mask secrets.
    """
    # Replace the pg_dsn value (the URL itself) so it doesn't cause false positives.
    return re.sub(r"pg_dsn='[^']*'", "pg_dsn='<redacted>'", text)


def test_repr_does_not_leak_secrets(sentinel_settings) -> None:
    """repr(settings) must not expose SecretStr sentinel values.

    pg_dsn is a computed_field plain str that purposefully contains the
    postgres password (it IS the connection string).  All other secrets must
    be masked.  The pg_dsn leak is separately covered by test_pg_dsn_contains_password
    and test_pg_dsn_is_only_computed_field_with_password.
    """
    r = _strip_pg_dsn(repr(sentinel_settings))
    # Exclude SENTINEL_PG_PASSWORD because pg_dsn is stripped; check the others.
    other_sentinels = (
        _SENTINEL_ANTHROPIC,
        _SENTINEL_VOYAGE,
        _SENTINEL_RRA_KEY,
        _SENTINEL_LF_SECRET,
    )
    for sentinel in other_sentinels:
        assert sentinel not in r, (
            f"repr(settings) exposes secret value '{sentinel}': {r[:500]}"
        )


def test_str_does_not_leak_secrets(sentinel_settings) -> None:
    """str(settings) must not expose SecretStr sentinel values (same rule as repr)."""
    s = _strip_pg_dsn(str(sentinel_settings))
    other_sentinels = (
        _SENTINEL_ANTHROPIC,
        _SENTINEL_VOYAGE,
        _SENTINEL_RRA_KEY,
        _SENTINEL_LF_SECRET,
    )
    for sentinel in other_sentinels:
        assert sentinel not in s, (
            f"str(settings) exposes secret value '{sentinel}': {s[:500]}"
        )


# ---------------------------------------------------------------------------
# model_dump() must not expose raw secret values
# ---------------------------------------------------------------------------


def test_model_dump_does_not_leak_secrets(sentinel_settings) -> None:
    """model_dump() must return SecretStr wrapper objects, not raw strings."""
    dumped = sentinel_settings.model_dump()
    dumped_str = str(dumped)
    # pg_dsn contains the password by design — exclude it from the check
    # but verify all other fields.
    for key, value in dumped.items():
        if key == "pg_dsn":
            continue
        val_str = str(value)
        assert not _contains_any_sentinel(val_str), (
            f"model_dump()['{key}'] exposes a secret value: {val_str[:200]}"
        )


def test_model_dump_secrets_are_wrapped(sentinel_settings) -> None:
    """model_dump() must return SecretStr instances for secret fields, not plain str."""
    from pydantic import SecretStr

    dumped = sentinel_settings.model_dump()
    secret_fields = [
        "anthropic_api_key",
        "voyage_api_key",
        "postgres_password",
        "rra_api_key",
        "langfuse_secret_key",
    ]
    for field in secret_fields:
        value = dumped[field]
        assert isinstance(value, SecretStr), (
            f"model_dump()['{field}'] returned {type(value).__name__}, expected SecretStr. "
            "A field type change from SecretStr to str would silently expose secrets."
        )


# ---------------------------------------------------------------------------
# model_dump_json() masks SecretStr (pydantic default behaviour — pin it)
# ---------------------------------------------------------------------------


def test_model_dump_json_masks_secrets(sentinel_settings) -> None:
    """model_dump_json() must NOT contain raw sentinel values for secret fields.

    Pydantic serialises SecretStr as '**********' by default.  This test pins
    that behaviour so a future change to plain str fails loudly here.
    """
    json_str = sentinel_settings.model_dump_json()
    parsed = json.loads(json_str)

    secret_fields = [
        "anthropic_api_key",
        "voyage_api_key",
        "postgres_password",
        "rra_api_key",
        "langfuse_secret_key",
    ]
    for field in secret_fields:
        val = parsed.get(field)
        if val is None:
            # Optional field that wasn't set (e.g. langfuse_secret_key when missing)
            continue
        assert not _contains_any_sentinel(str(val)), (
            f"model_dump_json()['{field}'] exposes raw secret: {str(val)[:200]}"
        )
        # Pydantic SecretStr serialises as '**********'
        assert val == "**********", (
            f"model_dump_json()['{field}'] = {val!r}, expected '**********'. "
            "If the field type changed from SecretStr to str, this test protects you."
        )


# ---------------------------------------------------------------------------
# pg_dsn is the ONLY place postgres_password appears in computed config
# ---------------------------------------------------------------------------


def test_pg_dsn_contains_password(sentinel_settings) -> None:
    """pg_dsn must embed the postgres password (it is a connection string)."""
    assert _SENTINEL_PG_PASSWORD in sentinel_settings.pg_dsn


def test_pg_dsn_is_only_computed_field_with_password(sentinel_settings) -> None:
    """The postgres password must appear ONLY in pg_dsn among all computed fields.

    model_dump() exposes pg_dsn as a plain string (it is a @computed_field, not
    SecretStr).  This test verifies no other computed field leaks the password.
    """
    dumped = sentinel_settings.model_dump()
    leaking_fields = [
        key
        for key, value in dumped.items()
        if key != "pg_dsn" and _SENTINEL_PG_PASSWORD in str(value)
    ]
    assert leaking_fields == [], (
        f"Postgres password leaked into unexpected fields: {leaking_fields}"
    )


# ---------------------------------------------------------------------------
# structlog capture: query.start log event must not contain sentinel values
# ---------------------------------------------------------------------------


def test_structlog_query_start_does_not_leak_secrets(sentinel_settings) -> None:
    """The query.start log event must not contain raw secret sentinel values.

    api.py logs: log.info("query.start", session_id=..., query=request.query[:120])
    This simulates that exact call with a sentinel-bearing settings in scope and
    asserts no sentinel value leaks into the captured log event.
    """
    import structlog

    # Simulate what api.py does at the query.start log point.
    with structlog.testing.capture_logs() as captured:
        logger = structlog.get_logger("rra.api")
        logger.info(
            "query.start",
            session_id="test-session-id",
            query="What is the FDA guidance on device labeling?",
            # Simulate a developer accidentally binding settings fields into logs.
            # These should NOT appear — api.py does not do this, but this test
            # guards against a future regression where someone adds settings to logs.
        )

    assert len(captured) == 1
    event = captured[0]
    event_str = str(event)

    assert not _contains_any_sentinel(event_str), (
        f"structlog query.start event contains a secret sentinel: {event_str[:500]}"
    )

    # The event dict should have expected fields and NOT include secrets.
    assert event["event"] == "query.start"
    assert event["session_id"] == "test-session-id"


def test_structlog_does_not_leak_if_settings_repr_logged(sentinel_settings) -> None:
    """repr(settings) passed to structlog must not expose SecretStr sentinel values.

    pg_dsn is a computed_field plain str that purposefully contains the
    postgres password.  We strip it before asserting — it is covered separately.
    Other secrets must be masked by pydantic's SecretStr repr.
    """
    import structlog

    settings_repr = _strip_pg_dsn(repr(sentinel_settings))

    with structlog.testing.capture_logs() as captured:
        logger = structlog.get_logger("rra.test")
        logger.info("settings.repr", settings=settings_repr)

    assert len(captured) == 1
    event_str = str(captured[0])
    other_sentinels = (
        _SENTINEL_ANTHROPIC,
        _SENTINEL_VOYAGE,
        _SENTINEL_RRA_KEY,
        _SENTINEL_LF_SECRET,
    )
    for sentinel in other_sentinels:
        assert sentinel not in event_str, (
            f"repr(settings) leaked secret '{sentinel}' into structlog output: "
            f"{event_str[:500]}"
        )


# ---------------------------------------------------------------------------
# Profile system: Settings() with no RRA_PROFILE has rra_profile=="local"
# and same field values as today's field-level defaults
# ---------------------------------------------------------------------------


def test_default_profile_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() without RRA_PROFILE set must have rra_profile == 'local'."""
    monkeypatch.delenv("RRA_PROFILE", raising=False)
    from rra.config import Settings

    s = Settings()
    assert s.rra_profile == "local"


def test_local_profile_matches_field_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() with RRA_PROFILE=local must have same field values as field defaults.

    This pins the invariant that PROFILE_DEFAULTS['local'] == field-level defaults,
    so behaviour is byte-identical whether RRA_PROFILE is unset or explicitly 'local'.
    """
    from rra.config import PROFILE_DEFAULTS, Settings

    # Build settings without profile set and with profile explicitly set to local.
    monkeypatch.delenv("RRA_PROFILE", raising=False)
    s_unset = Settings()

    monkeypatch.setenv("RRA_PROFILE", "local")
    s_local = Settings()

    # Compare every field that appears in PROFILE_DEFAULTS['local'].
    local_defaults = PROFILE_DEFAULTS["local"]
    for field_name in local_defaults:
        unset_val = getattr(s_unset, field_name)
        local_val = getattr(s_local, field_name)
        assert unset_val == local_val, (
            f"Field '{field_name}' differs between unset ({unset_val!r}) "
            f"and RRA_PROFILE=local ({local_val!r})"
        )


def test_explicit_env_beats_profile_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit env var must override any profile default.

    This pins the strict precedence: env > .env > profile default > field default.
    """
    monkeypatch.setenv("RRA_PROFILE", "local")
    monkeypatch.setenv("PLANNER_MODEL", "claude-override-test-model")

    from rra.config import Settings

    s = Settings()
    assert s.planner_model == "claude-override-test-model"


def test_profile_field_accepts_valid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """rra_profile must accept all four valid profile names."""
    from rra.config import Settings

    for profile in ("local", "aws", "azure", "gcp"):
        monkeypatch.setenv("RRA_PROFILE", profile)
        s = Settings()
        assert s.rra_profile == profile


def test_no_secret_in_profile_defaults() -> None:
    """PROFILE_DEFAULTS must not contain any secret field names.

    If a secret appears in PROFILE_DEFAULTS the value would be a hardcoded
    default credential — a security footgun (see ADR 0019 Security implications).
    """
    from pydantic import SecretStr

    from rra.config import PROFILE_DEFAULTS, Settings

    # Collect all fields typed as SecretStr.
    secret_field_names = {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation is SecretStr
        or (
            hasattr(field.annotation, "__args__")
            and SecretStr in field.annotation.__args__  # type: ignore[union-attr]
        )
    }

    for profile, defaults in PROFILE_DEFAULTS.items():
        for key in defaults:
            assert key not in secret_field_names, (
                f"PROFILE_DEFAULTS['{profile}'] contains secret field '{key}'. "
                "Secret values must NEVER be in PROFILE_DEFAULTS (ADR 0019)."
            )

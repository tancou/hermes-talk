"""Host compatibility for routed profiles sharing one Discord transport."""

from types import SimpleNamespace

import pytest

import talk_auth
import talk_config
import talk_discord


@pytest.mark.parametrize("allowed", [True, False])
def test_host_adapter_verdict_is_authoritative(allowed):
    primary = object()
    calls = []

    def resolve(profile):
        calls.append(profile)
        return {"discord": primary} if allowed else {}

    runner = SimpleNamespace(
        adapters={"discord": primary},
        _profile_adapters={"devops": {}},
        _adapters_for_profile=resolve,
    )
    assert talk_discord._resolve_discord_adapter_for_profile(runner, "devops") is (
        primary if allowed else None
    )
    assert calls == ["devops"]


def test_host_adapter_error_does_not_borrow_primary():
    def resolve(profile):
        raise PermissionError("profile denied")

    runner = SimpleNamespace(adapters={"discord": object()}, _adapters_for_profile=resolve)
    with pytest.raises(PermissionError):
        talk_discord._resolve_discord_adapter_for_profile(runner, "unknown")


def test_auth_uses_scoped_key_and_policy(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "foreign-key")
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "true")
    scope = {"TALK_OPENAI_API_KEY": "profile-key", "TALK_PREFER_CODEX_OAUTH": "false"}
    monkeypatch.setattr(
        talk_config, "_hermes_get_secret", lambda key, default=None: scope.get(key, default)
    )
    auth = talk_auth.resolve_auth()
    assert auth.token == "profile-key"
    assert auth.source == talk_auth.SOURCE_CONFIGURED


def test_auth_propagates_missing_scope(monkeypatch):
    class UnscopedSecretError(RuntimeError):
        pass

    def missing(key, default=None):
        raise UnscopedSecretError(key)

    monkeypatch.setattr(talk_config, "_hermes_get_secret", missing)
    with pytest.raises(UnscopedSecretError):
        talk_auth.resolve_auth()


def test_routed_oauth_does_not_read_vendor_file(monkeypatch):
    monkeypatch.setattr(talk_auth, "_resolve_hermes_codex_oauth", lambda: None)
    monkeypatch.setattr(talk_auth, "_hermes_codex_oauth_state", lambda now: None)
    monkeypatch.setattr(talk_auth, "_vendor_login_allowed", lambda: False)

    def forbidden(*args):
        pytest.fail("routed profile read the external Codex login")

    monkeypatch.setattr(talk_auth, "_codex_auth_path", forbidden)
    with pytest.raises(talk_auth.TalkAuthError):
        talk_auth.resolve_auth(env={"TALK_PREFER_CODEX_OAUTH": "true"})
    receipt = talk_auth.auth_diagnostic(env={"TALK_PREFER_CODEX_OAUTH": "true"})
    assert receipt["configured"] is False


def test_numeric_timeout_uses_profile_scope(monkeypatch):
    monkeypatch.setenv("TALK_API_SERVER_PROBE_TIMEOUT_S", "99")
    monkeypatch.setattr(talk_config, "_hermes_get_secret", lambda key, default=None: "0.75")
    assert talk_config.api_server_probe_timeout_s() == 0.75

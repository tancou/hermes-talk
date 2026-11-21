"""Deployment preferences are versioned, bounded and loaded for each new session."""
import pytest

import talk_identity as identity


def test_deployment_preferences_preserve_delegated_tool_contract():
    identity.TALK_INSTRUCTIONS_PATH.write_text("Keep spoken answers concise.", encoding="utf-8")
    prompt = identity.build_instructions({"USER": "Operator identity"}, delegated_work=True)
    assert "Keep spoken answers concise." in prompt
    assert "Operator identity" in prompt
    assert "submit_task" in prompt and "submission_status" in prompt
    assert "delegate_task" not in prompt


@pytest.mark.parametrize("build", [identity.build_instructions, identity.build_live_instructions])
def test_optional_instructions_reload_without_replacing_host_contract(build, caplog):
    baseline = build({"USER": "Operator identity"})
    path = identity.TALK_INSTRUCTIONS_PATH
    path.write_text("Keep replies brief — do not repeat the request.", encoding="utf-8")
    with caplog.at_level("INFO"):
        prompt = build({"USER": "Operator identity"})
    assert "Keep replies brief" in prompt
    assert "Operator identity" in prompt
    assert prompt.startswith(baseline.split("\n\n")[0])
    assert "Loaded TALK.md" in caplog.text
    assert "Keep replies brief" not in caplog.text
    path.write_text("Ask clarification questions aloud.", encoding="utf-8")
    updated = build({"USER": "Operator identity"})
    assert "Ask clarification questions" in updated and "Keep replies brief" not in updated
    path.write_text("  \n", encoding="utf-8")
    assert "Deployment voice preferences" not in build({})


def test_invalid_custom_instructions_are_not_silently_truncated():
    path = identity.TALK_INSTRUCTIONS_PATH
    path.write_text("x" * (identity.TALK_INSTRUCTIONS_LIMIT + 1), encoding="utf-8")
    with pytest.raises(ValueError, match=r"TALK\.md"):
        identity.build_instructions({})
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        identity.build_instructions({})


def test_custom_instructions_accept_exact_limit_and_surface_read_errors(monkeypatch):
    path = identity.TALK_INSTRUCTIONS_PATH
    path.write_text("♞" * identity.TALK_INSTRUCTIONS_LIMIT, encoding="utf-8")
    assert "♞" * identity.TALK_INSTRUCTIONS_LIMIT in identity.build_instructions({})

    def denied(*args, **kwargs):
        raise PermissionError("unreadable deployment instructions")

    monkeypatch.setattr(type(path), "open", denied)
    with pytest.raises(PermissionError, match="unreadable deployment instructions"):
        identity.build_instructions({})


@pytest.mark.parametrize("valid", [True, False])
def test_hybrid_session_combines_preferences_or_refuses_before_provider(
    monkeypatch, tmp_path, valid,
):
    import asyncio
    from types import SimpleNamespace

    from test_fake_provider_session import Audio, FakeProviderSession
    from test_hybrid_session import binding

    import talk_cli
    import talk_delegated

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(talk_cli.talk_transcript, "sweep_transcripts", lambda _home: None)
    custom = "Speak French briefly; do not repeat clear requests."
    identity.TALK_INSTRUCTIONS_PATH.write_text(
        custom if valid else "x" * (identity.TALK_INSTRUCTIONS_LIMIT + 1), encoding="utf-8",
    )
    host = SimpleNamespace(
        resolve_auth=lambda: SimpleNamespace(token="token", source="fake-auth"),
        identity_sections=lambda: {"USER": "Development operator"},
    )
    monkeypatch.setattr(talk_cli.talk_host, "host", lambda: host)
    monkeypatch.setattr(talk_cli.talk_apiserver, "warm_in_background", lambda: None)
    monkeypatch.setattr(talk_cli.talk_lifecycle, "attach_session", lambda *_: None)
    monkeypatch.setattr(talk_cli.talk_lifecycle, "detach_session", lambda: None)
    monkeypatch.setattr(talk_delegated.ConversationTail, "read", lambda _tail: [])
    ctx = SimpleNamespace(conversation_tools=lambda **_: {"ok": True, "tools": []})
    fake, audio, factories = FakeProviderSession(), Audio(), []
    audio.discord_speaker_authorization = True
    result = asyncio.run(talk_cli.run_talk_session(
        audio=audio, delegated_binding=binding(ctx), lane="discord",
        session_factory=lambda auth: factories.append(auth) or fake,
    ))
    if valid:
        assert result == 0 and len(factories) == 1
        prompt = fake.setup.instructions
        assert custom in prompt and "Development operator" in prompt
        assert prompt.startswith(identity.HYBRID_PREAMBLE)
        assert "control_task" in prompt and "delegation_control" in prompt
        assert "permissions and approvals still apply" in prompt
        assert "Codex" in prompt and "not proof" in prompt
    else:
        assert result != 0 and factories == [] and fake.setup is None

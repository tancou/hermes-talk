"""Optional mirror reads against the real host's ownership and transcript repositories."""

import os

import pytest

from talk_delegated import Binding
from talk_discord_threads import ChildTranscript

pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_AGENT_REPO"), reason="requires a Hermes Agent checkout",
)


def test_child_transcript_is_profile_pinned_owned_incremental_and_redacted(tmp_path, monkeypatch):
    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession, SessionLineageKind
    from persistence_assembly import open_persistence, profile_scope

    home, other = tmp_path / "alpha", tmp_path / "beta"
    for directory in (home, other):
        directory.mkdir()
        (directory / "config.yaml").write_text("persistence:\n  provider: sqlite\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    captured = Binding(None, "route", "parent", home, "42", "123", guild_id=7, hybrid=True)
    transcript = ChildTranscript(captured, "child-id")
    with open_persistence(profile_scope(home=home)) as store:
        for sid in ("parent", "other-parent"):
            store.sessions.create(NewSession(sid, source="discord"))
        # The newest matching subagent ID is unrelated; it must not win the lookup.
        for sid, parent in (("child", "parent"), ("foreign-child", "other-parent")):
            store.sessions.create(NewSession(
                sid, source="subagent", parent_session_id=parent,
                lineage_kind=SessionLineageKind.DELEGATE, model_config={"_subagent_id": "child-id"},
            ))
        store.messages.append("foreign-child", NewMessage("assistant", "FOREIGN CONTENT"))
        store.messages.append("child", NewMessage("system", "PRIVATE SYSTEM"))
        store.messages.append("child", NewMessage("user", "PRIVATE INJECTED CONTEXT"))
        store.messages.append("child", NewMessage(
            "assistant", "Running the test", reasoning_content="PRIVATE REASONING",
            tool_calls=({"id": "call", "type": "function", "function": {
                "name": "terminal", "arguments": '{"command":"pytest"}',
            }},),
        ))
        store.messages.append("child", NewMessage(
            "tool", '{"api_key":"sk-abcdefghijklmnopqrstuvwxyz1234567890"}',
            tool_call_id="call", tool_name="terminal",
        ))
        monkeypatch.setenv("HERMES_HOME", str(other))
        rows, ended = transcript.read()
        text = "\n".join(rows)
        assert transcript.session_id == "child" and ended is None
        assert "Running the test" in text and "terminal" in text
        assert "PRIVATE" not in text and "FOREIGN" not in text
        assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in text
        assert transcript.read() == ([], None)
        store.messages.append("child", NewMessage("assistant", "Tests passed"))
        store.sessions.end("child", "agent_close")
        assert transcript.read() == (["Assistant:\nTests passed"], None)
        assert transcript.read() == ([], "agent_close")

        foreign = ChildTranscript(captured, "missing-child")
        with pytest.raises(LookupError):
            foreign.read()

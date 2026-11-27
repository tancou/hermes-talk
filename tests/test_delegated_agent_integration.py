"""Optional real-storage coverage with HERMES_AGENT_REPO on PYTHONPATH."""
import os
from contextlib import ExitStack
from pathlib import Path

import pytest

from talk_delegated import Binding, ConversationTail

pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_AGENT_REPO"),
    reason="requires a Hermes Agent checkout with the public persistence API",
)


def test_context_is_bounded_profile_pinned_and_accepts_appends(tmp_path, monkeypatch):
    import persistence_assembly
    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    assert Path(persistence_assembly.__file__).resolve().is_relative_to(
        Path(os.environ["HERMES_AGENT_REPO"]).resolve()
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    for home in (alpha, beta):
        home.mkdir()
        (home / "config.yaml").write_text("persistence:\n  provider: sqlite\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(alpha))
    with ExitStack() as stack:
        writers = [stack.enter_context(open_persistence(profile_scope(home=home)))
                   for home in (alpha, beta)]
        for name, writer in zip(("alpha", "beta"), writers, strict=True):
            writer.sessions.create(NewSession("same-id", source="discord"))
            for index in range(24):
                writer.messages.append("same-id", NewMessage("user", f"{name} {index}"))
        tails = [ConversationTail(Binding(None, "route", "same-id", home, "42", "42"))
                 for home in (alpha, beta)]
        assert [row["content"] for row in tails[0].read()] == [f"alpha {i}" for i in range(8, 24)]
        monkeypatch.setenv("HERMES_HOME", str(beta))
        assert [row["content"] for row in tails[1].read()] == [f"beta {i}" for i in range(8, 24)]
        assert tails[0].read() == []
        writers[0].messages.append("same-id", NewMessage("assistant", "Exact profile update"))
        assert [row["content"] for row in tails[0].read()] == ["Exact profile update"]
        assert tails[1].read() == []


def test_context_refreshes_in_place_updates_without_closing_binding(tmp_path, monkeypatch):
    import asyncio
    import sqlite3

    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    from talk_delegated import SubmissionRelay

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("persistence:\n  provider: sqlite\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    relay = SubmissionRelay(Binding(None, "route", "parent", home, "42", "42"), lambda *_: None)
    with open_persistence(profile_scope(home=home)) as store:
        store.sessions.create(NewSession("parent", source="discord"))
        store.messages.append("parent", NewMessage("user", "Original"))

        async def run():
            await relay.context_commands()
            store.messages.append("parent", NewMessage("user", "New request"))
            # Native turns expand API context in place, also advancing the global revision.
            with sqlite3.connect(home / "state.db") as db:
                db.execute("UPDATE messages SET api_content = ? WHERE content = ?",
                           ("Expanded context", "New request"))
            await relay.context_commands()
            assert [row["content"] for row in relay.context_rows] == ["Original", "New request"]
            with sqlite3.connect(home / "state.db") as db:
                db.execute("UPDATE messages SET content = ? WHERE content = ?",
                           ("Corrected", "Original"))
            await relay.context_commands()
            assert [row["content"] for row in relay.context_rows] == ["Corrected", "New request"]
            assert not relay.closed
            with sqlite3.connect(home / "state.db") as db:
                db.execute("UPDATE messages SET active = 0 WHERE content = ?", ("Corrected",))
            with pytest.raises(RuntimeError, match="context changed"):
                await relay.context_commands()
        try:
            asyncio.run(run())
        finally:
            relay.close()


def test_concurrent_database_write_defers_context_without_losing_rows(tmp_path, monkeypatch):
    import asyncio
    from contextlib import contextmanager

    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    from talk_delegated import SubmissionRelay

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("persistence:\n  provider: sqlite\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    relay = SubmissionRelay(Binding(None, "route", "parent", home, "42", "42"), lambda *_: None)
    with open_persistence(profile_scope(home=home)) as store:
        for sid in ("parent", "other"):
            store.sessions.create(NewSession(sid, source="discord"))
        store.messages.append("parent", NewMessage("user", "Original"))
        dirty = False
        original = type(store.messages).read_snapshot

        @contextmanager
        def snapshot(messages):
            with original(messages) as check:
                def verify():
                    if dirty:
                        store.messages.append("other", NewMessage("assistant", "Concurrent write"))
                    return check()
                yield verify

        monkeypatch.setattr(type(store.messages), "read_snapshot", snapshot)

        async def run():
            nonlocal dirty
            await relay.context_commands()
            before = (relay.tail.cursor, relay.tail.cursor_state, relay.tail.revision)
            store.messages.append("parent", NewMessage("assistant", "Tool finished"))
            dirty = True
            for _ in range(3):
                assert await relay.context_commands() == []
                assert not relay.closed
                assert (relay.tail.cursor, relay.tail.cursor_state, relay.tail.revision) == before
                assert [row["content"] for row in relay.context_rows] == ["Original"]
            dirty = False
            assert await relay.context_commands()
            assert [row["content"] for row in relay.context_rows] == ["Original", "Tool finished"]
            assert await relay.context_commands() == []
        try:
            asyncio.run(run())
        finally:
            relay.close()

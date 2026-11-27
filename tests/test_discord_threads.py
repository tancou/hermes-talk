"""Discord follow-ups observe owned children; they never execute or control them."""

import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import talk_discord_threads as threads
from talk_delegated import Binding
from talk_hybrid import HybridRelay


@pytest.fixture
def redact(monkeypatch):
    module = ModuleType("tools.delegation_redaction")
    calls = []

    def scrub(text, *, force):
        calls.append((text, force))
        return text.replace("private-token", "[redacted]")

    module.redact_child_text = scrub
    monkeypatch.setitem(sys.modules, "tools.delegation_redaction", module)
    return calls


def binding(ctx=None):
    return Binding(ctx, "route", "parent", Path("/profile"), "42", "123", guild_id=7, hybrid=True)


def row(role="assistant", content="Hello", **kwargs):
    return SimpleNamespace(role=role, content=content, compressed_summary=False,
                           tool_calls=None, **kwargs)


def test_render_redacts_before_truncating_and_excludes_private_fields(redact):
    item = row(content="private-token @everyone " + "x" * 3000,
               reasoning_content="private reasoning")
    text = threads._render(item)
    assert "private-token" not in text and "@everyone" not in text
    assert "private reasoning" not in text and "abridged" in text and len(text) <= 1700
    assert "private-token" in redact[0][0] and redact[0][1] is True
    assert threads._render(row(role="system")) == ""
    assert threads._render(row(role="user")) == ""
    assert threads._render(row(content=[{"type": "thinking", "text": "hidden"}])) == ""
    item = row(content=None)
    item.tool_calls = [{"function": {"name": "terminal", "arguments": "private-token",
                                     "reasoning": "hidden"}}]
    text = threads._render(item)
    assert "terminal" in text and "[redacted]" in text and "hidden" not in text


def wire_transport(monkeypatch):
    sent, created = [], []
    member, bot = object(), object()
    permissions = SimpleNamespace(view_channel=True, send_messages=True,
                                  create_public_threads=True, send_messages_in_threads=True)
    guild = SimpleNamespace(id=7, me=bot, get_member=lambda _: member)

    class TextChannel:
        def __init__(self):
            self.guild = guild

        def permissions_for(self, who):
            return permissions

        def get_partial_message(self, message_id):
            async def create_thread(**kwargs):
                created.append((message_id, kwargs))
                return SimpleNamespace(id=456, parent_id=123, guild=guild)

            return SimpleNamespace(create_thread=create_thread)

    discord = ModuleType("discord")
    discord.TextChannel = TextChannel
    monkeypatch.setitem(sys.modules, "discord", discord)
    parent = TextChannel()

    async def send(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return SimpleNamespace(success=True, message_id="789")

    adapter = SimpleNamespace(_client=SimpleNamespace(get_channel=lambda _: parent), send=send)
    return adapter, sent, created, parent, permissions


def transcript_sequence(monkeypatch, batches):
    iterator = iter(batches)

    class Transcript:
        session_id = "child-session"

        def __init__(self, captured, child):
            assert captured.session_id == "parent" and child == "child-1"

        def read(self):
            value = next(iterator)
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(threads, "ChildTranscript", Transcript)
    monkeypatch.setattr(threads, "POLL_SECONDS", 0)


def launch():
    return {"status": "dispatched", "mode": "background", "subagent_ids": ["child-1"]}


def test_thread_streams_then_ends_and_contains_no_control_handler(monkeypatch, redact):
    adapter, sent, created, _, _ = wire_transport(monkeypatch)
    transcript_sequence(monkeypatch, [
        LookupError(), threads.TranscriptReadPending(),
        (["Assistant: Working", "Tool result: OK"], None), threads.TranscriptReadPending(),
        (["Assistant: Final result"], None), ([], "agent_close"),
    ])

    async def run():
        receipt = threads.start_followup(adapter, binding(), launch(), "Run tests @everyone")
        assert receipt == {"status": "starting"}
        await asyncio.gather(*threads._TASKS)
        await asyncio.sleep(0)
        assert not threads._TASKS
        assert receipt["status"] == "ended"
        assert receipt["url"] == "https://discord.com/channels/7/456"

    asyncio.run(run())
    assert len(created) == 1 and created[0][0] == 789
    assert [chat for chat, *_ in sent] == ["123", "456", "456", "456", "456", "456"]
    assert "Replies here are not forwarded" in sent[1][1]
    assert "Final result" in sent[-2][1] and "agent_close" in sent[-1][1]
    assert all("@everyone" not in text for _, text, _ in sent)
    assert all(kw == {"metadata": {"non_conversational": True}} for _, _, kw in sent)


def test_failed_send_is_not_retried_and_does_not_restart_work(monkeypatch, redact):
    adapter, sent, created, _, _ = wire_transport(monkeypatch)
    transcript_sequence(monkeypatch, [(["Assistant: Working"], None)])
    send = adapter.send

    async def fail(chat_id, text, **kwargs):
        result = await send(chat_id, text, **kwargs)
        if text == "Assistant: Working":
            result.success = False
        return result

    adapter.send = fail

    async def run():
        receipt = threads.start_followup(adapter, binding(), launch(), "Run tests")
        await asyncio.gather(*threads._TASKS)
        assert receipt["status"] == "stopped"

    asyncio.run(run())
    assert len(created) == 1
    assert sum(text == "Assistant: Working" for _, text, _ in sent) == 1
    assert "does not stop the delegated work" in sent[-1][1]


def test_visibility_revocation_stops_an_existing_mirror(monkeypatch, redact):
    adapter, sent, _, _, permissions = wire_transport(monkeypatch)
    transcript_sequence(monkeypatch, [(["Assistant: Must not be sent"], None)])
    send = adapter.send

    async def revoke_after_header(chat_id, text, **kwargs):
        result = await send(chat_id, text, **kwargs)
        if chat_id == "456":
            permissions.view_channel = False
        return result

    adapter.send = revoke_after_header

    async def run():
        receipt = threads.start_followup(adapter, binding(), launch(), "Run tests")
        await asyncio.gather(*threads._TASKS)
        assert receipt["status"] == "stopped"

    asyncio.run(run())
    assert len(sent) == 2
    assert all("Must not be sent" not in text for _, text, _ in sent)


def test_failed_thread_creation_is_not_retried(monkeypatch, redact):
    adapter, sent, _, parent, _ = wire_transport(monkeypatch)
    transcript_sequence(monkeypatch, [([], "agent_close")])
    attempts = []

    async def fail(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("private Discord response")

    parent.get_partial_message = lambda _: SimpleNamespace(create_thread=fail)

    async def run():
        receipt = threads.start_followup(adapter, binding(), launch(), "Run tests")
        await asyncio.gather(*threads._TASKS)
        assert receipt["status"] == "unavailable" and "url" not in receipt

    asyncio.run(run())
    assert len(attempts) == 1
    assert all("private Discord response" not in text for _, text, _ in sent)


def test_follower_limit_does_not_schedule_more_work(monkeypatch):
    monkeypatch.setattr(threads, "MAX_FOLLOWERS", 0)
    assert threads.start_followup(None, binding(), launch(), "task") == {"status": "unavailable"}


@pytest.mark.parametrize("change", ["guild", "channel_type", "permission"])
def test_parent_scope_and_permissions_fail_before_publication(monkeypatch, redact, change):
    adapter, sent, created, parent, permissions = wire_transport(monkeypatch)
    transcript_sequence(monkeypatch, [([], "agent_close")])
    if change == "guild":
        parent.guild.id = 8
    elif change == "channel_type":
        adapter._client.get_channel = lambda _: object()
    else:
        permissions.view_channel = False

    async def run():
        receipt = threads.start_followup(adapter, binding(), launch(), "Run tests")
        await asyncio.gather(*threads._TASKS)
        assert receipt["status"] == "unavailable"

    asyncio.run(run())
    assert not sent and not created


@pytest.mark.parametrize("payload", [None, {}, {"status": "refused"},
                                    {**launch(), "subagent_ids": []},
                                    {**launch(), "subagent_ids": ["a", "b"]}])
def test_unconfirmed_or_ambiguous_launch_does_not_schedule(payload):
    assert threads.start_followup(None, binding(), payload, "task") == {"status": "unavailable"}


def test_followup_starts_once_after_delayed_exact_receipt_and_survives_relay_close():
    observed = []
    responses = [
        {"ok": True, "status": "queued"},
        {"ok": True, "status": "terminal", "outputs": [
            {"call_id": "call", "output": json.dumps(launch())},
        ]},
    ]

    def execute(**kwargs):
        return responses.pop(0) if len(responses) > 1 else responses[0]

    receipt = {"status": "starting"}

    def follow(captured, payload, task):
        observed.append((captured, payload, task))
        return receipt

    async def run():
        voice = HybridRelay(binding(SimpleNamespace(execute_tools=execute)), lambda *_: None,
                            [{"name": "delegate_task"}], follow_delegation=follow)
        event = {"name": "delegate_work", "call_id": "call", "arguments": '{"task":"Run tests"}'}
        await voice.handle_tool_call_async(event)
        assert not observed
        request_id = next(iter(voice.direct_requests))
        result = await voice.status(request_id)
        assert result["thread_followup"] is receipt
        receipt.update(status="active", thread_id="456")
        result = await voice.status(request_id)
        assert result["thread_followup"]["thread_id"] == "456"
        assert len(observed) == 1 and observed[0][2] == "Run tests"
        voice.close()
        assert receipt["status"] == "active"

    asyncio.run(run())


def test_transcript_requires_owned_child_and_stops_on_rotation(monkeypatch, redact):
    @contextmanager
    def snapshot():
        yield lambda: True

    rows = [row(id=1)]
    owned_id = [None]
    messages = SimpleNamespace(
        read_snapshot=snapshot,
        conversation_revision=lambda _: SimpleNamespace(lineage_ids=("child",)),
        transcript_cursor_state=lambda *args: "stable",
        read_transcript=lambda *args: rows,
    )
    store = SimpleNamespace(messages=messages, sessions=SimpleNamespace(
        get=lambda _: SimpleNamespace(ended_at=None, end_reason=None),
    ))

    @contextmanager
    def reader(profile):
        assert profile == Path("/profile")
        yield store

    persistence = ModuleType("persistence_assembly")
    persistence.open_persistence_reader = reader
    message_module = ModuleType("persistence.conversation.messages")
    message_module.TranscriptRead = SimpleNamespace
    sessions = ModuleType("tools.delegate_tool_sessions")

    def owned(reader, child, ancestors):
        assert reader is store and child == "child-1" and ancestors == ["parent"]
        return owned_id[0]

    sessions.stored_child_session = owned
    monkeypatch.setitem(sys.modules, "persistence_assembly", persistence)
    monkeypatch.setitem(sys.modules, "persistence.conversation.messages", message_module)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_sessions", sessions)
    transcript = threads.ChildTranscript(binding(), "child-1")
    with pytest.raises(LookupError):
        transcript.read()
    owned_id[0] = "child"
    assert transcript.read() == (["Assistant:\nHello"], None)
    assert transcript.cursor == 1
    owned_id[0] = "unrelated-or-rotated"
    with pytest.raises(RuntimeError, match="rotated"):
        transcript.read()

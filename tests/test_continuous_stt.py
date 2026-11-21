"""Wire and host interoperability; run with the proposed Hermes core on PYTHONPATH."""

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

pytest.importorskip("agent.continuous_transcription")

from talk_continuous_stt import TalkContinuousSTT, run_discord_ingress
from talk_providers import OpenAITranscriptionProvider


class Wire:
    def __init__(self, events=(), **kwargs):
        self.items = iter(events)
        self.sent, self.configuration = [], None
        self.closed = False

    async def connect(self, **kwargs):
        self.configuration = kwargs

    async def send_json(self, items):
        self.sent.extend(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.items)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        self.closed = True


def acknowledged():
    return {"type": "session.updated", "session": {
        "tools": [], "audio": {"input": {"turn_detection": {"create_response": False}}}}}


def test_input_only_configuration_and_profile_context(monkeypatch):
    owner = ContextVar("test_voice_owner", default="wrong")
    wire = Wire([acknowledged(), {"type": "input_audio_buffer.committed", "item_id": "u1"}, {
        "type": "conversation.item.input_audio_transcription.completed",
        "item_id": "u1", "transcript": "check the build",
    }])
    seen = []

    def auth():
        seen.append(owner.get())
        return SimpleNamespace(token="fake", source="test")

    async def run():
        token = owner.set("profile-b")
        try:
            session = await TalkContinuousSTT.open(
                wire_factory=lambda **_: wire, auth_resolver=auth)
            await session.send_audio(b"\x01\x00")
            events = [event async for event in session.events()]
            assert [event.kind for event in events] == ["ready", "transcript"]
            assert events[1].text == "check the build" and events[1].final
            assert wire.configuration["automatic_response"] is False
            assert wire.configuration["session_update"]["session"]["tools"] == []
            assert wire.sent == [{"type": "input_audio_buffer.append", "audio": "AQA="}]
            assert seen == ["profile-b"]
            await session.close()
            assert wire.closed
            assert OpenAITranscriptionProvider().continuous_capable
        finally:
            owner.reset(token)
    asyncio.run(run())


@pytest.mark.parametrize("event", [
    {"type": "response.created"},
    {"type": "error", "error": {"message": "secret must not escape"}},
    {"type": "conversation.item.input_audio_transcription.failed"},
    {"type": "session.updated", "session": {"tools": [{"name": "terminal"}]}},
])
def test_provider_output_and_failed_configuration_never_become_user_input(event):
    async def run():
        session = TalkContinuousSTT(Wire([event]))
        with pytest.raises(RuntimeError) as exc:
            _ = [item async for item in session.events()]
        assert "secret" not in str(exc.value)
    asyncio.run(run())


def test_failed_connect_releases_wire():
    class FailedWire(Wire):
        async def connect(self, **kwargs):
            raise RuntimeError("connect refused")

    async def run():
        wire = FailedWire()
        with pytest.raises(RuntimeError, match="connect refused"):
            await TalkContinuousSTT.open(
                wire_factory=lambda **_: wire,
                auth_resolver=lambda: SimpleNamespace(token="fake", source="test"))
        assert wire.closed
    asyncio.run(run())


def test_final_transcripts_keep_audio_commit_order_and_ignore_replays():
    async def run():
        def commit(item):
            return {"type": "input_audio_buffer.committed", "item_id": item}

        def final(item, text):
            return {"type": "conversation.item.input_audio_transcription.completed",
                    "item_id": item, "transcript": text}

        session = TalkContinuousSTT(Wire([
            acknowledged(), commit("a"), commit("b"), final("b", "second"),
            final("a", "first"), final("a", "first"),
        ]))
        events = [event async for event in session.events()]
        assert [event.text for event in events if event.final] == ["first", "second"]
    asyncio.run(run())


def test_capture_pump_keeps_identity_bounds_and_closes_on_disconnect():
    calls, status = [], []
    packets = iter([
        SimpleNamespace(pcm=b"\x01\x00" * 24_001, speaker={"user_id": 42}),
        SimpleNamespace(pcm=b"\x00\x00", speaker=None),
        SimpleNamespace(pcm=b"\x02\x00", speaker=None),  # unknown speech is never silence
        SimpleNamespace(pcm=b"\x03\x00", speaker={"user_id": "42"}),
    ])

    class Attachment:
        closed = False

        def check(self):
            assert not self.closed

        async def feed_audio(self, pcm, *, speaker_user_id):
            calls.append((speaker_user_id, pcm))
            return "accepted"

        async def feed_synthesized_silence(self, pcm):
            calls.append((None, pcm))
            return "accepted"

        async def close(self):
            self.closed = True

    class Audio:
        stopped = False

        def start(self):
            calls.append("started")

        def read_input_packet(self):
            try:
                return next(packets)
            except StopIteration:
                raise EOFError("scripted disconnect") from None

        def stop(self):
            self.stopped = True

    async def run():
        attachment, audio = Attachment(), Audio()

        async def open_session(name):
            assert name == "talk_openai"
            return attachment

        with pytest.raises(EOFError, match="scripted disconnect"):
            await run_discord_ingress(SimpleNamespace(open=open_session),
                                      audio=audio, status_callback=status.append)
        assert attachment.closed and audio.stopped
        assert status == ["listening"]
        assert calls == ["started", (42, b"\x01\x00" * 24_000),
                         (42, b"\x01\x00"), (None, b"\x00\x00")]
    asyncio.run(run())

"""Actual Talk loop + canonical SQLite journal; only transport and delegation work are fake."""
import asyncio
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fake_provider_session import (
    Audio,
    FakeProviderSession,
    HostExecutionAttachment,
    _offline_policy,  # noqa: F401
)

import talk_cli
import talk_conversation_sync
import talk_realtime as rt


@pytest.mark.usefixtures('_offline_policy')
@pytest.mark.parametrize('during_speech', [False, True])
def test_delegation_completion_is_known_to_next_voice_turn(tmp_path, monkeypatch, during_speech):
    core = os.environ.get('HERMES_AGENT_REPO')
    if not core:
        pytest.skip('requires companion Hermes checkout via HERMES_AGENT_REPO')
    monkeypatch.syspath_prepend(core)
    from gateway.voice_conversation_feed import VoiceConversationFeed
    from hermes_state import SessionDB
    assert Path(sys.modules['gateway.voice_conversation_feed'].__file__).is_relative_to(Path(core))
    if not during_speech:
        monkeypatch.setattr(talk_conversation_sync, 'POLL_SECONDS', 3600)
    db = SessionDB(tmp_path / 'context.db')
    db.create_session('voice-parent', source='discord')
    db.create_session('someone-else', source='discord')
    entry = SimpleNamespace(session_id='voice-parent', session_key='owner-profile-room-thread')

    class Attachment(HostExecutionAttachment):
        realtime_execution_version = conversation_sync_version = 1
        operator_user_id = 42
        trusted_room = True

        def __init__(self):
            super().__init__()
            self.definitions[0]['name'] = 'delegate_task'
            self._turns = {}
            store = SimpleNamespace(_db_for_session_id=lambda _: db,
                                    peek_session_id=lambda _: entry.session_id)
            self.factory = SimpleNamespace(
                scope=nullcontext, runner=SimpleNamespace(session_store=store))
            self.feed = VoiceConversationFeed(self, entry)

        def admits_speaker(self, user):
            return user == 42

        def conversation_snapshot(self):
            return self.feed.snapshot()

        def initial_history(self):
            return []

        def read_conversation_updates(self):
            return self.feed.read()

        def acknowledge_conversation_updates(self, cursor):
            self.feed.acknowledge(cursor)

        def note_response(self, *args, **kwargs):
            pass

        async def execute_tool_batch(self, permits):
            self.executions.append(permits)
            # Native execution starts detached work; its eventual result is a
            # different committed event, not the immediate function output.
            return [{'call_id': 'delegation-call', 'output': 'Delegation started: delegation-42'}]

    attachment = Attachment()

    class RoomAudio(Audio):
        discord_speaker_authorization = True

        def __init__(self):
            super().__init__()
            self.recorded = asyncio.Event()

        def read_input_packet(self):
            if self.recorded.is_set():
                return None
            self.recorded.set()
            return SimpleNamespace(speaker={'user_id': 42}, pcm=bytes(80 * 24 * 2))

    class Provider(FakeProviderSession):
        def __init__(self, audio):
            super().__init__()
            self.audio = audio
            self.stage = 0
            self.requests = 0
            self.context_delivered = asyncio.Event()
            self.tool_returned = asyncio.Event()

        async def send(self, commands):
            await super().send(commands)
            for command in commands:
                if isinstance(command, rt.SubmitToolResult):
                    self.tool_returned.set()
                if isinstance(command, rt.ReplayConversationMessage):
                    assert 'PRIVATE FOREIGN RESULT' not in command.text
                    if 'Codex result-42' in command.text:
                        assert command.role is rt.TranscriptRole.ASSISTANT
                        self.context_delivered.set()
                if not isinstance(command, rt.StartResponse):
                    continue
                self.requests += 1
                response = f'response-{self.requests}'
                self.events.append(rt.ResponseStarted(response, metadata=command.metadata))
                if self.requests == 1:
                    self.events.append(rt.FunctionCall('delegation-call', 'delegate_task',
                        '{"value":1}', response_id=response, item_id='tool-item'))
                elif self.requests == 3:
                    assert self.context_delivered.is_set(), 'voice answered without the result'
                    replay = [c for b in self.sent for c in b
                              if isinstance(c, rt.ReplayConversationMessage)]
                    assert sum('Codex result-42' in c.text for c in replay) == 1
                    assert any('Discord text after connection' in c.text for c in replay)
                self.events.append(rt.ResponseFinished(response, status='completed'))

        async def __anext__(self):
            await asyncio.sleep(0)
            if self.events:
                return self.events.pop(0)
            if self.stage == 0:
                await self.audio.recorded.wait()
                self.stage = 1
                self.events.extend([rt.SpeechStopped('input-1', offset_ms=20),
                                    rt.InputAudioCommitted('input-1')])
                return rt.SpeechStarted('input-1', offset_ms=0)
            if self.requests < 2:
                # Wait for the real tool coordinator, without ending receive.
                await self.tool_returned.wait()
                if self.events:
                    return self.events.pop(0)
            if self.stage == 1:
                self.stage = 2
                return rt.SpeechStarted('input-2', offset_ms=20)
            if self.stage == 2:
                self.stage = 3
                db.append_message('voice-parent', 'user', 'Found Codex result-42',
                                  display_kind='async_delegation_complete')
                db.append_message('voice-parent', 'user', 'Discord text after connection')
                db.append_message('someone-else', 'user', 'PRIVATE FOREIGN RESULT')
                if during_speech:
                    await asyncio.wait_for(self.context_delivered.wait(), 5)
                    assert self.requests == 2, 'announcement interrupted user speech'
                return rt.SpeechStopped('input-2', offset_ms=40)
            if self.stage == 3:
                self.stage = 4
                return rt.InputAudioCommitted('input-2')
            return await super().__anext__()

    async def run():
        audio = RoomAudio()
        provider = Provider(audio)
        result = await asyncio.wait_for(talk_cli.run_talk_session(
            audio=audio, session_factory=lambda _: provider,
            host_execution_attachment=attachment), 10)
        assert result == 0 and provider.requests == 3
        assert len(attachment.executions) == 1, 'the completed search must not be delegated again'
        assert provider.closed and attachment.closed and audio.stopped
    try:
        asyncio.run(run())
    finally:
        db.close()

"""A completed result is context before the next answer, even while speech is busy."""
import asyncio
from copy import deepcopy

import pytest

import talk_conversation_sync as sync
import talk_realtime as rt


def message(number=1, *, session='owned', origin='internal', content='Found Codex: result-42'):
    return dict(message_id=number, event_id=str(number), session_id=session,
                role='assistant' if origin == 'internal' else origin,
                origin=origin, content=content)


class Attachment:
    conversation_sync_version = 1

    def __init__(self):
        self.messages = []
        self.cursor = 0
        self.acks = []

    def conversation_snapshot(self):
        return dict(session_id='owned', cursor=0, messages=[], has_more=False)

    def read_conversation_updates(self):
        pending = [m for m in self.messages if m['message_id'] > self.cursor]
        return dict(session_id='owned',
                    cursor=pending[-1]['message_id'] if pending else self.cursor,
                    messages=deepcopy(pending), has_more=False)

    def acknowledge_conversation_updates(self, cursor):
        self.acks.append(cursor)
        self.cursor = cursor


def test_completed_result_reaches_context_during_user_speech_without_announcing():
    async def run():
        attachment = Attachment()
        reader = sync.ConversationSync(attachment)
        sent = []

        async def send(commands):
            sent.extend(commands)

        await reader.start(send)
        reader.observe(rt.SpeechStarted(input_id='next'))
        attachment.messages.append(message())
        await reader.flush(send)
        assert 'result-42' in sent[0].text
        assert sent[0].role is rt.TranscriptRole.ASSISTANT
        assert not reader.announcement()
        reader.observe(rt.SpeechStopped(input_id='next'))
        assert not reader.announcement(), 'the pending user answer owns the next speaking slot'
        await reader.flush(send)
        assert len(sent) == 1
        reader.response_sent((rt.StartResponse(),))
        assert not reader.announcement(), 'do not repeat the update behind the user answer'
    asyncio.run(run())


def test_failed_context_send_is_not_acknowledged_and_reconnect_replays_same_identity():
    async def run():
        attachment = Attachment()
        reader = sync.ConversationSync(attachment)

        async def discard(_commands):
            pass

        await reader.start(discard)
        attachment.messages.append(message())
        attempted = []

        async def fail(commands):
            attempted.extend(commands)
            raise ConnectionError('socket gone before delivery')

        with pytest.raises(ConnectionError):
            await reader.flush(fail)
        assert attachment.cursor == 0
        replacement = sync.ConversationSync(attachment)
        received = []

        async def receive(commands):
            received.extend(commands)

        await replacement.start(receive)
        await replacement.flush(receive)
        assert received == attempted
        assert attachment.cursor == 1
    asyncio.run(run())


def test_context_is_committed_before_next_response_even_without_poll_tick():
    async def run():
        attachment = Attachment()
        reader = sync.ConversationSync(attachment)
        sent = []

        async def send(commands):
            for command in commands:
                if isinstance(command, rt.StartResponse):
                    assert any('result-42' in c.text for c in sent
                               if isinstance(c, rt.ReplayConversationMessage))
                sent.append(command)

        await reader.start(send)
        attachment.messages.append(message())
        await reader.flush(send)
        await send((rt.StartResponse(),))
        assert [type(c) for c in sent] == [rt.ReplayConversationMessage, rt.StartResponse]
    asyncio.run(run())


def test_foreign_conversation_is_rejected_before_provider_write():
    async def run():
        attachment = Attachment()
        reader = sync.ConversationSync(attachment)
        reader.session_id = 'foreign'
        attachment.messages.append(message())

        async def forbidden(_commands):
            pytest.fail('foreign context reached the provider')

        with pytest.raises(PermissionError):
            await reader.flush(forbidden)
        assert attachment.cursor == 0
    asyncio.run(run())


def test_notification_requests_speech_only_after_context_and_tracks_audio_separately(caplog):
    async def run():
        attachment = Attachment()
        reader = sync.ConversationSync(attachment)

        async def send(_commands):
            pass

        await reader.start(send)
        attachment.messages.append(message())
        await reader.flush(send)
        announcement = reader.announcement()
        assert len(announcement) == 1 and announcement[0].allow_tools is False
        reader.response_sent(announcement)
        reader.observe(rt.ResponseStarted('notice', metadata=announcement[0].metadata))
        reader.observe(rt.OutputAudio(b'pcm', response_id='notice'))
        reader.observe(rt.ResponseFinished('notice', status='completed'))
        assert 'playback completed' not in caplog.text
        reader.playback_idle()
        assert 'playback completed' in caplog.text
        assert not reader.announcement()
    with caplog.at_level('INFO'):
        asyncio.run(run())


def test_large_internal_results_are_bounded_data_chunks_with_stable_ids():
    result = '\x00"\\' * 12000
    commands = sync.commands_for([message(content=result)])
    assert len(commands) > 1
    assert all(len(c.text) <= 32000 for c in commands)
    assert len({c.item_id for c in commands}) == len(commands)
    assert all(len(c.item_id) <= 32 for c in commands)
    assert commands == sync.commands_for([message(content=result)])
    assert commands[0].item_id != sync.commands_for([message(number=2)])[0].item_id
    assert commands[0].item_id != sync.commands_for([message(session='other')])[0].item_id


def test_provider_context_rejection_is_terminal_but_unrelated_errors_remain_recoverable():
    from talk_openai_realtime import decode_event, encode_command
    command = sync.commands_for([message()])[0]
    request = encode_command(command)
    reader = sync.ConversationSync(Attachment())
    reader.observe(rt.ProviderFailure('unrelated recoverable tool error'))
    rejection = decode_event({'type': 'error', 'error': {
        'message': 'invalid conversation item', 'event_id': request['event_id']}})
    with pytest.raises(rt.RealtimeSessionError, match='rejected canonical.*invalid conversation item'):
        reader.observe(rejection)


def test_interrupted_announcement_is_not_reported_as_played(caplog):
    reader = sync.ConversationSync(Attachment())
    reader.observe(rt.ResponseStarted('notice', metadata={'hermes_context_cursor': '9'}))
    reader.observe(rt.OutputAudio(b'first', response_id='notice'))
    reader.observe(rt.SpeechStarted(input_id='interrupt'))
    reader.observe(rt.OutputAudio(b'late tail', response_id='notice'))
    reader.observe(rt.ResponseFinished('notice', status='completed'))
    with caplog.at_level('INFO'):
        reader.playback_idle()
    assert 'playback completed' not in caplog.text

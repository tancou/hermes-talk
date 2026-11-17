"""Canonical capture/playback must use the receiving bot, including routed profiles."""
import asyncio
import importlib
from types import SimpleNamespace as NS

import pytest
from test_discord import _FakeConnection, _FakeReceiver, _FakeVoiceClient
from test_register import load_plugin


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("stt", [False, True])
def test_canonical_audio_uses_attachment_connection(monkeypatch, trusted, stt):
    plugin = load_plugin()
    discord = plugin.talk_discord
    connection = _FakeConnection()
    voice = _FakeVoiceClient(connection)
    receiver = _FakeReceiver(voice)
    connection.add_socket_listener(receiver._on_packet)
    voice.channel = NS(id=90)
    adapter = NS(_voice_clients={7: voice}, _voice_receivers={7: receiver})
    other_voice = _FakeVoiceClient(_FakeConnection())
    other_voice.channel = NS(id=91)
    other_receiver = _FakeReceiver(other_voice)
    other_voice._connection.add_socket_listener(other_receiver._on_packet)
    other = NS(guild_id=7, voice_client=other_voice, receiver=other_receiver, adapter=NS())

    def check():
        if adapter._voice_clients[7] is not voice or voice.channel.id != 90:
            raise RuntimeError("connection changed")

    factory = NS(adapter=adapter, voice_client=voice, guild_id=7, channel_id=90,
                 check_binding=check, transcription_ingress_version=1, operator_only=True)
    attachment = NS(factory=factory, realtime_execution_version=1, trusted_room=trusted,
                    close=lambda: None)
    monkeypatch.setattr(discord, "resolve_voice_bridge", lambda *_: vars(other))
    entered = asyncio.Event()
    stop = asyncio.Event()

    async def session(*args, audio, **kwargs):
        try:
            audio.start()
            assert audio._bridge["adapter"] is adapter
            assert audio._bridge["voice_client"] is voice
            assert audio._bridge["receiver"] is receiver
            if not stt:
                audio.queue_playback(b"\x01\x00" * 480)
                assert voice.playing is not None
                assert other_voice.playing is None
            adapter._voice_clients[7] = other_voice
            with pytest.raises(discord.talk_audio.TalkAudioError):
                audio.read_input_packet()
            entered.set()
            await stop.wait()
        finally:
            audio.stop()

    monkeypatch.setattr(plugin.talk_cli, "run_talk_session", session)
    core = importlib.import_module(f"{plugin.__name__}.talk_core_session")
    monkeypatch.setattr(core, "run_core_session", session)

    async def run():
        discord.reset_for_tests()
        try:
            if stt:
                discord.start_core_session(factory)
            else:
                discord.start_session(host_execution_attachment=attachment)
            task = discord._SESSION["task"]
            # Await either startup or its failure, without stranding a test task.
            ready = asyncio.create_task(entered.wait())
            try:
                done, _ = await asyncio.wait((ready, task), timeout=2,
                                             return_when=asyncio.FIRST_COMPLETED)
                assert done
                if task in done:
                    await task
                assert entered.is_set()
            finally:
                stop.set()
                ready.cancel()
                await asyncio.gather(task, ready, return_exceptions=True)
        finally:
            discord.reset_for_tests()
    asyncio.run(run())


def test_refused_join_closes_only_the_new_attachment(monkeypatch):
    plugin = load_plugin()
    discord = plugin.talk_discord

    class Attachment:
        trusted_room = True
        closed = False

        def close(self):
            self.closed = True

    attachment = Attachment()

    class Invocation:
        async def capture_realtime_execution_attachment(self):
            return attachment

    async def run():
        existing = asyncio.create_task(asyncio.Event().wait())
        discord._SESSION.update(task=existing, mode="provider-host-tools",
                                speaker_policy="operator only", generation=1)
        before = dict(discord._SESSION)
        monkeypatch.setattr(discord, "start_session", lambda **_: "already live")
        try:
            result = await plugin._start_canonical_duplex(Invocation())
            assert result == "already live"
            assert attachment.closed
            assert before == discord._SESSION
        finally:
            existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)
            discord.reset_for_tests()
    asyncio.run(run())


def test_expired_stt_binding_returns_a_startup_refusal():
    plugin = load_plugin()

    def expired():
        raise RuntimeError("synthetic revoked connection")

    async def run():
        plugin.talk_discord.reset_for_tests()
        factory = NS(transcription_ingress_version=1, check_binding=expired)
        result = plugin.talk_discord.start_core_session(factory)
        assert "captured Discord voice connection changed" in result
        assert not plugin.talk_discord._SESSION
    asyncio.run(run())

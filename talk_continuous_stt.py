"""Continuous input-only STT for Hermes's ordinary Discord agent pipeline."""

from __future__ import annotations

import asyncio
import base64
from collections import deque

from agent.continuous_transcription import ContinuousTranscriptionSession, TranscriptionEvent

try:
    from . import talk_auth, talk_config, talk_wire
    from .talk_openai_realtime import _OpenAIWireSession
except ImportError:  # flat plugin installation
    import talk_auth
    import talk_config
    import talk_wire
    from talk_openai_realtime import _OpenAIWireSession


class TalkContinuousSTT(ContinuousTranscriptionSession):
    """One speaker per socket; the host alone associates transcripts with users."""

    def __init__(self, wire):
        self.wire = wire
        self._order = deque()
        self._committed = set()
        self._finals = {}

    def _commit(self, item_id):
        if not isinstance(item_id, str) or not item_id or len(item_id) > 512:
            raise ValueError("Invalid committed audio item")
        if item_id in self._committed:
            return
        if len(self._committed) >= 4096:
            raise RuntimeError("Transcription item capacity reached; rejoin to reset")
        self._committed.add(item_id)
        self._order.append(item_id)

    def _complete(self, event):
        item = TranscriptionEvent("transcript", event.get("item_id"),
                                  event.get("transcript"), final=True)
        if item.item_id not in self._committed:
            raise RuntimeError("Transcript has no committed input audio")
        previous = self._finals.get(item.item_id)
        if previous is not None and previous != item:
            raise RuntimeError("Conflicting final transcription")
        self._finals[item.item_id] = item
        # Transcription completions may arrive out of order. Preserve the
        # provider's audio commit order without executing partial transcripts.
        while self._order and self._order[0] in self._finals:
            yield self._finals[self._order.popleft()]

    @classmethod
    async def open(cls, *, wire_factory=_OpenAIWireSession, auth_resolver=talk_auth.resolve_auth):
        # Token minting is synchronous in the existing wire. Keep it off the
        # gateway loop while preserving the profile's credential ContextVars.
        auth = await asyncio.to_thread(auth_resolver)
        wire = wire_factory(auth_token=auth.token, auth_source=auth.source)
        model, voice = talk_config.talk_model(), talk_config.talk_voice()
        detection = talk_config.turn_detection("openai")
        payload = talk_wire.build_session_payload(
            model=model, voice=voice, instructions="", tools=None,
            automatic_response=False, turn_detection=detection)
        payload.pop("model", None)
        payload["tools"] = []
        try:
            await wire.connect(
                model=model, voice=voice, instructions="", tools=None,
                automatic_response=False, turn_detection=detection,
                session_update={"type": "session.update", "session": payload})
        except BaseException:
            await wire.close()
            raise
        return cls(wire)

    async def send_audio(self, pcm):
        if type(pcm) is not bytes or not pcm or len(pcm) % 2 or len(pcm) > 48_000:
            raise ValueError("Expected bounded PCM16LE audio")
        await self.wire.send_json(({
            "type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii"),
        },))

    async def events(self):
        ready = False
        async for event in self.wire:
            kind = event.get("type")
            if kind == "session.updated":
                session = event.get("session", {})
                detection = session.get("audio", {}).get("input", {}).get("turn_detection", {})
                if detection.get("create_response") is not False or session.get("tools") != []:
                    raise RuntimeError("Provider did not acknowledge input-only transcription")
                if not ready:
                    ready = True
                    yield TranscriptionEvent("ready")
            elif kind == "input_audio_buffer.committed":
                self._commit(event.get("item_id"))
            elif kind == "conversation.item.input_audio_transcription.completed":
                if not ready:
                    raise RuntimeError("Transcript arrived before configuration acknowledgement")
                for item in self._complete(event):
                    yield item
            elif kind == "input_audio_buffer.speech_started":
                yield TranscriptionEvent("speech_started", event.get("item_id"))
            elif kind in {"error", "conversation.item.input_audio_transcription.failed"}:
                raise RuntimeError("Continuous transcription provider failed")
            elif isinstance(kind, str) and (
                kind.startswith("response.") or "function_call" in kind
            ):
                raise RuntimeError("Unexpected assistant output on transcription-only session")
            elif kind in {"conversation.item.created", "conversation.item.done"}:
                item = event.get("item", {})
                if item.get("role") == "assistant" or item.get("type") in {
                    "function_call", "function_call_output",
                }:
                    raise RuntimeError("Unexpected assistant item on transcription-only session")

    async def close(self):
        await self.wire.close()


async def run_discord_ingress(factory, *, audio, status_callback=None):
    """Capture PCM only; Hermes keeps its normal playback and agent execution."""
    attachment = None
    try:
        attachment = await factory.open("talk_openai")
        audio.start()
        if status_callback:
            status_callback("listening")
        while True:
            attachment.check()
            packet = await asyncio.to_thread(audio.read_input_packet)
            if packet is None:
                await asyncio.sleep(0.01)
                continue
            pcm, speaker = packet.pcm, packet.speaker
            if type(pcm) is not bytes or not pcm or len(pcm) % 2:
                continue
            if speaker is None:
                if not any(pcm):
                    for offset in range(0, len(pcm), 48_000):
                        await attachment.feed_synthesized_silence(pcm[offset:offset + 48_000])
            elif type(speaker) is dict and type(speaker.get("user_id")) is int:
                for offset in range(0, len(pcm), 48_000):
                    await attachment.feed_audio(pcm[offset:offset + 48_000],
                                                speaker_user_id=speaker["user_id"])
    finally:
        try:
            audio.stop()
        finally:
            if attachment is not None:
                await attachment.close()

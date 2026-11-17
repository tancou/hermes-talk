"""Canonical tools retain authenticated speakers, duplex audio and native history."""

import asyncio
from types import SimpleNamespace

from test_fake_provider_session import HostExecutionAttachment
from test_host_authority import _bind_response, _make_tool_event, _pcm, _speaker

import talk_cli
import talk_host_duplex
import talk_operator_auth
import talk_realtime as rt


class Attachment(HostExecutionAttachment):
    realtime_execution_version = 1

    def __init__(self):
        super().__init__()
        self.inputs, self.responses = [], []

    def admits_speaker(self, user):
        return user == 42

    def note_input(self, user, text, **kwargs):
        self.inputs.append((user, text))

    def note_response(self, response, user, text, **kwargs):
        self.responses.append((response, user, text, kwargs))


def test_canonical_tool_policy_runs_beyond_legacy_allowlist_with_same_speaker():
    attachment = Attachment()
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(
        canonical_speaker_allowed=attachment.admits_speaker
    )
    ledger.record_packet(_speaker(42), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger, tool_name="terminal", arguments='{"command":"pwd"}')
    relay = talk_cli.HostExecutionRelay(attachment, tool_authorizer=ledger.authorize_tool)
    result = asyncio.run(relay.handle_tool_batch_async((event,), "batch"))
    assert attachment.minted[0][0]["speaker_user_id"] == 42
    assert attachment.minted[0][0]["tool_name"] == "terminal"
    assert result[0][0].output == "exact host output for call-1"
    repeated = asyncio.run(relay.handle_tool_batch_async((event,), "replay"))
    assert len(attachment.minted) == 1
    assert "no tool was run" in repeated[0][0].output


def test_untrusted_speaker_cannot_use_even_a_readonly_tool_in_native_mode():
    attachment = Attachment()
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(
        canonical_speaker_allowed=attachment.admits_speaker
    )
    ledger.record_packet(_speaker(99), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger, tool_name="search_memory")
    relay = talk_cli.HostExecutionRelay(attachment, tool_authorizer=ledger.authorize_tool)
    asyncio.run(relay.handle_tool_batch_async((event,), "batch"))
    assert attachment.minted == []


def test_voice_projection_preserves_turn_order_and_filters_room_audio():
    attachment = Attachment()
    ledger = SimpleNamespace(
        input_speaker=lambda _: 42,
        binding_for_response=lambda _: SimpleNamespace(user_id=42, input_item_id="input-1"),
    )
    projection = talk_host_duplex.HostDuplexProjection(attachment, ledger)
    user = rt.Transcript(
        role=rt.TranscriptRole.USER,
        text="check the cluster",
        final=True,
        provenance=rt.TranscriptProvenance.INPUT_AUDIO,
        item_id="input-1",
    )
    projection.observe(user)
    projection.observe(user)
    projection.observe(
        rt.FunctionCall(
            response_id="tool-response",
            item_id="call-item",
            call_id="call-1",
            name="terminal",
            arguments="{}",
        )
    )
    projection.observe(rt.ResponseFinished(response_id="tool-response"))
    projection.observe(
        rt.Transcript(
            role=rt.TranscriptRole.ASSISTANT,
            text="Ready.",
            final=True,
            provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
            response_id="reply",
        )
    )
    projection.observe(rt.ResponseFinished(response_id="reply"))
    projection.observe(rt.ResponseFinished(response_id="reply"))
    assert attachment.inputs == [(42, "check the cluster")]
    assert attachment.responses == [
        ("tool-response", 42, "", {"has_tools": True, "input_item_id": "input-1"}),
        ("reply", 42, "Ready.", {"has_tools": False, "input_item_id": "input-1"}),
    ]
    assert projection.filter_audio({"user_id": 42}, b"\x01\x02")[1] == b"\x01\x02"
    assert projection.filter_audio({"user_id": 99}, b"\x01\x02") == (None, b"\0\0")


def test_final_transcript_after_commit_keeps_exact_input_identity():
    attachment = Attachment()
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(
        canonical_speaker_allowed=attachment.admits_speaker
    )
    ledger.record_packet(_speaker(42), _pcm(20))
    _bind_response(ledger)
    binding = ledger.binding_for_response("resp-1")
    assert binding.input_item_id
    assert ledger.input_speaker(binding.input_item_id) == 42
    projection = talk_host_duplex.HostDuplexProjection(attachment, ledger)
    projection.observe(
        rt.Transcript(
            role=rt.TranscriptRole.USER,
            text="late transcript",
            final=True,
            provenance=rt.TranscriptProvenance.INPUT_AUDIO,
            item_id=binding.input_item_id,
        )
    )
    assert attachment.inputs == [(42, "late transcript")]


def test_native_prompt_and_replay_preserve_history_roles():
    import talk_identity
    from talk_openai_realtime import encode_command

    prompt = talk_identity.build_instructions(
        {}, tools=[{"name": "terminal"}], host_execution=True, canonical_history=True
    )
    assert "talk_capabilities" not in prompt
    assert "temporary live transcript" not in prompt
    assert "normal session, skill, tool and approval" in prompt
    for role, content_type in (
        (rt.TranscriptRole.USER, "input_text"),
        (rt.TranscriptRole.ASSISTANT, "output_text"),
    ):
        event = encode_command(rt.ReplayConversationMessage("history-1", role, "prior context"))
        assert event["item"]["role"] == role.value
        assert event["item"]["content"] == [{"type": content_type, "text": "prior context"}]


def test_vad_and_commit_advance_host_inputs_before_reversed_final_transcripts():
    attachment = Attachment()
    attachment.started = []
    attachment.note_input_started = lambda user, **kw: attachment.started.append(
        (user, kw["input_item_id"]))
    ledger = SimpleNamespace(input_speaker=lambda _: 42)
    projection = talk_host_duplex.HostDuplexProjection(attachment, ledger)
    for item in ("A", "B"):
        projection.observe(rt.SpeechStopped(input_id=item, offset_ms=20))
        projection.observe(rt.InputAudioCommitted(input_id=item))
    for item in ("B", "A"):
        projection.observe(rt.Transcript(role=rt.TranscriptRole.USER, text=item, final=True,
                           provenance=rt.TranscriptProvenance.INPUT_AUDIO, item_id=item))
    assert attachment.started == [(42, "A"), (42, "A"), (42, "B"), (42, "B")]
    assert attachment.inputs == [(42, "B"), (42, "A")]


def test_trusted_room_attributes_unresolved_audio_to_owner_and_keeps_permit_live(monkeypatch):
    attachment = Attachment()
    attachment.trusted_room = True
    attachment.operator_user_id = 42
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(
        canonical_speaker_allowed=attachment.admits_speaker, trusted_room_owner=42
    )
    projection = talk_host_duplex.HostDuplexProjection(attachment, ledger)
    speaker, pcm = projection.filter_audio({"user_id": None}, b"\x01\x02")
    assert speaker["user_id"] == 42 and pcm == b"\x01\x02"
    ledger.record_packet({"user_id": None}, _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger, tool_name="terminal", arguments='{"command":"pwd"}')
    then = talk_operator_auth.time.monotonic()
    monkeypatch.setattr(talk_operator_auth.time, "monotonic", lambda: then + 3600)
    assert ledger.authorize_tool("terminal", event) is None
    assert ledger.authorize_tool("terminal", event) is not None
    assert ledger.binding_for_response("resp-1").user_id == 42


def test_canonical_execution_error_returns_a_result_without_ending_voice():
    class FailingAttachment(Attachment):
        async def execute_tool_batch(self, permits):
            raise RuntimeError("synthetic native refusal")

    attachment = FailingAttachment()
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(
        canonical_speaker_allowed=attachment.admits_speaker
    )
    ledger.record_packet(_speaker(42), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger, tool_name="terminal", arguments='{"command":"pwd"}')
    relay = talk_cli.HostExecutionRelay(attachment, tool_authorizer=ledger.authorize_tool)
    result = asyncio.run(relay.handle_tool_batch_async((event,), "batch"))
    assert "could not confirm execution" in result[0][0].output


def test_trusted_owner_prompt_reports_actual_approval_policy():
    import talk_identity

    prompt = talk_identity.build_instructions(
        {}, canonical_history=True, trusted_room=True, approvals_disabled=True
    )
    assert "private Discord room as trusted" in prompt
    assert "without asking for another confirmation" in prompt

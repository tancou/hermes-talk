# Canonical Discord voice through Hermes

This branch integrates with Hermes's optional continuous transcription API.
It does not require the proposed `agent.realtime_voice_provider` API v2.

After joining the bot to your voice channel with `/voice join`, run `/talk join`
in the linked text channel. On a compatible host this selects canonical voice;
`/talk core join` selects it explicitly. `/talk legacy join` keeps the old
provider-owned conversation available. Old hosts retain their old `/talk join`
behavior. Canonical startup never silently falls back to legacy after a refusal.

Talk streams OpenAI realtime transcription using the existing Talk authentication
and model configuration, with automatic assistant output and tools disabled.
Hermes runs the actual agent turn and speaks with its configured TTS. Consequently
`TALK_VOICE` does not select the final spoken voice in this mode. The agent model,
tools, memory and session are the host's normal Discord ones. This first provider
uses OpenAI; it does not select Gemini/Grok from `TALK_PROVIDER`.

Use `/talk status` to see readiness, pause state and speaker policy. `/talk pause`
and `/talk resume` control capture, and `/talk leave` closes the STT sockets and
restores the capture bridge. These controls must come from the linked text channel.
Pause is microphone-only: accepted agent work and playback can continue.

For a restricted room where all authorized participants may issue requests, set
this in the **owning Hermes profile**, then leave/rejoin Talk:

```yaml
discord:
  voice_operator_only: false
```

The default `true` admits only the person who started Talk. With `false`, current
human room members who pass Hermes's Discord access rules are admitted under their
own identities, with one STT stream per speaker (and potentially more provider
usage). Neither mode bypasses normal tool approvals or profile isolation.
`TALK_DISCORD_OPERATOR_USER_IDS` governs the separate legacy tool executor, not
this native path. Doctor reports `continuous_stt_available` independently from the
optional speculative realtime API; warnings about that older API do not establish
whether this canonical ingress is available. `/talk status` reports the live path.

## Joint acceptance before deployment

1. In a dedicated Discord test room, join voice and start `/talk core join`.
   Check status changes from starting to listening and states the expected policy.
2. Ask for information given earlier in the linked text conversation, then request
   a harmless tool operation. Check the existing session's tool trace and history.
3. Exercise an operation that normally requests approval; verify its usual prompt
   and refusal behavior. Check memory and an MCP tool if enabled in this profile.
4. Speak two successive requests; check each final transcript is admitted once,
   replies are audible, and normal busy/steering behavior is preserved.
5. Pause, speak, resume, then leave. Confirm pause drops input, leave stops capture,
   and ordinary Hermes voice playback/input still works afterward.
6. With strict policy, another member's speech must not enter a turn. With relaxed
   policy, another authorized member must keep their own user ID. An unauthorized
   member, unresolved audio, another text channel or another guild stays refused.
7. Rebind the linked text channel or reconnect Discord mid-transcription. The
   attachment must end rather than replaying the old speech into the new binding.
8. Repeat from another Hermes profile and verify its model, tools and conversation.

This implementation does not promise provider-native duplex latency or immediate
speech-start interruption of tools. It uses the existing native Hermes turn and
TTS behavior. Offline tests do not replace these microphone/provider checks.

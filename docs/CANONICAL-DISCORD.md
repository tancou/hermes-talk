# Canonical Discord voice with Realtime duplex

On a compatible Hermes host, `/talk join` now selects native Realtime audio with
canonical Hermes tool execution. `/talk core join` explicitly selects this path.
First connect the bot with `/voice join` in the linked text channel. `/talk status`
reports **Realtime duplex voice with canonical Hermes tools** and the speaker policy.
Startup refusal never silently downgrades to the legacy executor.

OpenAI Realtime (including the existing Codex OAuth authentication) owns listening,
spoken responses and speech interruption. `TALK_MODEL` and `TALK_VOICE` select that
model and voice; use `TALK_VOICE_MODE=native` for native audio. Its exact tool calls
enter the host's ordinary Discord admission, agent loop, tool authorization and
approval pipeline. No second model reinterprets the spoken request. A child agent
uses the profile's normal model. Native TTS acknowledgements are suppressed while
Realtime owns the audio. Audio continues and barge-in cancels/truncates playback
while a host tool is pending; this does not undo or automatically cancel that tool.

The host exposes its enabled tools, skills and MCP tools through the existing
canonical discovery surface. Its initial context includes up to 20 recent text
messages (16,000 characters) from the linked Hermes conversation, preserving roles.
Final input, tool calls/results and the final spoken response are recorded together
as a native turn. Input attribution uses immutable Discord IDs and the exact VAD
input item, including when transcription arrives after the first response. Tools
wait up to 30 seconds for that final transcript. Missing attribution or transcription
refuses execution. Mixed-speaker intervals cannot authorize tools.
VAD/commit order advances the native input timeline; final transcripts only
complete their own items, so a late final cannot close a newer tool turn.

### Live context and asynchronous results

Hosts exposing `conversation_sync_version == 1` provide committed additions to the
exact bound conversation. Talk polls this journal while the call is open and drains
it again immediately before every `response.create`, including tool continuations.
Completed delegations and new Discord text therefore become model context without
rejoining or polling `delegate_task(action="list")`. That tool lists active work;
it is not a completed-result inbox.

Message IDs and origins survive delivery. Internal results are assistant data with
an explicit provenance envelope, not invented user instructions. Native voice
input/tool/speech rows already delivered by Realtime are excluded from the tail.
Only successfully transmitted batches are acknowledged. Failed writes terminate
the connection; unacknowledged batches remain replayable, and a new attachment
starts with the host's bounded historical snapshot plus subsequent additions.
There is no persistent WebSocket resume or guarantee of exactly-once spoken
announcements across a process crash.

Context delivery continues while the user speaks. A spontaneous result announcement
waits for an idle response, idle playback and no pending user turn. A user's next
answer takes precedence over a redundant announcement. Logs distinguish committed
rows, context transmission, announcement requests and completed playback; they
contain identifiers, not result text. A context read failure or session replacement
ends the attachment instead of answering from stale or foreign context.

The first implementation requires a host agent using the `chat_completions` protocol
(e.g. the configured OpenRouter devops profile); it refuses other host protocols.
This restriction concerns the host executor, not Realtime audio authentication.
Replay is bounded context, not live synchronization of concurrent text edits.

## Profile configuration

```yaml
discord:
  voice_engine: realtime
  voice_operator_only: true
```

Set these through Hermes configuration, for example:

```sh
hermes -p devops config set discord.voice_engine realtime
hermes -p devops config set discord.voice_operator_only false
```

`voice_operator_only: false` admits other currently authorized human members of the
same room, under their own Discord identities. `true` admits only the initiating
operator. Leave/rejoin after changing it. The switch relaxes room-speaker selection.
Realtime admits only members whose freshly resolved route matches this attachment's
profile; another profile requires its own attachment. STT keeps native per-speaker routing.
Discord access rules, profile isolation and normal tool approvals remain active.
`TALK_DISCORD_OPERATOR_USER_IDS` governs the separate legacy executor. Canonical
mode replaces the legacy fixed tool allowlist with the host's actual tool policy.
Approval prompts use the linked text conversation; a spoken yes does not resolve
a pending native approval unless the host explicitly provides that mechanism.

`/talk pause`, `/talk resume` and `/talk leave` control the session from its linked
text channel. Pause affects microphone capture, not already admitted tool work.
`/talk transcribe join` selects continuous STT → native agent → configured TTS.
`discord.voice_engine: transcription` makes that path the default for `/talk join`.
`/talk legacy join` explicitly selects the old provider-owned tool executor.
Older hosts retain their available behavior. Doctor's older optional realtime API-v2
warnings do not describe this new invocation-bound attachment; inspect live status.

## Dedicated owner and trusted room

For a private room fully owned by the operator:

```yaml
discord:
  voice_engine: realtime
  voice_trusted_room: true
approvals:
  mode: off
```

`voice_trusted_room` defaults to false. When true, all audio from this bound room
is attributed to the person who started Talk, including unknown or overlapping
Discord speaker attribution. The legacy spoken-permit expiry does not reject
those calls. This supersedes `voice_operator_only` for audio. Use it when every
participant in that room may act as the initiating owner. Status reports
`trusted room as owner`. Leave/rejoin to change the voice policy.

`approvals.mode: off` is Hermes's existing profile-wide approval setting; it also
applies to ordinary text and child tasks in this profile. The voice prompt reports
that setting so the model does not add an unnecessary confirmation round. Explicit
plugin blocks and external OS/service access constraints remain separate. Existing
optional settings include `security.protected_instruction_files: false` to allow
instruction-file writes without a human prompt, `security.allow_private_urls: true`
for private network URLs, and `approvals.mcp_reload_confirm: false`.

The gateway's receiving adapter callback still carries the real bot/profile
provenance. Trusting room audio does not invent a different runtime profile. This
also binds audio capture and playback to the captured adapter, connection and
room, even when the runtime profile owns a different bot in the same server.
STT uses the same bound transport. Reconnects invalidate it instead of selecting
another connection. A refused concurrent join closes only its new attachment and
leaves the running session's policy intact. This
fixes the native `authorization_home_without_provenance` refusal observed on
multiplexed devops; the regression test now uses real profile callback wiring.
A native execution error is returned as an unconfirmed tool outcome instead of
terminating the entire audio session; uncertain operations are not retried blindly.

## Acceptance checks

1. Start on the intended profile; verify status names Realtime duplex/canonical tools.
2. Ask about a recent message in the linked text conversation, then request a harmless
   tool call. Verify one native history turn, exact result and the expected profile.
3. Interrupt a spoken answer, including while a tool is pending. Playback must stop
   promptly; the tool's eventual outcome must remain accurately reported.
4. Exercise a native approval and deny it in text. Confirm the operation did not run.
5. Test skills/MCP and a child task enabled in this profile.
6. Test pause/resume/leave, then rejoin; confirm context and normal Discord voice recover.
7. With strict policy, another member is ignored; with relaxed policy, an authorized
   member works under their own identity. Unknown/mixed speakers stay refused.
8. Reconnect/rebind Discord; the old attachment must stop, never target a new room.

Offline tests cover real gateway admission, tool execution, persistence and replay,
late input, speaker provenance, expired/reused permits, and duplex interruption
while tools wait. They do not replace live microphone/provider acceptance.

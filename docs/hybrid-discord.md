# Same-chat hybrid Discord voice (experimental)

`/talk hybrid join` adds short host tool operations and native background children
to [same-chat delegated voice](delegated-discord.md). It uses the same initiating
speaker, profile and text conversation. Ordinary `/talk join` is unchanged.

This is a development integration, not a released host compatibility promise.
It requires the host plugin APIs `submit_message`, `submission_status`,
`control_submission`, `conversation_tools` and `execute_tools`, plus plugin
`allow_gateway_injection` permission. Missing APIs or a refused tool catalog stop
setup. There is no fallback to detached API-server work or bare tool dispatch.
Realtime and cascade use this route; GPT-Live does not.

Follow the delegated-mode setup, then use `/talk hybrid join`. It automatically
disables Hermes automatic speech in the text chat, just like delegated mode.
The initiating speaker can ask for a short operation,
start a longer task, and discuss another subject while the child works. Host
tool availability, permissions and approvals still apply. This change does not
add a spoken approval bridge: an approval may still require the host's interface.

| Voice tool | Host behavior |
| --- | --- |
| Advertised host tool | Executes the exact arguments through the normal host conversation loop and tool executor. |
| `delegate_work` | Starts native `delegate_task` children; refuses if the host cannot run them in the background. |
| `delegation_control` | Lists native children or requests steering/stop for a specific child ID. |
| `submit_task` | Submits an ordinary parent conversation turn; it can occupy the parent until it finishes. |
| `submission_status` / `control_task` | Observes or controls that exact submitted parent turn, not its children. |

The parent remains a single writer. Short operations queue behind any current
parent turn; they can run while native background children work. A slow direct
tool can occupy the parent too: the prompt asks the model to delegate code changes,
investigations and test suites, but it cannot predict every tool's duration.
The voice call remains duplex while host requests are pending.

### Discord delegation follow-up threads

In this experimental branch, a confirmed `delegate_work` launch starts one
read-only thread under the initiating text channel. Its starter message links
to the thread through Discord's native thread UI. The thread contains the task,
the child's Hermes session ID, stored assistant messages, tool calls/results,
and the final stored response followed by the session's recorded end reason.
It observes the existing child; it never opens another agent conversation or
forwards thread replies to that child. Use Talk's existing controls to steer/stop.

The receiving bot needs View Channel, Send Messages, Create Public Threads and
Send Messages in Threads permissions. Only the original server text channel is
eligible: there is no fallback from a private thread, DM or forum to a broader
channel. The initiating user's channel visibility is checked before each send.
Child ownership is checked against the captured profile and parent conversation.
System prompts, injected user context and reasoning fields are omitted; output is
secret-redacted before shortening and mentions are neutralized. Long entries are
abridged; the stored Hermes transcript remains the detailed source.

The tool's `thread_followup` receipt distinguishes `starting`, `active`, `ended`,
`unavailable` and `stopped`; it includes the link once creation is confirmed.
These are mirror states, not child execution outcomes. Failed Discord delivery
does not repeat native work, stop the child, or block voice. Ambiguous sends and
thread creation are not retried automatically.

Followers continue after leaving Talk, but are limited to 16 active children per
gateway process and 24 hours each. Child storage may take up to 30 seconds to
appear. Gateway restart does not restore followers, and transcript rotation or
rewrite stops a follower instead of silently replaying another segment. Only
explicit hybrid `delegate_work` launches observed in this call are mirrored;
children launched inside `submit_task`, nested fan-out and historical tasks are
not automatically adopted. The host's existing private child-session ownership
helper and Discord bridge remain compatibility requirements.
Catalog, execution and control waits use dedicated workers so they cannot occupy
the gateway's default pool while waiting for that same pool to read or admit work.

Submission status reports available controls and observed tool progress, not a
percentage or an invented completion estimate. Queued work can be cancelled;
running parent work can be steered or interrupted when the host advertises that
control. A stale authority/generation is refused. A lost control acknowledgement
is reported as unknown and is not automatically retried.

Native child steering is backend-dependent. This integration verifies delivery
to the next native Hermes agent inference. It does not add steering delivery to
native Codex app-server children: their queue acknowledgement is not proof of
delivery. Child control requires the host's resident parent/tree ownership; it
does not adopt arbitrary child sessions after a restart. A stopped or completed
parent turn is not evidence that its children stopped or completed.

Exact tool requests reuse their original admission identity when observed again.
A repeated provider call cannot change its arguments. Terminal receipts carry
actual tool outputs; background completion notices remain separate. Updates and
results enter voice context and announcements wait for idle speech/playback,
using the existing delegated-mode behavior. Interrupting speech does not cancel
work. No automatic child-result injection into an active host inference is added.

The delegated-mode reset/compaction, history and audio-bridge limits still apply.
Leave and rejoin after a reset or compaction. Talk tracks at most 64 requests per
call and does not adopt its previous call's requests after a process restart.
Live microphone, provider audio, Discord reconnect and spoken approval behavior
need acceptance testing before relying on this mode in production.

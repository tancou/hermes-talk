"""Read-only Discord mirrors of children launched by hybrid voice."""

from __future__ import annotations

import asyncio
import json
import logging
import time

_log = logging.getLogger(__name__)
_TASKS: set[asyncio.Task] = set()
MAX_FOLLOWERS = 16
POLL_SECONDS = 2


def _safe_text(text: str, limit: int = 1700) -> str:
    from tools.delegation_redaction import redact_child_text

    # Redact before truncation so a cut cannot hide a credential's opening marker.
    text = redact_child_text(text, force=True).replace("@", "@\u200b")
    if len(text) > limit:
        suffix = "\n[abridged — full transcript remains in Hermes]"
        text = text[:limit - len(suffix)] + suffix
    return text


def _content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            block["text"] for block in value
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
            and isinstance(block.get("text"), str)
        )
    return ""


def _render(row) -> str:
    # System prompts, injected user context and reasoning sidecars are not a public transcript.
    if row.role not in {"assistant", "tool"} or row.compressed_summary:
        return ""
    parts = []
    if text := _content(row.content):
        parts.append(("Assistant" if row.role == "assistant" else "Tool result") + ":\n" + text)
    if row.role == "assistant" and isinstance(row.tool_calls, list):
        for call in row.tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict):
                parts.append("Tool call:\n" + json.dumps(
                    {key: function.get(key) for key in ("name", "arguments")}, ensure_ascii=False,
                ))
    return _safe_text("\n\n".join(parts)) if parts else ""


class ChildTranscript:
    """Read only the captured profile and a child proven to descend from its parent."""

    def __init__(self, binding, child_id):
        self.binding, self.child_id = binding, child_id
        self.session_id = None
        self.cursor = 0
        self.cursor_state = None

    def read(self):
        from persistence.conversation.messages import TranscriptRead
        from persistence_assembly import open_persistence_reader
        from tools.delegate_tool_sessions import stored_child_session

        with open_persistence_reader(self.binding.profile_dir) as reader:
            messages = reader.messages
            with messages.read_snapshot() as snapshot:
                session_id = stored_child_session(reader, self.child_id, [self.binding.session_id])
                if session_id is None:
                    raise LookupError("Child transcript is not available in this conversation")
                # shortcut: stop on rotation, add lineage-aware paging before following compression.
                if self.session_id is not None and session_id != self.session_id:
                    raise RuntimeError("Child transcript rotated; follow-up stopped")
                session = reader.sessions.get(session_id)
                revision = messages.conversation_revision(session_id)
                if self.cursor_state is not None and messages.transcript_cursor_state(
                    session_id, self.cursor, revision.lineage_ids,
                ) != self.cursor_state:
                    raise RuntimeError("Child transcript changed; follow-up stopped")
                rows = messages.read_transcript(
                    session_id, TranscriptRead(after_id=self.cursor, limit=4),
                )
                cursor = rows[-1].id if rows else self.cursor
                state = messages.transcript_cursor_state(session_id, cursor, revision.lineage_ids)
                rendered = [text for row in rows if (text := _render(row))]
                if not snapshot():
                    raise RuntimeError("Child transcript changed during read")
                self.session_id, self.cursor, self.cursor_state = session_id, cursor, state
                ended = None
                if session.ended_at is not None and not rows:
                    ended = session.end_reason or "closed"
                return rendered, ended


async def _parent(adapter, binding):
    import discord

    client = adapter._client
    parent = client.get_channel(int(binding.chat_id))
    if parent is None:
        parent = await client.fetch_channel(int(binding.chat_id))
    # Never move a mirror to a thread's parent, a forum, another server or another bot.
    if not isinstance(parent, discord.TextChannel) or parent.guild.id != binding.guild_id:
        raise ValueError("Follow-up requires the originating server text channel")
    member = parent.guild.get_member(int(binding.user_id))
    if member is None:
        member = await parent.guild.fetch_member(int(binding.user_id))
    if not parent.permissions_for(member).view_channel:
        raise PermissionError("The initiating user can no longer view this channel")
    permissions = parent.permissions_for(parent.guild.me)
    if not all(getattr(permissions, name, False) for name in (
        "view_channel", "send_messages", "create_public_threads", "send_messages_in_threads",
    )):
        raise PermissionError("The bot cannot create and publish this follow-up")
    return parent


async def _send(adapter, binding, chat_id, text):
    await _parent(adapter, binding)
    result = await adapter.send(
        str(chat_id), _safe_text(text), metadata={"non_conversational": True},
    )
    if not result.success:
        raise RuntimeError("Discord follow-up delivery failed")
    return result


async def _follow(adapter, binding, child_id, task, receipt):
    transcript = ChildTranscript(binding, child_id)
    thread_id = None
    try:
        # Admission can precede the child's first persisted row.
        for attempt in range(15):
            try:
                rows, ended = await asyncio.to_thread(transcript.read)
                break
            except LookupError:
                if attempt == 14:
                    raise
                await asyncio.sleep(POLL_SECONDS)
        parent = await _parent(adapter, binding)
        starter = await _send(adapter, binding, binding.chat_id, "Delegation follow-up:\n" + task)
        thread = await parent.get_partial_message(int(starter.message_id)).create_thread(
            name=_safe_text("Hermes delegation " + child_id[:40]), auto_archive_duration=1440,
        )
        if thread.parent_id != int(binding.chat_id) or thread.guild.id != binding.guild_id:
            raise ValueError("Discord returned an unrelated thread")
        thread_id = str(thread.id)
        receipt.update(
            status="active", thread_id=thread_id, child_session_id=transcript.session_id,
            url=f"https://discord.com/channels/{binding.guild_id}/{thread_id}",
        )
        await _send(
            adapter, binding, thread_id,
            f"Hermes session: {transcript.session_id}\n"
            "Read-only follow-up: use Talk to steer or stop this child. "
            "Replies here are not forwarded to it. Long entries are abridged.\n\nTask:\n" + task,
        )
        deadline = time.monotonic() + 24 * 60 * 60
        while True:
            for text in rows:
                await _send(adapter, binding, thread_id, text)
            if ended is not None:
                await _send(adapter, binding, thread_id, "Hermes session ended: " + ended)
                receipt["status"] = "ended"
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Follow-up time limit reached")
            await asyncio.sleep(POLL_SECONDS)
            rows, ended = await asyncio.to_thread(transcript.read)
    except asyncio.CancelledError:
        receipt["status"] = "stopped"
        raise
    except Exception as exc:  # noqa: BLE001 - a failed mirror must not affect the child or voice
        receipt["status"] = "stopped" if thread_id else "unavailable"
        _log.warning("Discord delegation follow-up stopped (%s)", type(exc).__name__)
        try:
            await _send(
                adapter, binding, thread_id or binding.chat_id,
                "Discord follow-up is unavailable or stopped. This does not stop the delegated "
                "work. Ask Talk for its status; its transcript remains in Hermes.",
            )
        except Exception:  # noqa: BLE001 - transport failure is already reported in the log
            _log.debug("Could not deliver delegation follow-up failure notice")


def start_followup(adapter, binding, payload, task):
    """Schedule a confirmed native launch; never interpret a receipt as child completion."""
    if not isinstance(payload, dict):
        return {"status": "unavailable"}
    children = payload.get("subagent_ids")
    if payload.get("status") != "dispatched" or payload.get("mode") != "background":
        return {"status": "unavailable"}
    # delegate_work submits exactly one task. Do not guess when the host returns a different shape.
    if not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], str):
        return {"status": "unavailable"}
    if not children[0] or len(children[0]) > 128 or len(_TASKS) >= MAX_FOLLOWERS:
        return {"status": "unavailable"}
    # shortcut: process-local followers, add durable recovery before restart adoption.
    receipt = {"status": "starting"}
    follower = asyncio.create_task(_follow(adapter, binding, children[0], task, receipt))
    _TASKS.add(follower)
    follower.add_done_callback(_TASKS.discard)
    return receipt

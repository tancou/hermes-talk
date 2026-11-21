"""A synchronous credential mint must not freeze Discord's owning event loop."""

import asyncio
import threading

import pytest

from talk_openai_realtime import _OpenAIWireSession


def test_mint_runs_off_the_gateway_loop():
    started, release = threading.Event(), threading.Event()

    def mint(**_):
        started.set()
        release.wait(5)
        raise RuntimeError("scripted mint stopped")

    async def run():
        wire = _OpenAIWireSession(auth_token="fake", auth_source="test", mint_session=mint)
        # Also release on the old blocking implementation, so its regression is
        # an assertion failure instead of a permanently wedged test process.
        guard = threading.Timer(3, release.set)
        guard.start()
        task = asyncio.create_task(wire.connect(
            model="test", voice="cedar", instructions="", tools=None,
            automatic_response=False, session_update={}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            responsive = not task.done() and not release.is_set()
            release.set()
            with pytest.raises(RuntimeError, match="scripted mint stopped"):
                await task
            assert responsive, "the gateway loop froze during credential minting"
        finally:
            release.set()
            guard.cancel()
            guard.join(timeout=5)
            await wire.close()
    asyncio.run(run())

"""Real gateway admission, tools, approval and TTS with Talk's late-input projection."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("profile,trusted", [("default", False), ("devops", False),
                                            ("devops", True)])
def test_projection_preserves_pending_native_turn_with_reversed_finales(tmp_path, profile, trusted):
    agent = Path(os.environ.get("HERMES_AGENT_REPO", ""))
    peer = agent / "tests/gateway/fixtures/realtime_execution_peer.py"
    if not os.environ.get("HERMES_AGENT_REPO") or not peer.is_file():
        pytest.skip("requires the companion Agent checkout via HERMES_AGENT_REPO")
    talk = Path(__file__).resolve().parents[1]
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ", "SYSTEMROOT") if k in os.environ}
    env.update(HERMES_HOME=str(tmp_path), PYTHONPATH=os.pathsep.join((str(agent), str(talk))))
    code = textwrap.dedent("""
        import inspect
        import runpy
        import sys
        from pathlib import Path
        from gateway.realtime_execution import RealtimeExecutionAttachment
        import talk_host_duplex
        import talk_operator_auth
        import talk_realtime as rt

        peer, profile, policy = sys.argv[1:]
        assert Path(inspect.getfile(RealtimeExecutionAttachment)).resolve().is_relative_to(
            Path(peer).resolve().parents[3])
        original = RealtimeExecutionAttachment.note_input
        inserted = set()
        def final(self, user, text, *, input_item_id=None):
            if input_item_id != 'fixture-input' or id(self) in inserted:
                return original(self, user, text, input_item_id=input_item_id)
            inserted.add(id(self))
            ledger = talk_operator_auth.DiscordToolAuthorizationLedger(
                canonical_speaker_allowed=self.admits_speaker,
                trusted_room_owner=(42 if self.trusted_room else None))
            ledger.record_packet({'user_id': 42}, bytes(40 * 24 * 2))
            projection = talk_host_duplex.HostDuplexProjection(self, ledger)
            for item, start, end in (('older-A', 0, 20), ('fixture-input', 20, 40)):
                ledger.note_speech_started({'item_id': item, 'audio_start_ms': start})
                ledger.note_speech_stopped({'item_id': item, 'audio_end_ms': end})
                projection.observe(rt.SpeechStopped(input_id=item, offset_ms=end))
            for item, transcript in (('fixture-input', text), ('older-A', 'late older A')):
                projection.observe(rt.Transcript(role=rt.TranscriptRole.USER, text=transcript,
                    final=True, provenance=rt.TranscriptProvenance.INPUT_AUDIO, item_id=item))
            assert not self._turns[(42, 'fixture-input')].ended
        RealtimeExecutionAttachment.note_input = final
        sys.argv = [peer, profile, policy]
        runpy.run_path(peer, run_name='__main__')
        assert inserted, 'Talk projection was never exercised'
    """)
    result = subprocess.run([sys.executable, "-c", code, str(peer.resolve()), profile,
                             "trusted" if trusted else "strict"],
                            env=env, cwd=agent, capture_output=True, text=True, timeout=80,
                            check=False)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr

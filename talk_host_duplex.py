"""Project duplex transcripts onto the host's authenticated canonical turns."""
try:
    from . import talk_realtime as rt
except ImportError:
    import talk_realtime as rt


def canonical(attachment):
    return getattr(attachment, 'realtime_execution_version', None) == 1


class HostDuplexProjection:
    def __init__(self, attachment, ledger):
        self.attachment, self.ledger = attachment, ledger
        self.text = {}
        self.tool_responses = set()
        self.seen_inputs = set()
        self.seen_responses = set()

    def observe(self, event):
        if isinstance(event, rt.Transcript):
            if event.role == rt.TranscriptRole.USER and event.final:
                if event.item_id in self.seen_inputs:
                    return
                if len(self.seen_inputs) >= 4096:
                    raise RuntimeError('Voice history capacity reached; rejoin')
                self.seen_inputs.add(event.item_id)
                user = self.ledger.input_speaker(event.item_id)
                if user is not None and self.attachment.admits_speaker(user):
                    self.attachment.note_input(user, event.text, input_item_id=event.item_id)
            elif event.role == rt.TranscriptRole.ASSISTANT and event.response_id:
                if len(self.text) >= 128 and event.response_id not in self.text:
                    raise RuntimeError('Too many unfinished voice responses')
                self.text[event.response_id] = (event.text if event.final else
                    self.text.get(event.response_id, '') + event.text)[:32000]
        elif isinstance(event, rt.FunctionCall):
            self.tool_responses.add(event.response_id)
        elif isinstance(event, rt.ResponseFinished):
            if event.response_id in self.seen_responses:
                return
            if len(self.seen_responses) >= 4096:
                raise RuntimeError('Voice history capacity reached; rejoin')
            self.seen_responses.add(event.response_id)
            text = self.text.pop(event.response_id, '')
            has_tools = event.response_id in self.tool_responses
            self.tool_responses.discard(event.response_id)
            binding = self.ledger.binding_for_response(event.response_id)
            user = binding.user_id if binding is not None else None
            if user is not None and self.attachment.admits_speaker(user):
                self.attachment.note_response(event.response_id, user, text, has_tools=has_tools,
                    input_item_id=binding.input_item_id)

    def filter_audio(self, speaker, chunk):
        user = speaker.get('user_id') if isinstance(speaker, dict) else None
        if user is None or not self.attachment.admits_speaker(user):
            return None, bytes(len(chunk))
        return speaker, chunk

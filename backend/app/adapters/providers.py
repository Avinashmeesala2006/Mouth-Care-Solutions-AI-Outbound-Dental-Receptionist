from dataclasses import dataclass

@dataclass
class MockVoiceProvider:
    name: str = 'mock'
    def synthesize(self, text: str) -> dict:
        return {'provider': self.name, 'text': text, 'audio': None}
    def start_session(self, session_id: str) -> dict:
        return {'session_id': session_id, 'status': 'started', 'provider': self.name}
    def stop_session(self, session_id: str) -> dict:
        return {'session_id': session_id, 'status': 'stopped', 'provider': self.name}
    def usage_event(self, minutes: float) -> dict:
        return {'provider': self.name, 'minutes': minutes}

class MockNotificationProvider:
    def send(self, channel: str, recipient: str, message: str) -> dict:
        return {'provider': 'mock', 'channel': channel, 'recipient': recipient, 'status': 'queued'}

class MockTelephonyProvider:
    def inbound_call(self, call_id: str) -> dict:
        return {'call_id': call_id, 'status': 'accepted', 'provider': 'mock'}
    def transfer(self, call_id: str, target: str) -> dict:
        return {'call_id': call_id, 'target': target, 'status': 'queued'}

class ElevenLabsAdapter(MockVoiceProvider):
    """Credential-gated seam; real API calls are intentionally not enabled in demo mode."""
    name = 'elevenlabs'

class GoogleCalendarAdapter:
    """Credential-gated scheduling seam. Live availability must replace demo slots."""
    def check_availability(self, *args, **kwargs):
        raise RuntimeError('live calendar is not configured')

class NotificationAdapter(MockNotificationProvider):
    pass

VoiceProvider = MockVoiceProvider
''

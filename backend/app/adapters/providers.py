"""Non-voice integration seams. Telephony is Twilio and speech is Fish Speech; there are no
alternative or mock voice/telephony providers in the application."""
from dataclasses import dataclass


@dataclass
class MockNotificationProvider:
    name: str = 'mock'

    def send(self, channel: str, recipient: str, message: str) -> dict:
        return {'provider': self.name, 'channel': channel, 'recipient': recipient, 'status': 'queued'}


class GoogleCalendarAdapter:
    """Credential-gated scheduling seam. Live availability must replace demo slots."""

    def check_availability(self, *args, **kwargs):
        raise RuntimeError('live calendar is not configured')


class NotificationAdapter(MockNotificationProvider):
    pass

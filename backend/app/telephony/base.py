"""Provider-neutral telephony errors."""
from __future__ import annotations


class TelephonyError(RuntimeError):
    """A provider failure with a stage and a safe, secret-free message.

    ``outcome_unknown`` is true when the provider may have acted even though the request
    failed (for example a timeout after the create-call request was sent). Such requests
    must never be retried automatically: a retry can place a duplicate call.
    """

    def __init__(self, stage: str, message: str, *, code: str | None = None, http_status: int | None = None,
                 outcome_unknown: bool = False, retry_after: int | None = None):
        super().__init__(f'{stage}: {message}')
        self.stage, self.message, self.code = stage, message, code
        self.http_status = http_status
        self.outcome_unknown = outcome_unknown
        self.retry_after = retry_after

    def as_dict(self) -> dict:
        return {'stage': self.stage, 'message': self.message, 'code': self.code, 'http_status': self.http_status,
                'outcome_unknown': self.outcome_unknown}

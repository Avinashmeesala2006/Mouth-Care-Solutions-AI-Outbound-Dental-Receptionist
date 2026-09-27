"""Verify the running application, its public HTTPS route for Twilio, and live-call readiness.

    python scripts/verify_public_route.py [--origin https://host] [--destination 919XXXXXXXXX] [--verify-remote]

Checks that PUBLIC_BASE_URL reaches *this* FastAPI process (same instance id) and serves the
Twilio health route, then prints ``GET /api/twilio/preflight``. ``--verify-remote`` makes one
read-only Twilio account request. Never places a call. Exit code 0 only
when LIVE_CALL_ALLOWED is true.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.core.config import settings  # noqa: E402

HEADERS = {'ngrok-skip-browser-warning': 'true', 'Accept': 'application/json'}


def response_failure(response, expected_instance_id: str | None = None) -> str | None:
    content_type = response.headers.get('content-type', '').lower()
    body = response.text[:2000].lower()
    if 'err_ngrok' in body or (content_type.startswith('text/html') and 'ngrok' in body):
        return 'ngrok_error_or_interstitial'
    if response.status_code != 200 or not content_type.startswith('application/json'):
        return 'fastapi_unreachable'
    try:
        payload = response.json()
    except ValueError:
        return 'fastapi_unreachable'
    if expected_instance_id is not None:
        return None if payload.get('instance_id') == expected_instance_id else 'wrong_fastapi_process'
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin', default=settings.resolve().public_origin)
    parser.add_argument('--destination')
    parser.add_argument('--verify-remote', action='store_true')
    args = parser.parse_args()
    try:
        local = httpx.get('http://127.0.0.1:8000/health', timeout=5).json()
    except (httpx.HTTPError, ValueError) as exc:
        print('LOCAL_HEALTH=fastapi_unreachable', type(exc).__name__)
        return 1
    print(f"LOCAL_HEALTH=ok mode={local.get('mode')} telephony={local.get('telephony')}")
    origin = (args.origin or '').rstrip('/')
    if origin:
        try:
            public = httpx.get(f'{origin}/health', timeout=15, follow_redirects=False, headers=HEADERS)
            failure = response_failure(public, local.get('instance_id'))
            twilio = httpx.get(f'{origin}/api/telephony/twilio/health', timeout=15, headers=HEADERS)
            twilio_ok = twilio.status_code == 200 and twilio.json().get('provider') == 'twilio'
        except (httpx.HTTPError, ValueError) as exc:
            failure, twilio_ok = f'fastapi_unreachable ({type(exc).__name__})', False
        print(f'PUBLIC_ORIGIN={origin} PUBLIC_HEALTH=' + (failure or 'verified_same_fastapi_process')
              + f' TWILIO_ROUTES_PUBLIC={twilio_ok}')
    else:
        print('PUBLIC_ORIGIN=(not configured: Twilio cannot reach the webhook endpoints)')
    params = {**({'destination': args.destination} if args.destination else {}),
              **({'verify_remote': '1'} if args.verify_remote else {})}
    preflight = httpx.get('http://127.0.0.1:8000/api/twilio/preflight', params=params, timeout=120).json()
    details = preflight.pop('DETAILS', {})
    for key, value in preflight.items():
        print(f'{key}={json.dumps(value)}')
    print('FISH_SPEECH', json.dumps(details.get('fish_speech')))
    return 0 if preflight.get('LIVE_CALL_ALLOWED') is True else 1


if __name__ == '__main__':
    sys.exit(main())

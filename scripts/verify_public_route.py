"""Verify the running application and (optionally) its public HTTPS route; print the live-call preflight.

    python scripts/verify_public_route.py [--origin https://host] [--destination +91...]

The public origin (e.g. an ngrok domain) only serves the web application; it does not
provide telephone connectivity. Live calls go FastAPI -> Asterisk -> the configured SIP
trunk or GSM modem. Exit code 0 only when LIVE_CALL_ALLOWED is true.
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
    if 'err_ngrok_6024' in body or (content_type.startswith('text/html') and 'ngrok' in body):
        return 'ngrok_browser_interstitial'
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
    args = parser.parse_args()
    try:
        local = httpx.get('http://127.0.0.1:8000/health', timeout=5).json()
    except (httpx.HTTPError, ValueError) as exc:
        print('LOCAL_HEALTH=fastapi_unreachable', type(exc).__name__)
        return 1
    print('LOCAL_HEALTH=ok mode=' + str(local.get('mode')))
    origin = (args.origin or '').rstrip('/')
    if origin:
        try:
            public = httpx.get(f'{origin}/health', timeout=15, follow_redirects=False, headers=HEADERS)
            failure = response_failure(public, local.get('instance_id'))
        except httpx.HTTPError as exc:
            failure = f'fastapi_unreachable ({type(exc).__name__})'
        print(f'PUBLIC_ORIGIN={origin} PUBLIC_HEALTH=' + (failure or 'verified_same_fastapi_process'))
    else:
        print('PUBLIC_ORIGIN=(not configured; not required for telephony)')
    params = {'refresh': '1', **({'destination': args.destination} if args.destination else {})}
    preflight = httpx.get('http://127.0.0.1:8000/api/telephony/preflight', params=params, timeout=120).json()
    details = preflight.pop('DETAILS', {})
    for key, value in preflight.items():
        print(f'{key}={json.dumps(value)}')
    print('TELEPHONY_DETAILS', json.dumps(details.get('telephony')))
    return 0 if preflight.get('LIVE_CALL_ALLOWED') is True else 1


if __name__ == '__main__':
    sys.exit(main())

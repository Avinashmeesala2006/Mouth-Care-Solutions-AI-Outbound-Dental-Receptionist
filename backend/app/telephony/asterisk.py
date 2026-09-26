"""Asterisk telephony provider: AMI for readiness and call origination, events for call state.

Outbound flow: ``Originate`` (async) on the configured outbound channel (a PJSIP trunk or
a chan_dongle GSM modem); when the callee answers, Asterisk runs the receptionist
dialplan context, which hands the call to this application's FastAGI server. Asterisk
itself cannot reach the phone network: readiness is only reported when a real trunk is
registered or a real modem is attached, registered and has a ready SIM.
"""
from __future__ import annotations

import re
import threading
import time

from ..core.config import mask_phone
from .ami import AMIConnection, AMIError
from .base import TelephonyError
from .host import CachedHostInspector, HostReport

ORIGINATE_FAILURE_REASONS = {'0': 'failed', '1': 'failed', '3': 'no-answer', '5': 'busy', '8': 'congestion'}
HANGUP_CAUSE_STATUS = {'1': 'invalid-number', '17': 'busy', '18': 'no-answer', '19': 'no-answer', '21': 'rejected',
                       '28': 'invalid-number', '34': 'congestion', '38': 'congestion', '41': 'congestion', '42': 'congestion'}
FINAL_STATUSES = {'completed', 'failed', 'busy', 'no-answer', 'rejected', 'congestion', 'invalid-number', 'canceled'}
_SAFE_EVENT_KEYS = ('Event', 'ChannelStateDesc', 'Cause', 'Cause-txt', 'Response', 'Reason', 'Uniqueid', 'Linkedid')
_GSM_READY_STATES = {'free', 'ring', 'dialing', 'outgoing', 'incoming', 'active', 'held', 'waiting'}


def _mask_channel(channel: str | None) -> str | None:
    if not channel:
        return channel
    return re.sub(r'\+?\d{6,}', lambda m: mask_phone(m.group(0)), channel)


class AsteriskTelephonyProvider:
    name = 'asterisk'

    def __init__(self, settings, store, *, host_inspector=None, ami_factory=AMIConnection):
        self.settings = settings
        self.store = store
        self.host_inspector = host_inspector or CachedHostInspector()
        self.ami_factory = ami_factory
        self._channels: dict[str, str] = {}
        self._answered: set[str] = set()
        self._lock = threading.Lock()

    def _ami(self, *, events: bool = False) -> AMIConnection:
        s = self.settings
        return self.ami_factory(s.asterisk_ami_host, s.asterisk_ami_port, s.asterisk_ami_username,
                                s.asterisk_ami_secret, timeout=5.0, events=events)

    def channel_for(self, destination: str) -> str:
        template = self.settings.resolve().channel_template
        if not template:
            raise TelephonyError('asterisk_originate', 'no outbound channel configured (TELEPHONY_INTERFACE is empty)')
        return template.replace('{e164}', destination).replace('{digits}', destination.lstrip('+'))

    # Readiness ------------------------------------------------------------------------
    def preflight(self, destination: str | None, *, refresh: bool = False) -> dict:
        s = self.settings
        cfg = s.resolve()
        interface = cfg.telephony_interface
        host: HostReport = self.host_inspector(s.asterisk_wsl_distro, refresh=refresh)
        checks = {
            'WSL_INSTALLED': host.wsl_installed,
            'ASTERISK_INSTALLED': host.asterisk_installed,
            'ASTERISK_RUNNING': False,
            'ASTERISK_VERSION': host.asterisk_version,
            'ASTERISK_CONTROL_READY': False,
            'ASTERISK_DIALPLAN_READY': False,
            'ASTERISK_AUDIO_FORMAT_READY': False,
            'TELEPHONY_INTERFACE': interface or None,
            'TELEPHONY_INTERFACE_READY': False,
            'GSM_MODEM_PRESENT': bool(host.gsm_modems),
            'GSM_REGISTERED': False,
            'SIM_READY': False,
            'SIP_TRUNK_CONFIGURED': interface == 'sip' and bool(s.sip_trunk_host),
            'SIP_TRUNK_READY': False,
            'OUTBOUND_CHANNEL': cfg.channel_template or None,
            'OUTBOUND_CHANNEL_READY': False,
            'OUTBOUND_CALLER_ID': mask_phone(cfg.caller_id) or None,
        }
        details = {'host': list(host.details), 'gsm_modems': host.gsm_modems, 'ami': None, 'interface': None}
        software, interface_blockers = [], []
        control_error = None
        try:
            with self._ami() as ami:
                checks['ASTERISK_RUNNING'] = checks['ASTERISK_INSTALLED'] = True
                checks['ASTERISK_CONTROL_READY'] = True
                core = ami.action('CoreSettings')
                checks['ASTERISK_VERSION'] = core.get('AsteriskVersion') or checks['ASTERISK_VERSION']
                response, events = ami.list_action('ShowDialPlan', Context=s.asterisk_context)
                checks['ASTERISK_DIALPLAN_READY'] = response.get('Response') == 'Success' and any(
                    e.get('Extension') == 's' for e in events)
                checks['ASTERISK_AUDIO_FORMAT_READY'] = ami.action('ModuleCheck', Module='format_wav').get('Response') == 'Success'
                if interface == 'sip':
                    self._check_sip(ami, checks, details)
                elif interface == 'gsm':
                    self._check_gsm(ami, checks, details)
        except AMIError as exc:
            control_error = str(exc)
            details['ami'] = control_error

        if not checks['ASTERISK_INSTALLED']:
            software.append('ASTERISK_NOT_INSTALLED: ' + ('; '.join(host.details) or 'asterisk not found'))
        elif not checks['ASTERISK_RUNNING'] or not checks['ASTERISK_CONTROL_READY']:
            if control_error and 'login_failed' in control_error:
                software.append('ASTERISK_CONTROL_NOT_READY: AMI login rejected (check ASTERISK_AMI_USERNAME/SECRET)')
            else:
                software.append(f'ASTERISK_NOT_RUNNING: AMI {s.asterisk_ami_host}:{s.asterisk_ami_port} unreachable')
        else:
            if not checks['ASTERISK_DIALPLAN_READY']:
                software.append(f'ASTERISK_DIALPLAN_MISSING: context {s.asterisk_context} has no s extension')
            if not checks['ASTERISK_AUDIO_FORMAT_READY']:
                software.append('ASTERISK_WAV_FORMAT_MISSING: format_wav module not loaded')

        checks['TELEPHONY_INTERFACE_READY'] = (
            (interface == 'sip' and checks['SIP_TRUNK_READY'])
            or (interface == 'gsm' and checks['GSM_REGISTERED'] and checks['SIM_READY']))
        checks['OUTBOUND_CHANNEL_READY'] = checks['TELEPHONY_INTERFACE_READY'] and bool(cfg.channel_template)
        if not interface:
            found = f'{len(host.gsm_modems)} GSM modem(s) detected on the host' if host.gsm_modems else 'no GSM/4G modem detected on the host'
            interface_blockers.append(f'NO_EXTERNAL_TELEPHONY_INTERFACE: {found}; no SIP trunk configured '
                                      '(set TELEPHONY_INTERFACE=sip with SIP_TRUNK_* or TELEPHONY_INTERFACE=gsm)')
        elif not checks['ASTERISK_CONTROL_READY']:
            interface_blockers.append(f'TELEPHONY_INTERFACE_UNVERIFIED: {interface} interface cannot be checked while Asterisk is unavailable')
        elif interface == 'sip' and not checks['SIP_TRUNK_READY']:
            interface_blockers.append(f'SIP_TRUNK_NOT_READY: {details.get("interface") or "trunk not registered"}')
        elif interface == 'gsm':
            if not checks['GSM_MODEM_PRESENT']:
                interface_blockers.append('GSM_MODEM_NOT_PRESENT: no cellular modem attached to the host')
            if not checks['GSM_REGISTERED']:
                interface_blockers.append(f'GSM_NOT_REGISTERED: {details.get("interface") or "modem not registered"}')
            if not checks['SIM_READY']:
                interface_blockers.append('SIM_NOT_READY')
        if interface and not cfg.caller_id:
            interface_blockers.append('OUTBOUND_CALLER_ID_NOT_SET: configure the caller identity authorized by the carrier')
        checks['DETAILS'] = details
        checks['SOFTWARE_BLOCKERS'] = software
        checks['INTERFACE_BLOCKERS'] = interface_blockers
        return checks

    def _check_sip(self, ami, checks: dict, details: dict) -> None:
        endpoint = self.settings.sip_trunk_endpoint
        if self.settings.sip_trunk_requires_registration:
            response, events = ami.list_action('PJSIPShowRegistrationsOutbound')
            mine = [e for e in events if e.get('ObjectName') == f'{endpoint}-reg' or e.get('Endpoint') == endpoint]
            status = mine[0].get('Status') if mine else None
            checks['SIP_TRUNK_READY'] = status == 'Registered'
            details['interface'] = f'registration status={status or "not found"}'
        else:
            response = ami.action('PJSIPShowEndpoint', Endpoint=endpoint)
            checks['SIP_TRUNK_READY'] = response.get('Response') == 'Success'
            details['interface'] = f'endpoint {endpoint} ' + ('present' if checks['SIP_TRUNK_READY'] else 'missing')

    def _check_gsm(self, ami, checks: dict, details: dict) -> None:
        device = self.settings.gsm_device
        try:
            output = ami.command(f'dongle show device state {device}')
        except AMIError as exc:
            details['interface'] = f'chan_dongle unavailable ({exc})'
            return
        if 'No such command' in output or not output.strip():
            details['interface'] = 'chan_dongle module not loaded'
            return
        state = re.search(r'^\s*State\s*:\s*(.+)$', output, re.M)
        registration = re.search(r'GSM Registration Status\s*:\s*(.+)$', output, re.M)
        state_text = state.group(1).strip().lower() if state else ''
        checks['GSM_MODEM_PRESENT'] = checks['GSM_MODEM_PRESENT'] or bool(state) and 'not connected' not in state_text
        checks['GSM_REGISTERED'] = bool(registration and registration.group(1).strip().lower().startswith('registered'))
        checks['SIM_READY'] = state_text in _GSM_READY_STATES
        details['interface'] = f'dongle {device} state={state_text or "unknown"} registration=' + (
            registration.group(1).strip() if registration else 'unknown')

    # Calls ------------------------------------------------------------------------------
    def create_outbound_call(self, call_id: str, destination: str) -> dict:
        s = self.settings
        cfg = s.resolve()
        channel = self.channel_for(destination)
        fields = {
            'ActionID': call_id, 'ChannelId': call_id, 'Channel': channel, 'Context': s.asterisk_context,
            'Exten': 's', 'Priority': '1', 'Timeout': str(s.outbound_ring_timeout_seconds * 1000), 'Async': 'true',
            'Variable': [f'MCS_CALL_ID={call_id}', 'MCS_DIRECTION=outbound'],
        }
        if cfg.caller_id:
            fields['CallerID'] = f'"Mouth Care Solutions" <{cfg.caller_id}>'
        try:
            with self._ami() as ami:
                response = ami.action('Originate', **fields)
        except AMIError as exc:
            raise TelephonyError('asterisk_ami', str(exc)) from exc
        if response.get('Response') != 'Success':
            raise TelephonyError('asterisk_originate', str(response.get('Message') or 'originate rejected'))
        return {'call_id': call_id, 'status': 'queued', 'channel': _mask_channel(channel),
                'provider_message': response.get('Message')}

    def get_call_status(self, call_id: str) -> dict | None:
        return self.store.get_call_request(call_id)

    def hangup_call(self, call_id: str) -> bool:
        channel = self._channels.get(call_id)
        if not channel:
            return False
        try:
            with self._ami() as ami:
                return ami.action('Hangup', Channel=channel).get('Response') == 'Success'
        except AMIError:
            return False

    def handle_call_event(self, event: dict) -> None:
        name = event.get('Event')
        call_id = event.get('ActionID') if name == 'OriginateResponse' else event.get('Uniqueid')
        if not call_id or not self.store.get_call_request(call_id):
            return
        safe = {k: event[k] for k in _SAFE_EVENT_KEYS if k in event}
        if event.get('Channel'):
            safe['Channel'] = _mask_channel(event['Channel'])
            with self._lock:
                self._channels[call_id] = event['Channel']
        self.store.add_event(call_id, 'asterisk_event', safe)
        current = (self.store.get_call_request(call_id) or {}).get('status')
        status = None
        if name == 'OriginateResponse':
            if event.get('Response') == 'Success':
                status = 'answered'
            else:
                status = ORIGINATE_FAILURE_REASONS.get(str(event.get('Reason')), 'failed')
        elif name in {'Newchannel', 'Newstate'}:
            desc = (event.get('ChannelStateDesc') or '').lower()
            status = {'ringing': 'ringing', 'ring': 'ringing', 'up': 'answered'}.get(desc)
        elif name == 'Hangup':
            status = 'completed' if call_id in self._answered or current == 'answered' else \
                HANGUP_CAUSE_STATUS.get(str(event.get('Cause')), 'failed')
        if status == 'answered':
            self._answered.add(call_id)
        if status and current not in FINAL_STATUSES and status != current:
            self.store.update_call_request(call_id, status=status)


class AMIEventListener:
    """Long-lived AMI connection that forwards call events to the provider (reconnects)."""

    def __init__(self, provider: AsteriskTelephonyProvider, *, retry_seconds: float = 5.0):
        self.provider = provider
        self.retry_seconds = retry_seconds
        self.connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='ami-events', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self.provider._ami(events=True) as ami:
                    ami.set_timeout(30.0)
                    self.connected = True
                    while not self._stop.is_set():
                        try:
                            message = ami.read_message()
                        except AMIError as exc:
                            if 'timeout' in str(exc):
                                continue
                            raise
                        if 'Event' in message:
                            self.provider.handle_call_event(message)
            except AMIError:
                pass
            finally:
                self.connected = False
            self._stop.wait(self.retry_seconds)

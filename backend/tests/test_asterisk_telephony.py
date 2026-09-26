"""Asterisk provider against a protocol-faithful fake AMI server (no real telephony)."""
import pytest

from backend.app.core.config import Settings
from backend.app.services.call_store import CallStore
from backend.app.telephony import host as host_module
from backend.app.telephony.ami import AMIConnection, AMIError, encode_action, parse_message
from backend.app.telephony.asterisk import AsteriskTelephonyProvider
from backend.app.telephony.asterisk_config import render
from backend.app.telephony.base import TelephonyError
from backend.app.telephony.host import HostReport
from fake_asterisk import FakeAMI

DEST = '+919900000001'
CALLER = '+914000000000'


@pytest.fixture
def ami():
    server = FakeAMI()
    yield server
    server.close()


def make_settings(ami_server, **overrides):
    values = dict(mock_mode=False, app_mode='live', call_provider='asterisk', asterisk_ami_host='127.0.0.1',
                  asterisk_ami_port=ami_server.port if ami_server else 1, asterisk_ami_secret='test-secret',
                  outbound_allowed_destinations=DEST)
    values.update(overrides)
    return Settings(_env_file=None, **values)


def installed_host(*modems):
    def inspector(distro, refresh=False):
        return HostReport(wsl_installed=True, wsl_distros=[distro], distro_present=True, asterisk_installed=True,
                          asterisk_version='Asterisk 20.6.0', gsm_modems=list(modems))
    return inspector


def provider(settings, host=None, store=None):
    return AsteriskTelephonyProvider(settings, store or CallStore(':memory:'), host_inspector=host or installed_host())


SIP = dict(telephony_interface='sip', sip_trunk_host='sip.example.net', sip_trunk_username='clinic',
           sip_trunk_password='trunk-pass', outbound_caller_id=CALLER)


# AMI protocol ---------------------------------------------------------------------------
def test_ami_message_round_trip_and_newline_injection_is_rejected():
    assert parse_message('Response: Success\r\nOutput: a\r\nOutput: b') == {'Response': 'Success', 'Output': ['a', 'b']}
    with pytest.raises(AMIError):
        encode_action('Originate', {'Channel': 'PJSIP/1\r\nAction: Command'})


def test_ami_login_actions_and_commands(ami):
    ami.state['commands']['core show uptime'] = 'System uptime: 5 minutes'
    with AMIConnection('127.0.0.1', ami.port, 'mouthcare', 'test-secret') as conn:
        assert conn.banner.startswith('Asterisk Call Manager')
        assert conn.action('CoreSettings')['AsteriskVersion'] == '20.6.0'
        response, events = conn.list_action('ShowDialPlan', Context='mouthcare-receptionist')
        assert response['Response'] == 'Success' and events[0]['Extension'] == 's'
        assert conn.command('core show uptime') == 'System uptime: 5 minutes'


def test_ami_login_failure_never_exposes_the_secret(ami):
    with pytest.raises(AMIError) as info:
        AMIConnection('127.0.0.1', ami.port, 'mouthcare', 'wrong-secret').connect()
    assert 'login_failed' in str(info.value) and 'wrong-secret' not in str(info.value)


def test_ami_unreachable_is_reported():
    with pytest.raises(AMIError, match='ami_unreachable'):
        AMIConnection('127.0.0.1', 1, 'u', 's', timeout=1).connect()


# Preflight ---------------------------------------------------------------------------
def test_asterisk_not_installed_is_a_software_blocker():
    def not_installed(distro, refresh=False):
        return HostReport(details=['WSL is not installed (run: wsl --install -d Ubuntu, then reboot)'])
    result = provider(make_settings(None), host=not_installed).preflight(DEST)
    assert result['ASTERISK_INSTALLED'] is False and result['ASTERISK_RUNNING'] is False
    assert any(b.startswith('ASTERISK_NOT_INSTALLED') and 'wsl --install' in b for b in result['SOFTWARE_BLOCKERS'])
    assert any(b.startswith('NO_EXTERNAL_TELEPHONY_INTERFACE') for b in result['INTERFACE_BLOCKERS'])


def test_installed_but_stopped_asterisk_is_reported(ami):
    port = ami.port
    ami.close()
    result = provider(make_settings(None, asterisk_ami_port=port)).preflight(DEST)
    assert result['ASTERISK_INSTALLED'] is True and result['ASTERISK_RUNNING'] is False
    assert any(b.startswith('ASTERISK_NOT_RUNNING') for b in result['SOFTWARE_BLOCKERS'])


def test_rejected_ami_login_is_a_control_blocker(ami):
    result = provider(make_settings(ami, asterisk_ami_secret='wrong')).preflight(DEST)
    assert result['ASTERISK_CONTROL_READY'] is False
    assert any(b.startswith('ASTERISK_CONTROL_NOT_READY') for b in result['SOFTWARE_BLOCKERS'])


def test_running_asterisk_without_a_phone_line_is_software_ready_but_not_live(ami):
    result = provider(make_settings(ami)).preflight(DEST)
    assert result['ASTERISK_RUNNING'] and result['ASTERISK_CONTROL_READY'] and result['ASTERISK_DIALPLAN_READY']
    assert result['ASTERISK_AUDIO_FORMAT_READY'] and result['ASTERISK_VERSION'] == '20.6.0'
    assert result['SOFTWARE_BLOCKERS'] == []
    assert result['TELEPHONY_INTERFACE_READY'] is False and result['OUTBOUND_CHANNEL_READY'] is False
    assert result['INTERFACE_BLOCKERS'][0].startswith('NO_EXTERNAL_TELEPHONY_INTERFACE: no GSM/4G modem detected')


@pytest.mark.parametrize('state,blocker', [
    ({'contexts': set()}, 'ASTERISK_DIALPLAN_MISSING'),
    ({'modules': set()}, 'ASTERISK_WAV_FORMAT_MISSING'),
])
def test_missing_dialplan_or_wav_support_blocks(ami, state, blocker):
    ami.state.update(state)
    result = provider(make_settings(ami)).preflight(DEST)
    assert any(b.startswith(blocker) for b in result['SOFTWARE_BLOCKERS'])


def test_registered_sip_trunk_makes_the_outbound_channel_ready(ami):
    ami.state['registrations'] = [{'ObjectName': 'pstn-trunk-reg', 'Status': 'Registered'}]
    result = provider(make_settings(ami, **SIP)).preflight(DEST)
    assert result['SIP_TRUNK_CONFIGURED'] and result['SIP_TRUNK_READY'] and result['TELEPHONY_INTERFACE_READY']
    assert result['OUTBOUND_CHANNEL_READY'] and result['INTERFACE_BLOCKERS'] == []


@pytest.mark.parametrize('registrations', [[], [{'ObjectName': 'pstn-trunk-reg', 'Status': 'Rejected'}],
                                           [{'ObjectName': 'other-trunk-reg', 'Status': 'Registered'}]])
def test_unregistered_sip_trunk_is_an_interface_blocker(ami, registrations):
    ami.state['registrations'] = registrations
    result = provider(make_settings(ami, **SIP)).preflight(DEST)
    assert result['SIP_TRUNK_READY'] is False
    assert any(b.startswith('SIP_TRUNK_NOT_READY') for b in result['INTERFACE_BLOCKERS'])


def test_missing_caller_id_is_an_interface_blocker(ami):
    ami.state['registrations'] = [{'ObjectName': 'pstn-trunk-reg', 'Status': 'Registered'}]
    result = provider(make_settings(ami, **{**SIP, 'outbound_caller_id': ''})).preflight(DEST)
    assert any(b.startswith('OUTBOUND_CALLER_ID_NOT_SET') for b in result['INTERFACE_BLOCKERS'])


GSM_OK = ('-------------- Status -------------\n  Device                  : dongle0\n  State                   : Free\n'
          '  GSM Registration Status : Registered, home network\n')


def test_registered_gsm_modem_with_ready_sim_is_ready(ami):
    ami.state['commands']['dongle show device state dongle0'] = GSM_OK
    s = make_settings(ami, telephony_interface='gsm', outbound_caller_id=CALLER)
    result = provider(s, host=installed_host({'name': 'HUAWEI Mobile Connect - Modem'})).preflight(DEST)
    assert result['GSM_MODEM_PRESENT'] and result['GSM_REGISTERED'] and result['SIM_READY']
    assert result['TELEPHONY_INTERFACE_READY'] and result['INTERFACE_BLOCKERS'] == []


@pytest.mark.parametrize('output,blockers', [
    ('No such command', {'GSM_MODEM_NOT_PRESENT', 'GSM_NOT_REGISTERED', 'SIM_NOT_READY'}),
    ('  State                   : SIM not ready\n  GSM Registration Status : Not registered\n', {'GSM_NOT_REGISTERED', 'SIM_NOT_READY'}),
])
def test_gsm_problems_are_interface_blockers(ami, output, blockers):
    ami.state['commands']['dongle show device state dongle0'] = output
    result = provider(make_settings(ami, telephony_interface='gsm', outbound_caller_id=CALLER)).preflight(DEST)
    assert result['TELEPHONY_INTERFACE_READY'] is False
    assert blockers <= {b.split(':')[0] for b in result['INTERFACE_BLOCKERS']}


# Origination --------------------------------------------------------------------------
def test_originate_uses_the_configured_channel_context_and_call_id(ami):
    p = provider(make_settings(ami, **SIP))
    result = p.create_outbound_call('CR-TEST-1', DEST)
    originate = [a for a in ami.actions if a.get('Action') == 'Originate'][0]
    assert originate['Channel'] == f'PJSIP/{DEST}@pstn-trunk'
    assert originate['Context'] == 'mouthcare-receptionist' and originate['Exten'] == 's'
    assert originate['ChannelId'] == originate['ActionID'] == 'CR-TEST-1' and originate['Async'] == 'true'
    assert originate['Variable'] == ['MCS_CALL_ID=CR-TEST-1', 'MCS_DIRECTION=outbound']
    assert originate['CallerID'] == f'"Mouth Care Solutions" <{CALLER}>'
    assert result['status'] == 'queued' and DEST not in result['channel']


def test_originate_rejection_and_unreachable_ami_raise_staged_errors(ami):
    ami.state['originate_response'] = 'Extension does not exist.'
    with pytest.raises(TelephonyError) as info:
        provider(make_settings(ami, **SIP)).create_outbound_call('CR-TEST-2', DEST)
    assert info.value.stage == 'asterisk_originate' and 'does not exist' in info.value.message
    with pytest.raises(TelephonyError) as info:
        provider(make_settings(None, **SIP)).create_outbound_call('CR-TEST-3', DEST)
    assert info.value.stage == 'asterisk_ami'


def test_originate_without_a_configured_interface_is_refused(ami):
    with pytest.raises(TelephonyError, match='no outbound channel configured'):
        provider(make_settings(ami)).create_outbound_call('CR-TEST-4', DEST)


# Call events --------------------------------------------------------------------------
def call(store):
    request = store.create_call_request(name='t', destination=DEST, preferred_window='Now', topic='t', mode='live')
    store.update_call_request(request['request_id'], call_id=request['request_id'], status='queued')
    return request['request_id']


def test_events_drive_the_call_record_to_a_real_final_status():
    store = CallStore(':memory:')
    p = provider(make_settings(None, **SIP), store=store)
    cid = call(store)
    channel = f'PJSIP/pstn-trunk-00000001;{DEST}'
    p.handle_call_event({'Event': 'Newchannel', 'Uniqueid': cid, 'Channel': channel, 'ChannelStateDesc': 'Down'})
    p.handle_call_event({'Event': 'Newstate', 'Uniqueid': cid, 'ChannelStateDesc': 'Ringing'})
    assert store.get_call_request(cid)['status'] == 'ringing'
    p.handle_call_event({'Event': 'Newstate', 'Uniqueid': cid, 'ChannelStateDesc': 'Up'})
    assert store.get_call_request(cid)['status'] == 'answered'
    p.handle_call_event({'Event': 'Hangup', 'Uniqueid': cid, 'Cause': '16', 'Cause-txt': 'Normal Clearing'})
    assert store.get_call_request(cid)['status'] == 'completed'
    p.handle_call_event({'Event': 'Newstate', 'Uniqueid': cid, 'ChannelStateDesc': 'Ringing'})
    assert store.get_call_request(cid)['status'] == 'completed'
    events = store.events(cid)
    assert [e['data']['Event'] for e in events] == ['Newchannel', 'Newstate', 'Newstate', 'Hangup', 'Newstate']
    assert all(DEST not in str(e['data']) for e in events)


@pytest.mark.parametrize('event,status', [
    ({'Event': 'OriginateResponse', 'Response': 'Failure', 'Reason': '5'}, 'busy'),
    ({'Event': 'OriginateResponse', 'Response': 'Failure', 'Reason': '3'}, 'no-answer'),
    ({'Event': 'OriginateResponse', 'Response': 'Failure', 'Reason': '8'}, 'congestion'),
    ({'Event': 'Hangup', 'Cause': '17'}, 'busy'),
    ({'Event': 'Hangup', 'Cause': '1'}, 'invalid-number'),
])
def test_unanswered_outcomes_use_real_carrier_causes(event, status):
    store = CallStore(':memory:')
    p = provider(make_settings(None, **SIP), store=store)
    cid = call(store)
    key = 'ActionID' if event['Event'] == 'OriginateResponse' else 'Uniqueid'
    p.handle_call_event({**event, key: cid})
    assert store.get_call_request(cid)['status'] == status


def test_events_for_unknown_calls_are_ignored():
    store = CallStore(':memory:')
    provider(make_settings(None), store=store).handle_call_event({'Event': 'Hangup', 'Uniqueid': 'someone-else', 'Cause': '16'})
    assert store.events('someone-else') == []


def test_hangup_uses_the_channel_learned_from_events(ami):
    store = CallStore(':memory:')
    p = provider(make_settings(ami, **SIP), store=store)
    cid = call(store)
    assert p.hangup_call(cid) is False
    p.handle_call_event({'Event': 'Newchannel', 'Uniqueid': cid, 'Channel': 'PJSIP/pstn-trunk-0000002a'})
    assert p.hangup_call(cid) is True
    assert [a for a in ami.actions if a.get('Action') == 'Hangup'][0]['Channel'] == 'PJSIP/pstn-trunk-0000002a'


# Rendered Asterisk configuration -------------------------------------------------------
def test_rendered_config_keeps_control_interfaces_local_and_minimal():
    files = render(make_settings(None, **SIP))
    manager = files['manager.conf']
    assert 'bindaddr = 127.0.0.1' in manager and 'webenabled = no' in manager
    assert 'deny = 0.0.0.0/0.0.0.0' in manager and 'permit = 127.0.0.1/255.255.255.255' in manager
    assert files['http.conf'].strip().endswith('enabled = no') and files['ari.conf'].strip().endswith('enabled = no')
    assert 'noload => res_ari.so' in files['modules.conf'] and 'noload => chan_iax2.so' in files['modules.conf']
    assert 'exten => s,1,' in files['extensions.conf']
    assert 'AGI(agi://${MCS_AGI_HOST}:${MCS_AGI_PORT}/receptionist,${MCS_CALL_ID},' in files['extensions.conf']
    assert '[pstn-trunk-reg]' in files['pjsip.conf'] and 'type = registration' in files['pjsip.conf']
    secrets_in = {name for name, text in files.items() if 'test-secret' in text or 'trunk-pass' in text}
    assert secrets_in == {'manager.conf', 'pjsip.conf'}
    assert 'dongle.conf' not in files and 'noload => chan_dongle.so' in files['modules.conf']


def test_no_trunk_is_rendered_without_sip_credentials_and_gsm_gets_dongle_config():
    assert 'type = endpoint' not in render(make_settings(None))['pjsip.conf']
    gsm = render(make_settings(None, telephony_interface='gsm'))
    assert '[dongle0]' in gsm['dongle.conf'] and 'noload => chan_dongle.so' not in gsm['modules.conf']
    assert '+1234567890' not in gsm['dongle.conf']


# Host detection -----------------------------------------------------------------------
def test_host_detection_reports_missing_wsl(monkeypatch):
    monkeypatch.setattr(host_module.sys, 'platform', 'win32')
    monkeypatch.setattr(host_module.shutil, 'which', lambda name: 'C:/Windows/System32/wsl.exe')
    monkeypatch.setattr(host_module, '_run', lambda args, timeout=15.0: (1, 'The Windows Subsystem for Linux is not installed.'))
    report = host_module.inspect_wsl('Ubuntu')
    assert report.wsl_installed is False and report.asterisk_installed is False
    assert 'wsl --install -d Ubuntu' in report.details[0]


def test_host_detection_finds_asterisk_in_the_distro(monkeypatch):
    monkeypatch.setattr(host_module.sys, 'platform', 'win32')
    monkeypatch.setattr(host_module.shutil, 'which', lambda name: 'wsl.exe')
    replies = iter([(0, 'Ubuntu\ndocker-desktop'), (0, 'Asterisk 20.6.0')])
    monkeypatch.setattr(host_module, '_run', lambda args, timeout=15.0: next(replies))
    report = host_module.inspect_wsl('Ubuntu')
    assert report.wsl_installed and report.distro_present and report.asterisk_installed
    assert report.asterisk_version == 'Asterisk 20.6.0'

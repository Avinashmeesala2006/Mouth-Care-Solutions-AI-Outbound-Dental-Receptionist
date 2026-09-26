from fastapi.testclient import TestClient
from starlette.datastructures import URL
from backend.app.main import app
from backend.app.auth import hash_password, verify_password
import base64, hashlib, hmac, io, wave

client = TestClient(app)

def test_health_and_readiness():
    assert client.get('/health').json()['status'] == 'ok'
    assert client.get('/ready').json()['status'] == 'ready'

def test_configured_phone_is_current():
    data = client.get('/api/admin/config', headers={'Authorization':'Bearer demo-admin-token'}).json()
    assert data['clinic']['phone'] == '+91 96423 40630'

def test_address_and_services():
    assert 'Vijayawada' in client.post('/api/knowledge/search', json={'query':'address'}).json()['text']
    assert 'Orthodontic' in client.post('/api/knowledge/search', json={'query':'services'}).json()['text']

def test_emergency_is_non_diagnostic():
    text = client.post('/api/agent/session', json={'message':'I have severe swelling emergency'}).json()['text']
    assert 'cannot diagnose' in text and 'urgent' in text

def test_human_callback():
    r = client.post('/api/callback', json={'patient_contact':'999','topic':'human help','preferred_window':'morning'})
    assert r.status_code == 200 and r.json()['status'] == 'queued'

def test_password_hashing():
    encoded = hash_password('correct horse battery staple')
    assert verify_password('correct horse battery staple', encoded)
    assert not verify_password('wrong', encoded)

def test_provider_interfaces_are_demo_safe():
    from backend.app.adapters.providers import MockVoiceProvider, MockNotificationProvider
    assert MockVoiceProvider().start_session('s')['status'] == 'started'
    assert MockNotificationProvider().send('email','x','hello')['status'] == 'queued'

def test_usage_thresholds():
    from backend.app.services.usage import UsageMeter
    meter = UsageMeter()
    assert meter.record(2100)['threshold'] == '70%'
    assert meter.record(750)['threshold'] == '95%'
    assert meter.record(150)['billable_calling_allowed'] is False


def test_live_llm_does_not_fallback(monkeypatch):
    from backend.app.core.config import settings
    from backend.app.adapters.llm import get_llm_provider
    monkeypatch.setattr(settings, 'mock_mode', False)
    monkeypatch.setattr(settings, 'llm_api_key', '')
    monkeypatch.setattr(settings, 'openai_api_key', '')
    try:
        get_llm_provider()
        assert False, 'LIVE MODE must not silently use mock LLM'
    except RuntimeError as exc:
        assert 'refusing mock fallback' in str(exc)
    finally:
        monkeypatch.setattr(settings, 'mock_mode', True)

def test_demo_admin_login_and_signed_session():
    r = client.post('/api/admin/login', json={'email':'demo@example.test','password':'demo-password'})
    assert r.status_code == 200 and r.json()['role'] == 'admin'
    token = r.json()['access_token']
    assert client.get('/api/admin/config', headers={'Authorization':'Bearer '+token}).status_code == 200

def test_invalid_admin_login_and_forbidden_role():
    assert client.post('/api/admin/login', json={'email':'demo@example.test','password':'wrong-password'}).status_code == 401
    assert client.get('/api/admin/config', headers={'Authorization':'Bearer mcs.bad.bad'}).status_code == 403


def test_root_and_favicon_are_handled():
    assert client.get('/').status_code == 200
    assert '/demo' in client.get('/').text
    assert client.get('/favicon.ico').status_code == 204


def test_call_request_and_public_status_are_safe(monkeypatch):
    from backend.app import main
    monkeypatch.delenv('CALL_PROVIDER', raising=False)
    monkeypatch.setattr(main.settings, 'call_provider', 'mock')
    status=client.get('/api/status').json()
    assert status['mode']=='demo' and 'clinic_phone' in status and 'ASTERISK_AMI_SECRET' not in str(status)
    r=client.post('/api/calls/request',json={'name':'QA Patient','patient_contact':'+919999999999','preferred_window':'Afternoon','topic':'Appointment','consent':True})
    assert r.status_code==200 and r.json()['status']=='queued'
    request_id=r.json()['request_id']
    assert client.get('/api/calls/'+request_id).json()['request_id']==request_id
    assert client.post('/api/calls/request',json={'name':'QA Patient','patient_contact':'+919999999999','preferred_window':'Afternoon','topic':'Appointment','consent':False}).status_code==400



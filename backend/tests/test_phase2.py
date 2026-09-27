import pytest
from fastapi.testclient import TestClient

from backend.app.auth import hash_password, verify_password
from backend.app.main import app

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

def test_notification_seam_is_demo_safe():
    from backend.app.adapters.providers import MockNotificationProvider
    assert MockNotificationProvider().send('email','x','hello')['status'] == 'queued'

def test_usage_thresholds():
    from backend.app.services.usage import threshold_label
    assert threshold_label(70.0) == '70%'
    assert threshold_label(95.0) == '95%'
    assert threshold_label(100.0) == '100%'
    usage = client.get('/api/usage').json()
    assert usage['quota_seconds'] == 180000 and usage['quota_hours'] == 50 and usage['threshold'] == 'below_70%'


def test_production_llm_does_not_fallback(monkeypatch):
    from backend.app.adapters.llm import get_llm_provider
    from backend.app.core.config import settings
    monkeypatch.setattr(settings, 'app_mode', 'production')
    monkeypatch.setattr(settings, 'llm_api_key', '')
    monkeypatch.setattr(settings, 'openai_api_key', '')
    try:
        get_llm_provider()
        pytest.fail('production must not silently use a mock LLM')
    except RuntimeError as exc:
        assert 'refusing mock fallback' in str(exc)

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


def test_call_request_and_public_status_are_safe():
    status=client.get('/api/status').json()
    assert status['mode']=='demo' and status['telephony']=='disabled' and 'clinic_phone' in status
    assert 'SECRET' not in str(status) and 'private' not in str(status).lower()
    r=client.post('/api/calls/request',json={'name':'QA Patient','patient_contact':'+12025550143','preferred_window':'Afternoon','topic':'Appointment','consent':True})
    assert r.status_code==200 and r.json()['status']=='queued' and r.json()['mode']=='demo'
    assert client.post('/api/calls/request',json={'name':'QA Patient','patient_contact':'+12025550143','preferred_window':'Afternoon','topic':'Appointment','consent':False}).status_code==400
    assert client.get('/api/calls/not-a-call').status_code==404



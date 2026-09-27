"""Call-state repository contract, run against the in-memory store and real PostgreSQL."""
import threading
from datetime import timedelta

import pytest
from conftest import OTHER, PATIENT

from backend.app.db.base import CallPolicy
from backend.app.db.memory import InMemoryCallRepository
from backend.app.services.call_state import CallStatus

POLICY = CallPolicy(quota_seconds=3600, max_concurrent=2, reserve_seconds=900, stale_after_seconds=3600, rate_limit_seconds=0)


@pytest.fixture(params=['memory', 'postgresql'])
def repo(request):
    if request.param == 'memory':
        yield InMemoryCallRepository()
        return
    url = request.getfixturevalue('pg_url')
    from backend.app.db.postgres import PostgresCallRepository
    store = PostgresCallRepository(url, min_size=1, max_size=8)
    store.open()
    yield store
    store.close()


def reserve(repo, phone=PATIENT, policy=POLICY, key=None, source='admin_api'):
    return repo.reserve_outbound_call(customer_number=phone, from_number='+12025550100', source=source, requested_by='test',
                                      idempotency_key=key, policy=policy)


def test_outbound_checks_run_in_the_required_order(repo):
    assert reserve(repo).blocked == 'consent_missing'
    repo.record_consent(PATIENT, source='test')
    repo.add_dnc(PATIENT, reason='asked by phone', source='test')
    assert reserve(repo).blocked == 'do_not_call'
    assert repo.remove_dnc(PATIENT)
    repo.record_opt_out(PATIENT, source='test')
    assert reserve(repo).blocked == 'opted_out'
    assert repo.compliance_status(PATIENT) == {'consent': False, 'do_not_call': False, 'opted_out': True}
    assert repo.clear_opt_out(PATIENT, reason='new written consent received at the clinic desk') == 1
    repo.record_consent(PATIENT, source='test')
    result = reserve(repo)
    assert result.blocked is None and result.call.status == 'CREATED' and result.call.reserved_seconds == 900


def test_idempotency_key_returns_the_same_call(repo):
    repo.record_consent(PATIENT, source='test')
    first = reserve(repo, key='idem-key-0001')
    second = reserve(repo, key='idem-key-0001')
    assert second.replayed and second.call.id == first.call.id
    assert repo.quota_usage(POLICY).active_calls == 1


def test_quota_and_concurrency_limits(repo):
    for phone in (PATIENT, OTHER):
        repo.record_consent(phone, source='test')
    policy = CallPolicy(quota_seconds=2000, max_concurrent=5, reserve_seconds=900, stale_after_seconds=3600)
    assert reserve(repo, policy=policy).blocked is None
    assert reserve(repo, OTHER, policy=policy).blocked is None
    assert reserve(repo, policy=policy).blocked == 'quota_exhausted'          # 900 + 900 + 900 > 2000
    policy = CallPolicy(quota_seconds=10**6, max_concurrent=2, reserve_seconds=900, stale_after_seconds=3600)
    assert reserve(repo, policy=policy).blocked == 'capacity_reached'


def test_rate_limit_applies_to_dialed_calls_only(repo):
    repo.record_consent(PATIENT, source='test')
    policy = CallPolicy(quota_seconds=10**6, max_concurrent=5, reserve_seconds=60, stale_after_seconds=3600, rate_limit_seconds=300)
    call = reserve(repo, policy=policy).call
    assert reserve(repo, policy=policy).blocked == 'rate_limited'
    repo.transition(call.id, CallStatus.FAILED, reason='invalid_request')       # never dialed: retry allowed
    assert reserve(repo, policy=policy).blocked is None


def test_lifecycle_is_monotonic_and_completion_consumes_quota(repo):
    repo.record_consent(PATIENT, source='test')
    call = reserve(repo).call
    repo.attach_provider_call(call.id, provider_call_id='CA-1', provider_conversation_id='CON-1')
    for status in (CallStatus.DIALING, CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.CONNECTED, CallStatus.ACTIVE):
        assert repo.transition(call.id, status)[1]
    assert repo.transition(call.id, CallStatus.RINGING)[1] is False               # late event never regresses
    assert repo.get_call_by_provider_id('CA-1').status == 'ACTIVE'
    assert repo.get_call_by_conversation_id('CON-1').id == call.id
    done, changed = repo.transition(call.id, CallStatus.COMPLETED, reason='completed', duration_seconds=125, price='0.01')
    assert changed and done.duration_seconds == 125 and done.end_time and done.answer_time and done.connected_time
    usage = repo.quota_usage(POLICY)
    assert (usage.consumed_seconds, usage.reserved_seconds, usage.active_calls) == (125, 0, 0)
    assert repo.transition(call.id, CallStatus.FAILED)[1] is False               # terminal is final


def test_late_duration_is_recorded_for_an_already_terminal_call(repo):
    repo.record_consent(PATIENT, source='test')
    call = reserve(repo).call
    repo.transition(call.id, CallStatus.FAILED, reason='answer_webhook_failed')
    updated, changed = repo.transition(call.id, CallStatus.NO_ANSWER, duration_seconds=9)
    assert not changed and updated.status == 'FAILED' and updated.duration_seconds == 9


def test_stale_calls_expire_and_release_capacity(repo):
    repo.record_consent(PATIENT, source='test')
    call = reserve(repo).call
    repo.transition(call.id, CallStatus.ANSWERED)
    if isinstance(repo, InMemoryCallRepository):
        repo._calls[call.id].created_at -= timedelta(hours=2)
    else:
        with repo._conn() as conn:
            conn.execute("UPDATE calls SET created_at = now() - interval '2 hours' WHERE id = %s", (call.id,))
    assert repo.expire_stale_calls(3600) == 1
    expired = repo.get_call(call.id)
    assert expired.status == 'TIMEOUT' and expired.termination_reason == 'stale_no_final_event'
    assert expired.duration_seconds == 900        # answered but never finalized: counted conservatively
    assert repo.quota_usage(POLICY).active_calls == 0


def test_provider_events_are_deduplicated(repo):
    assert repo.add_event(None, 'twilio_ringing', {'status': 'ringing'}, provider_call_id='CA-1', dedupe_key='twilio:CA-1:ringing:t1')
    assert not repo.add_event(None, 'twilio_ringing', {'status': 'ringing'}, provider_call_id='CA-1', dedupe_key='twilio:CA-1:ringing:t1')
    assert repo.add_event(None, 'note', {'n': 1}) and repo.add_event(None, 'note', {'n': 1})


def test_inbound_calls_are_created_once_and_respect_capacity(repo):
    policy = CallPolicy(quota_seconds=10**6, max_concurrent=1, reserve_seconds=900, stale_after_seconds=3600)
    first = repo.create_inbound_call(provider_call_id='in-1', provider_conversation_id='CON-in-1', customer_number=PATIENT,
                                     from_number='+12025550100', policy=policy)
    assert first.blocked is None and first.call.status == 'ANSWERED' and first.call.direction == 'inbound'
    again = repo.create_inbound_call(provider_call_id='in-1', provider_conversation_id='CON-in-1', customer_number=PATIENT,
                                     from_number=None, policy=policy)
    assert again.replayed and again.call.id == first.call.id
    busy = repo.create_inbound_call(provider_call_id='in-2', provider_conversation_id=None, customer_number=OTHER, from_number=None,
                                    policy=policy)
    assert busy.blocked == 'capacity_reached' and busy.call.status == 'FAILED'


def test_sessions_turns_and_captured_requests(repo):
    repo.record_consent(PATIENT, source='test')
    call = reserve(repo).call
    repo.save_session(call.id, {'stage': 'collect_time', 'appointment': {'preferred_date': 'Monday'}})
    repo.save_session(call.id, {'stage': 'confirm', 'appointment': {'preferred_date': 'Monday'}})
    assert repo.load_session(call.id)['stage'] == 'confirm'
    repo.add_turn(call.id, {'turn_index': 1, 'transcript': 'monday', 'intent': 'appointment_preferred_date',
                            'prompts': ['ask_preferred_time'], 'stt_latency_ms': 350, 'total_turn_latency_ms': 1200})
    assert repo.turns(call.id)[0]['stt_latency_ms'] == 350
    appointment = repo.add_appointment_request(call_id=call.id, caller=PATIENT, details={'patient_name': 'Asha', 'reason': 'cleaning'})
    callback = repo.add_callback(source='phone', contact=PATIENT, topic='price question', preferred_window='To be arranged',
                                 call_id=call.id)
    assert appointment['id'].startswith('AR-') and repo.appointment_requests()[0]['patient_name'] == 'Asha'
    assert callback['status'] == 'queued' and repo.callbacks()[0]['topic'] == 'price question'


def test_concurrent_reservations_never_exceed_the_limit(repo):
    for i in range(12):
        repo.record_consent(f'+120255501{i:02d}', source='test')
    policy = CallPolicy(quota_seconds=10**6, max_concurrent=3, reserve_seconds=60, stale_after_seconds=3600)
    results, barrier = [], threading.Barrier(12)

    def worker(i):
        barrier.wait()
        results.append(reserve(repo, f'+120255501{i:02d}', policy=policy))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r.blocked is None) == 3
    assert {r.blocked for r in results if r.blocked} == {'capacity_reached'}
    assert repo.quota_usage(policy).active_calls == 3

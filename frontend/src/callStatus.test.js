import { describe, expect, it } from 'vitest'
import { NOW_WINDOW, callOutcomeMessage, callReadinessLabel, callRequestTarget, newId, readinessFromStatus, runtimeStatusMessage, statusRows } from './callStatus'

describe('call request routing', () => {
  it('only the explicit "now" window places an automated call', () => {
    expect(callRequestTarget(NOW_WINDOW)).toBe('/api/calls/request')
    expect(callRequestTarget('Evening')).toBe('/api/callback')
    expect(callRequestTarget('')).toBe('/api/callback')
  })
})

describe('call outcome messages', () => {
  it('claims a ringing phone only for a live call with a real call id', () => {
    expect(callOutcomeMessage(200, { call_id: 'CR-20260926-AB12CD34', mode: 'live' })).toMatch(/ring/)
    expect(callOutcomeMessage(200, { mode: 'demo', request_id: 'CR-1' })).toMatch(/no real call/)
    expect(callOutcomeMessage(200, { id: 'CB-1', status: 'queued' })).not.toMatch(/ring/)
    expect(callOutcomeMessage(200, { call_id: 'CR-1' })).not.toMatch(/ring/)
  })
  it('explains blocked live calls without leaking internals', () => {
    const blocked = callOutcomeMessage(503, { detail: { error: 'live_call_preflight_failed', blockers: ['provider_disabled'] } })
    expect(blocked).toMatch(/not available/)
    expect(blocked).not.toMatch(/INTERFACE|provider_disabled/)
    expect(callOutcomeMessage(403, { detail: { error: 'destination_not_allowed' } })).toMatch(/not enabled/)
    expect(callOutcomeMessage(429, {})).toMatch(/recently/)
    expect(callOutcomeMessage(422, {})).toMatch(/valid phone/)
  })
})

describe('runtime readiness', () => {
  it('never labels live calls ready without an affirmative preflight result', () => {
    expect(callReadinessLabel('live', null)).toBe('Live readiness unknown')
    expect(callReadinessLabel('live', { LIVE_CALL_ALLOWED: false })).toBe('Live calls blocked')
    expect(callReadinessLabel('live', { LIVE_CALL_ALLOWED: 'true' })).toBe('Live calls blocked')
    expect(callReadinessLabel('live', { LIVE_CALL_ALLOWED: true })).toBe('Live calls ready')
    expect(callReadinessLabel('demo', { LIVE_CALL_ALLOWED: true })).toBe('Demo mode')
  })

  it('includes current provider, pack state, and live blockers', () => {
    const message = runtimeStatusMessage({
      backend: 'healthy', mode: 'live', provider: 'twilio',
      preflight: { LIVE_CALL_ALLOWED: false, VOICE_PACK_VALID: false, BOOKING_REPOSITORY_READY: false, BLOCKERS: ['TWILIO_DISABLED'] },
    })
    expect(message).toContain('Provider twilio')
    expect(message).toContain('Voice pack not ready')
    expect(message).toContain('Scheduling unavailable')
    expect(message).toContain('Blockers: TWILIO_DISABLED')
  })
})

describe('status panel rows', () => {
  const live = {
    mode: 'live', telephony: 'twilio', fish_speech_ready: true, voice_pack_valid: true, booking_repository_ready: false,
    call_state_store_ready: true,
    readiness: { SOFTWARE_READY_FOR_LIVE_CALL: false, LIVE_CALL_ALLOWED: false, BLOCKERS: ['TWILIO_NOT_CONFIGURED'] },
  }
  it('shows Twilio, the voice pipeline and software readiness separately from live-call permission', () => {
    const rows = Object.fromEntries(statusRows('healthy', live))
    expect(rows.Telephony).toBe('Twilio')
    expect(rows['Voice (Fish Speech)']).toBe('Verified voice pack')
    expect(rows['Call records']).toBe('Ready')
    expect(rows['Software ready']).toBe('No')
    expect(rows['Live calls']).toBe('Blocked')
    expect(rows.Scheduling).toBe('Not connected (requests only)')
    expect(JSON.stringify(rows)).not.toMatch(/legacy_provider|trial/i)
  })
  it('reports unknown readiness instead of guessing when none is available', () => {
    const rows = Object.fromEntries(statusRows('healthy', { ...live, readiness: null }))
    expect(rows['Live calls']).toBe('Checking…')
    expect(readinessFromStatus({ ...live, readiness: null }).known).toBe(false)
  })
  it('never shows live rows in demo mode', () => {
    const rows = Object.fromEntries(statusRows('healthy', { mode: 'demo', telephony: 'disabled', booking_repository_ready: true }))
    expect(rows.Mode).toBe('Demo (no real calls)')
    expect(rows.Telephony).toBe('Disabled')
    expect(rows['Live calls']).toBeUndefined()
  })
})

describe('outbound refusal messages', () => {
  it('explains compliance, capacity and ambiguous outcomes without internals', () => {
    expect(callOutcomeMessage(403, { detail: { error: 'opted_out' } })).toMatch(/asked not to receive/)
    expect(callOutcomeMessage(403, { detail: { error: 'consent_missing' } })).toMatch(/not enabled/)
    expect(callOutcomeMessage(429, { detail: { error: 'capacity_reached' } })).toMatch(/lines are busy/)
    expect(callOutcomeMessage(429, { detail: { error: 'rate_limited' } })).toMatch(/recently/)
    expect(callOutcomeMessage(502, { detail: { error: 'twilio_outcome_unknown' } })).toMatch(/could not confirm/)
    expect(callOutcomeMessage(503, { detail: { error: 'voice_pipeline_not_ready' } })).toMatch(/not available/)
  })
})

describe('ids', () => {
  it('produces unique ids even without crypto.randomUUID', () => {
    expect(newId()).not.toBe(newId())
  })
})

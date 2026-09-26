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
    const blocked = callOutcomeMessage(503, { detail: { error: 'live_call_preflight_failed', blockers: ['NO_EXTERNAL_TELEPHONY_INTERFACE'] } })
    expect(blocked).toMatch(/not available/)
    expect(blocked).not.toMatch(/INTERFACE|Asterisk/)
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
      backend: 'healthy', mode: 'live', provider: 'asterisk',
      preflight: { LIVE_CALL_ALLOWED: false, VOICE_PACK_VALID: false, BOOKING_REPOSITORY_READY: false, BLOCKERS: ['NO_EXTERNAL_TELEPHONY_INTERFACE'] },
    })
    expect(message).toContain('Provider asterisk')
    expect(message).toContain('Voice pack not ready')
    expect(message).toContain('Scheduling unavailable')
    expect(message).toContain('Blockers: NO_EXTERNAL_TELEPHONY_INTERFACE')
  })
})

describe('status panel rows', () => {
  const live = {
    mode: 'live', telephony: 'asterisk', fish_speech_ready: true, voice_pack_valid: true, booking_repository_ready: false,
    readiness: {
      SOFTWARE_READY_FOR_LIVE_CALL: true, LIVE_CALL_ALLOWED: false, ASTERISK_INSTALLED: true, ASTERISK_RUNNING: true,
      ASTERISK_VERSION: '20.6.0', TELEPHONY_INTERFACE: null, TELEPHONY_INTERFACE_READY: false,
      BLOCKERS: ['NO_EXTERNAL_TELEPHONY_INTERFACE'],
    },
  }
  it('shows Asterisk, the phone line and software readiness separately from live-call permission', () => {
    const rows = Object.fromEntries(statusRows('healthy', live))
    expect(rows.Telephony).toBe('Asterisk')
    expect(rows.Asterisk).toBe('Running (20.6.0)')
    expect(rows['Phone line']).toBe('None connected')
    expect(rows['Software ready']).toBe('Yes')
    expect(rows['Live calls']).toBe('Blocked')
    expect(rows.Scheduling).toBe('Not connected (requests only)')
    expect(JSON.stringify(rows)).not.toMatch(/twilio|trial/i)
  })
  it('reports a registered trunk and a missing Asterisk truthfully', () => {
    const trunk = { ...live, readiness: { ...live.readiness, TELEPHONY_INTERFACE: 'sip', TELEPHONY_INTERFACE_READY: true } }
    expect(Object.fromEntries(statusRows('healthy', trunk))['Phone line']).toBe('SIP trunk: ready')
    const missing = { ...live, readiness: { ...live.readiness, ASTERISK_INSTALLED: false, ASTERISK_RUNNING: false } }
    expect(Object.fromEntries(statusRows('healthy', missing)).Asterisk).toBe('Not installed')
  })
  it('reports unknown readiness instead of guessing when no preflight has run', () => {
    const rows = Object.fromEntries(statusRows('healthy', { ...live, readiness: null }))
    expect(rows['Live calls']).toBe('Checking…')
    expect(rows.Asterisk).toBe('Checking…')
    expect(readinessFromStatus({ ...live, readiness: null }).known).toBe(false)
  })
  it('never shows live rows in demo mode', () => {
    const rows = Object.fromEntries(statusRows('healthy', { mode: 'demo', telephony: 'mock', booking_repository_ready: true }))
    expect(rows.Mode).toBe('Demo (no real calls)')
    expect(rows['Live calls']).toBeUndefined()
  })
})

describe('ids', () => {
  it('produces unique ids even without crypto.randomUUID', () => {
    expect(newId()).not.toBe(newId())
  })
})

// Maps /api/calls/request and /api/callback outcomes and /api/status readiness to patient-facing text.
// Never claims a call is connected unless the backend returned a real provider call id.
export const NOW_WINDOW = 'Now (AI receptionist call)'

export function callRequestTarget(window) {
  return window === NOW_WINDOW ? '/api/calls/request' : '/api/callback'
}

export function callReadinessLabel(mode, preflight) {
  if (mode === 'checking') return 'Checking…'
  if (mode === 'offline') return 'Service offline'
  if (mode !== 'live') return 'Demo mode'
  if (!preflight) return 'Live readiness unknown'
  return preflight.LIVE_CALL_ALLOWED === true ? 'Live calls ready' : 'Live calls blocked'
}

export function runtimeStatusMessage({ backend, mode, provider, preflight }) {
  const voicePack = preflight ? (preflight.VOICE_PACK_VALID ? 'ready' : 'not ready') : 'unknown'
  const scheduling = preflight ? (preflight.BOOKING_REPOSITORY_READY ? 'ready' : 'unavailable') : 'unknown'
  const parts = [`Backend ${backend}`, `Provider ${provider}`, `Voice pack ${voicePack}`, `Scheduling ${scheduling}`, callReadinessLabel(mode, preflight)]
  if (mode === 'live' && preflight && preflight.LIVE_CALL_ALLOWED === false && preflight.BLOCKERS?.length) {
    parts.push(`Blockers: ${preflight.BLOCKERS.join('; ')}`)
  }
  return parts.join(' · ')
}

// Merge the cheap /api/status payload with its readiness summary.
export function readinessFromStatus(status) {
  if (!status) return null
  const readiness = status.readiness || {}
  return {
    ...readiness,
    VOICE_PACK_VALID: status.voice_pack_valid,
    BOOKING_REPOSITORY_READY: status.booking_repository_ready,
    FISH_SPEECH_READY: status.fish_speech_ready,
    known: Boolean(status.readiness),
  }
}

const yesNo = (value, yes, no, unknown = 'Unknown') => (value === true ? yes : value === false ? no : unknown)

export function statusRows(backend, status) {
  const readiness = readinessFromStatus(status)
  const live = status?.mode === 'live'
  const rows = [
    ['Backend', backend === 'healthy' ? 'Healthy' : backend === 'checking' ? 'Checking…' : 'Unavailable'],
    ['Mode', status ? (live ? 'Live' : 'Demo (no real calls)') : 'Unknown'],
    ['Telephony', status?.telephony === 'twilio' ? 'Twilio' : status ? 'Disabled' : 'Unknown'],
    ['Voice (Fish Speech)', yesNo(status?.voice_pack_valid, 'Verified voice pack', 'Not ready')],
    ['Fish Speech server', yesNo(status?.fish_speech_ready, 'Ready', 'Not reachable')],
    ['Scheduling', yesNo(status?.booking_repository_ready, live ? 'Connected' : 'Demo slots', 'Not connected (requests only)')],
  ]
  if (live) {
    rows.push(['Call records', yesNo(status?.call_state_store_ready, 'Ready', 'Unavailable')])
    rows.push(['Software ready', readiness?.known ? yesNo(readiness.SOFTWARE_READY_FOR_LIVE_CALL, 'Yes', 'No') : 'Checking…'])
    rows.push(['Live calls', readiness?.known ? yesNo(readiness.LIVE_CALL_ALLOWED, 'Allowed', 'Blocked') : 'Checking…'])
  }
  return rows
}

export function callOutcomeMessage(status, body) {
  const detail = body && typeof body.detail === 'object' ? body.detail : {}
  if (status >= 200 && status < 300) {
    if (body?.call_id && body?.mode === 'live') return 'Calling you now. Your phone should ring shortly.'
    if (body?.mode === 'demo') return 'Demo mode: your request was recorded and no real call was placed.'
    return 'Your request has been received. The clinic team will contact you in the chosen window.'
  }
  if (status === 403 && detail.error === 'opted_out') return 'This number asked not to receive automated calls. The clinic team can still help you by phone.'
  if (status === 403) return 'Automated calls to this number are not enabled. Please choose a time window for a callback from the team.'
  if (status === 429 && ['capacity_reached', 'quota_exhausted'].includes(detail.error)) return 'All our lines are busy right now. Please choose a callback window or try again in a few minutes.'
  if (status === 429) return 'A call to this number was placed recently. Please wait a few minutes before trying again.'
  if (status === 502 && detail.error === 'twilio_outcome_unknown') return 'We could not confirm whether the call started. Please wait a few minutes before trying again.'
  if (status === 503) return 'Live calling is not available right now. Please choose a time window for a callback from the team.'
  if (status === 422 || status === 400) return 'Please enter a valid phone number and give consent.'
  return 'We could not submit the request. Please try again or call the clinic.'
}

export function newId() {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  } catch { /* non-secure context */ }
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

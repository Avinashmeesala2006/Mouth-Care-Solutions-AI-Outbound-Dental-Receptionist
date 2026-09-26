// Maps /api/calls/request and /api/callback outcomes and /api/status readiness to patient-facing text.
// Never claims a call is connected unless the backend returned a real call id from the phone system.
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

// Merge the cheap /api/status payload with its cached full-preflight summary.
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

function asteriskLabel(readiness) {
  if (!readiness?.known) return 'Checking…'
  if (readiness.ASTERISK_RUNNING) return `Running${readiness.ASTERISK_VERSION ? ` (${readiness.ASTERISK_VERSION})` : ''}`
  return readiness.ASTERISK_INSTALLED ? 'Installed, not running' : 'Not installed'
}

function phoneLineLabel(readiness) {
  if (!readiness?.known) return 'Checking…'
  if (!readiness.TELEPHONY_INTERFACE) return 'None connected'
  const kind = readiness.TELEPHONY_INTERFACE === 'sip' ? 'SIP trunk' : 'GSM modem'
  return `${kind}: ${readiness.TELEPHONY_INTERFACE_READY ? 'ready' : 'not ready'}`
}

export function statusRows(backend, status) {
  const readiness = readinessFromStatus(status)
  const live = status?.mode === 'live'
  const rows = [
    ['Backend', backend === 'healthy' ? 'Healthy' : backend === 'checking' ? 'Checking…' : 'Unavailable'],
    ['Mode', status ? (live ? 'Live' : 'Demo (no real calls)') : 'Unknown'],
    ['Telephony', status?.telephony === 'asterisk' ? 'Asterisk' : status?.telephony || 'Unknown'],
    ['Fish Speech', yesNo(status?.fish_speech_ready, 'Ready', 'Not reachable')],
    ['Voice pack', yesNo(status?.voice_pack_valid, 'Verified', 'Not ready')],
    ['Scheduling', yesNo(status?.booking_repository_ready, live ? 'Connected' : 'Demo slots', 'Not connected (requests only)')],
  ]
  if (live) {
    rows.push(['Asterisk', asteriskLabel(readiness)])
    rows.push(['Phone line', phoneLineLabel(readiness)])
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
  if (status === 403) return 'Automated calls to this number are not enabled. Please choose a time window for a callback from the team.'
  if (status === 429) return 'A call to this number was placed recently. Please wait a few minutes before trying again.'
  if (status === 503 && detail.error === 'live_call_preflight_failed') return 'Live calling is not available right now. Please choose a time window for a callback from the team.'
  if (status === 422 || status === 400) return 'Please enter a valid phone number and give consent.'
  return 'We could not submit the request. Please try again or call the clinic.'
}

export function newId() {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  } catch { /* non-secure context */ }
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

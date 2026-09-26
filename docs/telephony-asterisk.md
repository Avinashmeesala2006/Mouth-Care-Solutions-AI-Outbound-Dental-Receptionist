# Telephony: open-source Asterisk

The receptionist places and answers phone calls through **Asterisk** (open source). FastAPI
controls Asterisk over **AMI** on localhost; answered calls are handed to the application's
**FastAGI** server, which plays the verified Fish Speech voice pack and listens to the caller.

```
POST /api/calls/request
  -> allow-list, rate limit, fresh /api/telephony/preflight gate
  -> AMI Originate (async) on OUTBOUND_CHANNEL_TEMPLATE, e.g. PJSIP/{e164}@pstn-trunk
  -> Asterisk -> SIP trunk or GSM modem -> phone network -> callee answers
  -> dialplan [mouthcare-receptionist] s,1 -> AGI(agi://127.0.0.1:4573/receptionist,<call id>,outbound)
  -> FastAGI loop: STREAM FILE <verified Fish asset>  ->  RECORD FILE (silence detection)
                   -> faster-whisper transcript -> grounded phone agent -> next prompts | HANGUP
AMI events (Newchannel/Newstate/OriginateResponse/Hangup + cause) -> durable call record
```

## What Asterisk cannot do by itself

Asterisk alone cannot make a mobile phone ring. A real telephone-network connection is
required, one of:

| `TELEPHONY_INTERFACE` | Needs | Asterisk side |
|---|---|---|
| `sip` | A SIP/PSTN trunk account from a carrier (host, username, password, authorized caller ID) | `chan_pjsip` trunk `pstn-trunk` + outbound registration |
| `gsm` | A USB GSM/4G modem with a voice-capable SIM, attached to WSL with `usbipd-win` | third-party `chan_dongle` (build separately), device `dongle0` |

Until one of these exists and is registered, `/api/telephony/preflight` reports
`LIVE_CALL_ALLOWED=false` with `NO_EXTERNAL_TELEPHONY_INTERFACE`, and `/api/calls/request`
refuses to originate. The caller ID (`OUTBOUND_CALLER_ID`) must be the identity the carrier
authorizes for that line; it is never invented.

## Setup on this Windows host

1. Install WSL2 and Ubuntu (elevated PowerShell), reboot, open Ubuntu once to create a user:
   `wsl --install -d Ubuntu`
2. `.\scripts\setup_asterisk.ps1` - enables WSL mirrored networking (Windows and Asterisk share
   `127.0.0.1`), generates `ASTERISK_AMI_SECRET` into `.env` (never printed), renders the
   configuration (`scripts/asterisk/render_config.py` -> `telephony/asterisk/generated/`,
   git-ignored) and installs/configures Asterisk (`scripts/asterisk/install_asterisk.sh`).
3. Configure the phone line in `.env` (`TELEPHONY_INTERFACE`, `SIP_TRUNK_*` or `GSM_DEVICE`,
   `OUTBOUND_CALLER_ID`), re-run `setup_asterisk.ps1`, then restart FastAPI:
   `.\scripts\run_local_stack.ps1 -RestartApi`.
4. `.\scripts\asterisk_ctl.ps1 status` or `GET /api/telephony/preflight?refresh=1`.

Generated configuration: `manager.conf` (AMI bound to 127.0.0.1, one least-privilege user),
`http.conf`/`ari.conf` disabled, `modules.conf` (unused channel drivers, ARI, voicemail,
queues etc. not loaded), `extensions.conf` (receptionist + inbound contexts), `pjsip.conf`
(transport, plus the trunk only when SIP credentials exist), `dongle.conf` for GSM.

## Readiness

`GET /api/telephony/preflight` computes, from live system state:

- Software (`SOFTWARE_READY_FOR_LIVE_CALL`): Asterisk installed and running, AMI login,
  receptionist dialplan, `format_wav`, FastAGI listening, speech recognition loaded, Fish
  Speech reachable, voice pack valid (hashes, reference voice, ASR-verified wording), call
  state store writable, configuration valid, destination E.164 and allow-listed.
- Interface (`TELEPHONY_INTERFACE_READY`): SIP trunk registered, or GSM modem present,
  registered and SIM ready; caller ID configured.
- `LIVE_CALL_ALLOWED` = software ready and interface ready. It is never a setting.

## Evidence for a real call

`GET /api/calls/{request_id}` returns the status driven by real AMI events
(`queued -> ringing -> answered -> completed`, or `busy`, `no-answer`, `congestion`,
`invalid-number`, `failed` from the carrier's cause code). Admins can read the full event
trail (`GET /api/calls/{request_id}/events`): origination, Asterisk channel events,
`agi_session_started`, every `audio_played` (asset, SHA-256, samples played), every
`speech_turn`, and the hangup cause.

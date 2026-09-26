# Live phone test procedure

1. Start FastAPI on port 8000, Fish Speech on port 8080, and the public HTTPS tunnel; confirm the public origin reaches the same FastAPI process.
2. Run `python scripts/validate_voice_pack.py` and `python scripts/verify_public_route.py --destination +919908552414`.
3. Review `SOFTWARE_READY_FOR_LIVE_CALL`, `TRIAL_CALL_ALLOWED`, and `LIVE_CALL_ALLOWED` independently. Do not treat a ready software stack or Trial permission as production permission.
4. For a Trial audio test, confirm that `+919908552414` is a verified recipient, that it is inside the account's sign-up country, and that the authenticated Console confirms the Trial Voice From number and exact `voice_play_audio` request/input fields. Place exactly one call only when `TRIAL_CALL_ALLOWED=true` and the public `outbound_greeting` WAV is verified.
5. For production, use an upgraded account, its owned Voice-capable number, permitted destination/geography, and the application's outbound webhook. Place exactly one call only when `LIVE_CALL_ALLOWED=true`.
6. Verify the Call SID, initial/final Twilio status, status callback, signed application webhook event, and `audio_served` event for `outbound_greeting`. An API-accepted call alone is not proof of delivered audio.
7. Treat live appointment availability as unavailable until a durable live booking repository is implemented and configured; phone calls can be tested independently.

If account facts or provisioning are missing, report the exact blocker. Never mark a recipient verified, supply an invented Trial From number or template field, bypass signature validation, or claim a call/audio delivery without evidence.

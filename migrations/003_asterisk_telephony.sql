-- Asterisk telephony (ARI -> PJSIP -> GSM/LTE voice gateway).
--
-- 1. Call lifecycle states that distinguish what Asterisk actually reported:
--    REQUEST_ACCEPTED -> ORIGINATE_ACCEPTED -> CHANNEL_CREATED -> RINGING -> ANSWERED -> MEDIA_ACTIVE -> ENDING
--    (old CREATED/DIALING/CONNECTED/ACTIVE rows are mapped to their equivalents).
-- 2. Which telephony implementation handled each call (rows created before this migration were Twilio).
-- 3. The Asterisk channel name (PJSIP/<endpoint>-<seq>) for diagnostics.
-- 4. Idempotent appointment requests (a retried confirmation never creates a second request) with
--    confirmation/update timestamps.
-- connected_time records when media became active (MEDIA_ACTIVE).

ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_status_check;
DROP INDEX IF EXISTS calls_active_idx;

UPDATE calls SET status = CASE status
    WHEN 'CREATED' THEN 'REQUEST_ACCEPTED'
    WHEN 'DIALING' THEN 'ORIGINATE_ACCEPTED'
    WHEN 'CONNECTED' THEN 'MEDIA_ACTIVE'
    WHEN 'ACTIVE' THEN 'MEDIA_ACTIVE'
    ELSE status END;

ALTER TABLE calls ADD CONSTRAINT calls_status_check CHECK (status IN (
    'REQUEST_ACCEPTED', 'ORIGINATE_ACCEPTED', 'CHANNEL_CREATED', 'RINGING', 'ANSWERED', 'MEDIA_ACTIVE', 'ENDING',
    'COMPLETED', 'FAILED', 'BUSY', 'NO_ANSWER', 'TIMEOUT', 'CANCELLED'));
CREATE INDEX calls_active_idx ON calls (status) WHERE status IN (
    'REQUEST_ACCEPTED', 'ORIGINATE_ACCEPTED', 'CHANNEL_CREATED', 'RINGING', 'ANSWERED', 'MEDIA_ACTIVE', 'ENDING');

ALTER TABLE calls ADD COLUMN provider TEXT;
UPDATE calls SET provider = 'twilio' WHERE provider IS NULL;
ALTER TABLE calls ALTER COLUMN provider SET DEFAULT 'asterisk';
ALTER TABLE calls ALTER COLUMN provider SET NOT NULL;
ALTER TABLE calls ADD COLUMN channel_name TEXT;

ALTER TABLE appointment_requests ADD COLUMN idempotency_key TEXT;
ALTER TABLE appointment_requests ADD COLUMN confirmed_at TIMESTAMPTZ;
ALTER TABLE appointment_requests ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE UNIQUE INDEX appointment_requests_idempotency_uidx ON appointment_requests (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX audit_events_entity_idx ON audit_events (entity_type, created_at DESC);

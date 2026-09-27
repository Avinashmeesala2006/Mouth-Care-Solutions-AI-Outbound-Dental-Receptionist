-- Twilio voice calls: unified call state, idempotent provider events, conversation state,
-- per-turn latency, captured requests, consent / do-not-call / opt-out and quota usage.

CREATE TABLE calls (
    id UUID PRIMARY KEY,                              -- internal session id
    provider_call_id TEXT UNIQUE,
    provider_conversation_id TEXT,
    direction TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    customer_number TEXT NOT NULL,                    -- E.164
    from_number TEXT,
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'DIALING', 'RINGING', 'ANSWERED', 'CONNECTED', 'ACTIVE',
                                           'ENDING', 'COMPLETED', 'FAILED', 'BUSY', 'NO_ANSWER', 'TIMEOUT', 'CANCELLED')),
    termination_reason TEXT,
    source TEXT NOT NULL,                             -- admin_api | web_form | inbound | test_script
    session_mode TEXT NOT NULL DEFAULT 'conversation' CHECK (session_mode IN ('conversation', 'websocket_echo')),
    requested_by TEXT,
    idempotency_key TEXT UNIQUE,
    request_name TEXT,
    request_topic TEXT,
    preferred_window TEXT,
    reserved_seconds INTEGER NOT NULL DEFAULT 0 CHECK (reserved_seconds >= 0),
    duration_seconds INTEGER CHECK (duration_seconds >= 0),
    price TEXT,
    rate TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    call_start_time TIMESTAMPTZ,
    answer_time TIMESTAMPTZ,
    connected_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ
);
CREATE INDEX calls_active_idx ON calls (status) WHERE status IN ('CREATED', 'DIALING', 'RINGING', 'ANSWERED', 'CONNECTED', 'ACTIVE', 'ENDING');
CREATE INDEX calls_customer_idx ON calls (customer_number, created_at DESC);
CREATE INDEX calls_conversation_idx ON calls (provider_conversation_id);

CREATE TABLE call_events (
    id BIGSERIAL PRIMARY KEY,
    call_id UUID REFERENCES calls (id) ON DELETE CASCADE,
    provider_call_id TEXT,
    kind TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,                           -- provider webhook idempotency
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX call_events_call_idx ON call_events (call_id, id);

CREATE TABLE call_sessions (
    call_id UUID PRIMARY KEY REFERENCES calls (id) ON DELETE CASCADE,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE call_turns (
    id BIGSERIAL PRIMARY KEY,
    call_id UUID NOT NULL REFERENCES calls (id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    transcript TEXT,
    intent TEXT,
    prompts JSONB NOT NULL DEFAULT '[]'::jsonb,
    barge_in BOOLEAN NOT NULL DEFAULT FALSE,
    stt_latency_ms INTEGER,
    ai_latency_ms INTEGER,
    tts_latency_ms INTEGER,
    first_audio_latency_ms INTEGER,
    total_turn_latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX call_turns_call_idx ON call_turns (call_id, turn_index);

CREATE TABLE appointment_requests (
    id TEXT PRIMARY KEY,
    call_id UUID REFERENCES calls (id) ON DELETE SET NULL,
    caller TEXT,
    patient_name TEXT,
    preferred_date TEXT,
    preferred_time TEXT,
    reason TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE callback_requests (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    call_id UUID REFERENCES calls (id) ON DELETE SET NULL,
    contact TEXT NOT NULL,
    topic TEXT NOT NULL,
    preferred_window TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE contact_consents (
    phone_number TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('granted', 'revoked')),
    source TEXT NOT NULL,
    note TEXT,
    granted_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE do_not_call (
    phone_number TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE opt_outs (
    id BIGSERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    source TEXT NOT NULL,
    call_id UUID REFERENCES calls (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleared_at TIMESTAMPTZ,
    cleared_reason TEXT
);
CREATE INDEX opt_outs_phone_idx ON opt_outs (phone_number) WHERE cleared_at IS NULL;

CREATE UNIQUE INDEX patients_phone_uidx ON patients (phone) WHERE phone IS NOT NULL;

-- Panel tables (schema "panel", owner: role "panel"). Applied on every start; must stay idempotent.

CREATE TABLE IF NOT EXISTS admins (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    totp_secret TEXT,
    role TEXT NOT NULL DEFAULT 'viewer',          -- super | viewer
    disabled BOOLEAN NOT NULL DEFAULT false,
    must_change BOOLEAN NOT NULL DEFAULT true,
    session_ver INTEGER NOT NULL DEFAULT 1,
    totp_last_step BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    last_login_ip TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind TEXT NOT NULL,
    detail JSONB,
    actor TEXT,
    actor_ip TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

-- current registration of each SIP endpoint (from AMI ContactStatus / PJSIPShowContacts)
CREATE TABLE IF NOT EXISTS devices (
    endpoint TEXT PRIMARY KEY,
    uri TEXT,
    ip TEXT,
    port INTEGER,
    user_agent TEXT,
    status TEXT,                                  -- Reachable | Unreachable | Created | Removed | NonQualified
    rtt_ms REAL,
    country TEXT,
    city TEXT,
    org TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    registered_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- history: registrations, IP changes, unregistrations, reachability, failed sign-ins
CREATE TABLE IF NOT EXISTS device_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    endpoint TEXT,
    kind TEXT NOT NULL,                           -- register | ip_change | unregister | reachable | unreachable | auth_failed
    ip TEXT,
    port INTEGER,
    user_agent TEXT,
    country TEXT,
    city TEXT,
    org TEXT,
    detail JSONB
);
CREATE INDEX IF NOT EXISTS device_log_ts ON device_log (ts);
CREATE INDEX IF NOT EXISTS device_log_endpoint ON device_log (endpoint, ts);

-- one row per finished call (linkedid), assembled from cel + queue_log + cdr
CREATE TABLE IF NOT EXISTS calls (
    linkedid TEXT PRIMARY KEY,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    direction TEXT NOT NULL,                      -- inbound (DID/IVR) | internal
    source TEXT,                                  -- trunk | endpoint name | local
    caller_num TEXT,
    caller_name TEXT,
    caller_e164 TEXT,
    caller_country TEXT,                          -- ISO code, "--" for internal numbers
    caller_region TEXT,
    caller_carrier TEXT,
    device_ip TEXT,                               -- internal caller: IP the phone was registered from
    device_country TEXT,
    did TEXT,
    dst TEXT,
    lang TEXT,                                    -- ru | en (IVR language choice)
    status TEXT NOT NULL,                         -- answered | missed | ivr | unanswered
    answered_by TEXT,                             -- extension that answered
    answered_queue TEXT,
    first_queue TEXT,
    queues TEXT[] NOT NULL DEFAULT '{}',
    ivr_path TEXT[] NOT NULL DEFAULT '{}',
    ivr_keys TEXT[] NOT NULL DEFAULT '{}',
    last_node TEXT,
    ivr_sec INTEGER NOT NULL DEFAULT 0,
    wait_sec INTEGER NOT NULL DEFAULT 0,
    talk_sec INTEGER NOT NULL DEFAULT 0,
    total_sec INTEGER NOT NULL DEFAULT 0,
    rings INTEGER NOT NULL DEFAULT 0,             -- operator ring attempts without answer
    recording TEXT,
    uniqueids TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS calls_start ON calls (start_at);
CREATE INDEX IF NOT EXISTS calls_caller ON calls (caller_num);
CREATE INDEX IF NOT EXISTS calls_status ON calls (status, start_at);

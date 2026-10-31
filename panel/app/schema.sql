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

-- ---- configuration managed in the panel (the panel writes Asterisk config files from these tables) ----

-- panel and Asterisk settings (Settings page); missing keys use defaults from app/options.py
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);

-- subscribers: SIP account (extension) + personal cabinet + notification channels
CREATE TABLE IF NOT EXISTS subscribers (
    ext TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    email TEXT,
    telegram TEXT,                                -- @username as entered (for reference)
    tg_chat_id BIGINT,                            -- set when the subscriber opens the bot link
    tg_linked_at TIMESTAMPTZ,
    notify_email BOOLEAN NOT NULL DEFAULT true,
    notify_telegram BOOLEAN NOT NULL DEFAULT true,
    notify_direct BOOLEAN NOT NULL DEFAULT true,  -- missed calls to my extension
    notify_group BOOLEAN NOT NULL DEFAULT false,  -- missed calls in my groups that rang me
    lang TEXT,
    sip_enabled BOOLEAN NOT NULL DEFAULT true,
    sip_digest TEXT,                              -- MD5(ext:realm:password), the password itself is not stored
    max_contacts INTEGER NOT NULL DEFAULT 3,
    web_enabled BOOLEAN NOT NULL DEFAULT false,
    password_hash TEXT,
    totp_secret TEXT,
    totp_last_step BIGINT NOT NULL DEFAULT 0,
    must_change BOOLEAN NOT NULL DEFAULT true,
    session_ver INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    last_login_ip TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS subscribers_email ON subscribers (lower(email)) WHERE email IS NOT NULL;

-- groups = Asterisk queues q<id>; nobody answered in ring_sec → next group (NULL: keep waiting in this one)
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    number TEXT UNIQUE,                           -- internal number that dials the group
    strategy TEXT NOT NULL DEFAULT 'ringall',
    ring_sec INTEGER NOT NULL DEFAULT 20,
    next_id INTEGER,
    notify_email TEXT,                            -- missed calls of the group
    tg_chat_id BIGINT,
    tg_title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    ext TEXT NOT NULL REFERENCES subscribers (ext) ON DELETE CASCADE,
    pos INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, ext)
);

-- one-time tokens of Telegram links (t.me/<bot>?start=<token>)
CREATE TABLE IF NOT EXISTS tg_tokens (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                           -- sub | group
    ref TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

-- sent notifications (and protection against sending the same one twice)
CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    linkedid TEXT,
    target TEXT NOT NULL,                         -- sub:<ext> | group:<id>
    channel TEXT NOT NULL,                        -- email | telegram
    status TEXT NOT NULL,                         -- sent | failed
    error TEXT,
    UNIQUE (linkedid, target, channel)
);
CREATE INDEX IF NOT EXISTS notifications_ts ON notifications (ts);

-- extensions that rang during the call (queue members and direct calls); for the cabinet and notifications
ALTER TABLE calls ADD COLUMN IF NOT EXISTS rung TEXT[] NOT NULL DEFAULT '{}';
-- calls stored before notifications existed are "done"; new rows wait for the notifier
ALTER TABLE calls ADD COLUMN IF NOT EXISTS notify_done BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE calls ALTER COLUMN notify_done SET DEFAULT false;
CREATE INDEX IF NOT EXISTS calls_notify ON calls (start_at) WHERE NOT notify_done;
CREATE INDEX IF NOT EXISTS calls_rung ON calls USING gin (rung);
CREATE INDEX IF NOT EXISTS calls_answered_by ON calls (answered_by, start_at);

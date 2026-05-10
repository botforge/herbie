-- Lila initial schema. Idempotent — re-running is a no-op.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,
    username         TEXT UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,
    telegram_chat_id BIGINT UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    type        TEXT NOT NULL,
    file_id     TEXT,
    slug        TEXT,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    transcript  TEXT,
    text        TEXT,
    midi_notes  TEXT,
    ext         TEXT,
    parent_id   TEXT,
    job_id      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_user_created_idx ON events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS events_user_tag_idx     ON events USING GIN (tags);
CREATE INDEX IF NOT EXISTS events_user_file_idx    ON events (user_id, file_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    type            TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'error')),
    input_file_id   TEXT,
    output_file_id  TEXT,
    params          JSONB NOT NULL DEFAULT '{}',
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS jobs_user_status_idx ON jobs (user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id     BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conv_user_idx ON conversation_turns (user_id, turn_id);

CREATE TABLE IF NOT EXISTS last_action (
    user_id     TEXT PRIMARY KEY REFERENCES users(user_id),
    snapshots   JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('001_initial_schema')
ON CONFLICT (version) DO NOTHING;

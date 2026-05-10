# Lila — Multi-user cloud deployment design

Date: 2026-05-10
Status: draft, pending owner approval

## Goal

Take Lila — currently a single-user FastAPI server + Telegram bot + static
web UI living on a laptop — and deploy it as a cloud-hosted, multi-user
web backend that the owner can use from their phone (browser PWA) and
from Telegram, without ever needing to launch anything locally.

Owner is the only user near-term. The data model and storage shape must
support multiple users without a rewrite.

## Non-goals

These are explicitly OUT of scope for this migration:

- Per-user Telegram pairing. The existing single bot keeps working for
  the owner; additional users get the web/PWA only.
- Cross-user features (sharing, collaboration). Each user's archive is
  isolated.
- A native mobile app. PWA over the existing static UI is the target.
- Postgres → R2 migration for audio files. Volume storage is fine until
  horizontal scale is needed; that move is Phase 2.
- A real frontend rewrite (React/Vue/etc.). Keep `static/index.html` as
  vanilla HTML/JS with no build step.
- Public registration UI. Owner admin-creates accounts via CLI.

## Invariants preserved

These come from the handoff doc and must not regress:

1. **Chat-side archive is append-only.** No mutating edit tool reappears
   on the LLM tool surface. Corrections continue to flow through
   `file_system_note` as new tagged events.
2. **Pipeline is transport-agnostic.** `services/pipeline.py` stays the
   single boundary. Any new transport (web JSON, future native, etc.)
   dispatches segments and manages history; routing/parsing stays in the
   pipeline.
3. **LLM is the sole intent router.** No Python keyword classifiers in
   front of the LLM. Eval-flag short-circuit (3+ leading threes) remains
   the one exception.
4. **Tool descriptions are part of the contract.** Any tool change
   updates the description in the same edit.
5. **Comments read as a numbered story.** All new code follows the
   CLAUDE.md convention.

---

## Architecture

```
                    ┌────────────────────────┐
                    │  Phone browser (PWA)   │
                    │  static/index.html +   │
                    │  manifest + SW         │
                    └───────────┬────────────┘
                                │ HTTPS
                                │ (cookie auth)
                                ▼
   ┌──────────────────────────────────────────────────────┐
   │              Fly.io app: lila                        │
   │   ┌──────────────────────┐  ┌────────────────────┐  │
   │   │  process: web        │  │ process: telegram  │  │
   │   │  uvicorn main:app    │  │ python tg_bot.py   │  │
   │   └──────────┬───────────┘  └─────────┬──────────┘  │
   │              │                        │             │
   │              │  services/pipeline.py  │             │
   │              ├──── shared service layer ────────────┤
   │              │  archive.py (Postgres + volume)      │
   │              └──────────────┬─────────────────────  │
   │                             │                       │
   │     /data (mounted volume)  │                       │
   │     ├── archive/<user_id>/raw/<file_id>.<ext>       │
   │     ├── faster-whisper/ (model cache)               │
   │     └── logs/                                       │
   │                             │                       │
   └─────────────────────────────┼───────────────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────┐
                  │  Fly Postgres            │
                  │  (managed, same region)  │
                  │  ├ users                 │
                  │  ├ events                │
                  │  ├ jobs                  │
                  │  └ conversation_history  │
                  └──────────────────────────┘
```

Two processes, one container image, one volume, one Postgres database.
Both processes import the same `services/` layer. Postgres is the source
of truth for everything except raw audio bytes (volume) and the LLM
prompt (`soul.md` baked into the image).

---

## Data model — Postgres schema

The events table preserves the event-sourced shape currently in
`events.jsonl`. Reads do not become richer; writes become safe under
concurrency. The append-only chat invariant continues to hold —
`file_system_note` writes a new row, never mutates one.

**Identity model.** `user_id` is a stable lowercase slug chosen at
account creation (e.g. `"dhruv"`). It is identical to `username` for
simplicity; we keep both columns so a future rename can change
`username` without rewriting every foreign key. UUIDs are over-built for
this scale — slugs are easier to grep in logs and SQL consoles.

```sql
-- Users. Owner admin-creates rows via CLI; no public registration UI.
CREATE TABLE users (
    user_id        TEXT PRIMARY KEY,             -- stable slug, e.g. "dhruv"
    username       TEXT UNIQUE NOT NULL,         -- starts equal to user_id
    password_hash  TEXT NOT NULL,                -- argon2id
    telegram_chat_id BIGINT UNIQUE,              -- nullable; one TG account per user
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only event log. One row per audio/text/delete/job_queued/job_done.
-- The web PATCH endpoint (the ONE mutating path per the handoff) updates
-- in place; everything from the chat surface inserts.
CREATE TABLE events (
    event_id    TEXT PRIMARY KEY,                -- 8 hex chars
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    type        TEXT NOT NULL,                   -- audio|text|delete|job_*
    file_id     TEXT,                            -- 8 hex chars when present
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
CREATE INDEX events_user_created_idx ON events (user_id, created_at DESC);
CREATE INDEX events_user_tag_idx     ON events USING GIN (user_id, tags);
CREATE INDEX events_user_file_idx    ON events (user_id, file_id);

-- Jobs as their own table (today: jobs/<job_id>.json files).
CREATE TABLE jobs (
    job_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    type            TEXT NOT NULL,
    status          TEXT NOT NULL,               -- queued|running|done|error
    input_file_id   TEXT,
    output_file_id  TEXT,
    params          JSONB NOT NULL DEFAULT '{}',
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX jobs_user_status_idx ON jobs (user_id, status, created_at DESC);

-- Per-user-global conversation history. One row per turn. Replaces the
-- in-memory _conversations dict. Per-user-global means: the user has one
-- conversation across all transports (web, Telegram, future).
--
-- Retention: rows accumulate forever (cheap; valuable for eval). When
-- building LLM context, callers fetch only the last 20 turns via
-- ORDER BY turn_id DESC LIMIT 20 — matching the existing 20-turn cap
-- in main.py and telegram_bot.py.
CREATE TABLE conversation_turns (
    turn_id     BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    role        TEXT NOT NULL,                   -- user|assistant
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX conv_user_idx ON conversation_turns (user_id, turn_id);

-- Single-step undo buffer (today: .last_action.json) — per user.
CREATE TABLE last_action (
    user_id     TEXT PRIMARY KEY REFERENCES users(user_id),
    snapshots   JSONB NOT NULL,                  -- list of {file_id, before}
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Storage paths on volume:**
```
/data/archive/<user_id>/raw/<file_id>.<ext>     # immutable audio bytes
/data/archive/<user_id>/raw/<file_id>.txt       # text-event payloads
/data/archive/<user_id>/raw/<file_id>.mid       # rendered MIDI on first reveal
/data/whisper-cache/                            # faster-whisper model
/data/logs/conversations.jsonl                  # eval log (kept on disk
                                                # for the existing flow)
```

Audio bytes stay on disk because Phase 2 will move them to R2; rewriting
the read/write paths now means doing the same work twice. The volume is
the right resting place until horizontal scale forces the issue.

---

## Service layer changes

Goal: the public function shapes in `services/archive.py` stay the same.
Callers add `user_id` as the first parameter; backend swaps from JSONL +
files to Postgres + files. No caller learns about Postgres.

### `services/archive.py` (rewritten internals)

Public API — every function gains `user_id: str` as its first parameter:

```
ingest_audio(user_id, src_path, slug, tags, ext, transcript, parent_id) → event
ingest_text(user_id, slug, tags, text, parent_id, midi_notes)           → event
stage_audio(user_id, src_path, ext)                                     → (file_id, path)
commit_audio(user_id, file_id, slug, tags, ext, transcript)             → event
update_file_meta(user_id, file_id, slug?, tags?, transcript?, text?)    → bool
undo_last_action(user_id)                                               → int
current_entry(user_id, file_id)                                         → dict
get_feed(user_id, tag, limit, offset)                                   → list[dict]
get_all_tags(user_id)                                                   → list[dict]
delete_file(user_id, file_id)                                           → bool
queue_job(user_id, job_type, input_file_id, params)                     → dict
complete_job(user_id, job_id, output_file_id, output_text)              → None
get_jobs(user_id, status)                                               → list[dict]
get_slug_version(user_id, slug)                                         → int
search(user_id, query)                                                  → list[dict]
ensure_user_dirs(user_id)                                               → None
```

Implementation notes:

1. Database connection: a single `psycopg`/`asyncpg` pool initialized at
   process startup. Held in a module-level singleton inside
   `services/db.py`.
2. Each writing function runs inside a single transaction. The
   `update_file_meta` snapshot+rewrite pattern becomes a transaction
   that captures the prior row to `last_action` and updates the row.
3. Reads filter on `user_id` everywhere. There is no path that reads
   another user's events — enforced at the SQL layer, not in Python.
4. Migration of the existing single-user archive: a one-shot
   `scripts/migrate_jsonl_to_postgres.py` script reads every event from
   the existing `events.jsonl`, assigns them `user_id = 'dhruv'`, and
   inserts into Postgres. Raw files stay on disk and are copied to
   `/data/archive/dhruv/raw/`.

### `services/pipeline.py`

Unchanged in shape. Both `handle_text` and `handle_audio` gain a
`user_id: str` parameter. They thread it into every archive call. The
existing transport-agnostic boundary holds.

### `services/llm.py`

The tool handler closures inside `respond_to_text` already accept a
single `args` dict. Update them to also close over `user_id` so the
embedded `_tool_list_entries`, `_tool_read_entries`, `_tool_file_text`,
`_tool_file_system_note` calls pass it through to archive functions.
The tool descriptions do not change — `user_id` is server-side context,
never visible to the LLM.

### `services/conversation_log.py`

`log_turn` gains `user_id`. The on-disk `logs/conversations.jsonl` file
stays (it's the source for eval-flag mining and the existing flow
expects it). Each appended row gains `"user_id": "..."` so eval analysis
can filter by user. The file lives at `/data/logs/conversations.jsonl`
in production, on the persistent volume.

### `services/users.py` (new)

Tiny CRUD layer for the `users` table plus a `__main__` block that
exposes the admin CLI:

```
python -m services.users create --username dhruv --password '...' \
    --telegram-chat-id 12345
python -m services.users set-telegram --username dhruv --chat-id 12345
python -m services.users list
```

No public registration route. The CLI is the only way to make a user.

---

## Authentication

**Strategy: username/password, JWT in httpOnly cookies.**

1. Owner admin-creates accounts via a CLI:
   ```
   python -m services.users create --username dhruv --password ...
   ```
   Hashes with argon2id (`argon2-cffi`), inserts into `users`.

2. Login route: `POST /auth/login` — verifies password, issues a JWT
   signed with `LILA_JWT_SECRET`, sets it as an httpOnly Secure cookie
   with a 30-day expiry.

3. Logout route: `POST /auth/logout` — clears the cookie.

4. FastAPI dependency `get_current_user(request) -> User` reads the
   cookie, validates the JWT, returns the user record. Raises 401 if
   invalid/missing.

5. Every archive route depends on `get_current_user`. The user's
   `user_id` is the parameter passed into all `services/archive.py`
   calls. Routes do not accept `user_id` from query params or bodies.

6. Static UI: the existing `static/index.html` mount becomes auth-gated
   except for `/auth/login` and the static assets. The PWA login screen
   is a simple form posting to `/auth/login`.

7. Telegram: the bot maps `update.effective_chat.id` to a `user_id` via
   the `users.telegram_chat_id` column. If no row matches, it replies
   "this telegram account is not linked to a Lila user." The owner's
   chat_id is set when their user row is created.

---

## Hard fixes that come along for free

These are blocking issues for cloud deployment that get fixed in this
migration:

1. **Remove `/files/{file_id}/reveal`.** It calls `subprocess.Popen
   (["open", "-R", ...])` which is macOS-only. Drop the route, drop the
   "Reveal" button from the UI.
2. **Add `/health` endpoint.** Returns `{"ok": true}` and a DB ping.
   Fly's health probes use it.
3. **Replace `print(flush=True)` debug at logging boundaries.** Use
   `logging` with JSON formatter so Fly's log aggregator parses fields.
   Keep `print` calls inside service code if they're already useful — do
   not chase a full rewrite.
4. **Wrap synchronous job execution in FastAPI `BackgroundTasks`.** The
   route returns the `job` row immediately; execution happens in the
   background. Phase 2 swaps `BackgroundTasks` for Celery.
5. **Bake the faster-whisper model into the Docker image.** Avoid the
   ~140MB cold-download timeout on first request. Model files cached at
   `/data/whisper-cache/` so the volume mounts pick up across deploys.
6. **`ARCHIVE_PATH` defaults to `/data/archive`** in the deployed image.
   Local development continues to use `./archive` via `.env`.

---

## Hosting — Fly.io

**Why Fly.io:**
- Native support for two processes from one image via `[processes]` in
  `fly.toml` (web + telegram bot).
- Persistent volumes (1GB free tier, $0.15/GB/mo beyond) attach to one
  machine and survive restarts.
- Managed Fly Postgres in the same region; private networking, no
  public exposure.
- Single region single machine fits the current scale; horizontal scale
  is a config change later.
- Generous free tier; expected cost ~$5–10/mo for compute + DB + volume.

**Topology:**
- Region: `iad` (or owner's nearest region).
- One app, two processes:
  ```
  [processes]
    web      = "uvicorn main:app --host 0.0.0.0 --port 8080"
    telegram = "python telegram_bot.py"
  ```
- Volume: `lila_data` mounted at `/data`, 5GB to start.
- Postgres: `lila-db` (Fly Postgres single-node), connection string
  injected as `DATABASE_URL` env var.
- Secrets injected via `flyctl secrets set`:
  `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `LILA_JWT_SECRET`,
  `MODEL`.

---

## Mobile UX — PWA over `static/index.html`

**Why not a SPA rewrite:** the existing UI is vanilla HTML/JS with no
build step. Preserving that simplicity is more valuable than the
marginal UX win of a framework. Add manifest + service worker; do not
rewrite.

Changes to `static/`:

1. `static/manifest.json` — name, icons, display mode `standalone`.
2. `static/sw.js` — service worker with a minimal cache strategy: cache
   the shell (`/`, `/static/*`); pass-through everything else.
3. `static/index.html` — link the manifest, register the service worker,
   add a `<meta name="viewport">` tag for mobile.
4. **Voice memo recording on mobile.** A new "record" button uses
   `navigator.mediaDevices.getUserMedia({ audio: true })` and
   `MediaRecorder` to capture audio, then POSTs to `/upload`. The
   existing upload endpoint accepts the blob without changes.
5. **Login screen.** Simple form view on `/login` posting to
   `/auth/login`. Redirects to `/` on success.

The PWA installs to the home screen on iOS and Android. Owner adds it
once, opens it from the icon, never sees a browser chrome.

---

## Telegram bot — minimal changes

The single-user bot keeps working for the owner. Changes:

1. On startup, look up the configured owner's `user_id` by joining on
   `telegram_chat_id`. If no row matches, log a warning and exit.
2. Pass `user_id` into every `pipeline.handle_text` /
   `pipeline.handle_audio` call.
3. Conversation history reads/writes go through the new
   `conversation_turns` table, keyed by `user_id`. Across restarts,
   history persists. Across transports, history is shared (per the
   per-user-global decision).

Per-user Telegram pairing remains a Phase 2 stretch goal.

---

## Migration plan (existing single-user archive → cloud)

Steps run once, before flipping DNS:

1. Provision Fly.io app + volume + Postgres. Run schema migrations to
   create the tables above.
2. Create the owner's user row via the CLI:
   `python -m services.users create --username dhruv --password ...`
   Set `users.telegram_chat_id` to the owner's chat ID.
3. SSH into the Fly machine. Run
   `scripts/migrate_jsonl_to_postgres.py --user-id dhruv` against the
   existing `archive/events.jsonl` uploaded to `/data/archive/_import/`.
   The script:
   - Reads every event, assigns `user_id = 'dhruv'`, inserts into
     `events`.
   - Copies every file in the source `raw/` to
     `/data/archive/dhruv/raw/`.
   - Replays every job JSON into the `jobs` table.
   - Writes a `migration_complete.json` marker so re-running the script
     is a no-op.
4. Smoke-test the web UI: log in, view feed, file a text note, file a
   voice note, file a system note (correction).
5. Smoke-test the Telegram bot: send a voice note, verify it files;
   send a correction, verify it appends a system note.
6. Cut over: stop the local laptop processes. From now on, everything
   runs on Fly.

Rollback: the laptop processes can be restarted at any time; the cloud
deployment is additive until the cutover step.

---

## Manual setup the owner needs to do

These are the steps that aren't code — accounts, secrets, DNS:

1. **Create a Fly.io account.** `flyctl auth signup` (or login if you
   already have one). Add a payment method (free tier covers initial
   usage but a card is required).
2. **Install `flyctl`** locally. `brew install flyctl`.
3. **(Optional) Buy a domain** if you want `lila.yourdomain.com`. Skip
   if `lila.fly.dev` is fine — Fly gives you a free subdomain.
4. **Confirm the existing Telegram bot token** in `.env` is the one you
   want production to use. If you want a fresh bot for prod (recommended
   so dev and prod don't collide), create one with `@BotFather` and save
   the new token.
5. **Generate a JWT signing secret.** `python -c "import secrets;
   print(secrets.token_urlsafe(48))"`. Save it; it goes into Fly
   secrets.
6. **Pick a username and password** for the owner account. The CLI
   command to create your user runs after deploy.
7. **Set the owner's Telegram chat ID.** Send any message to the bot
   running locally; it logs your chat ID. Or use `@userinfobot`. Save
   the integer.

Once those are gathered, the deployment commands run end-to-end
(captured in the implementation plan).

---

## What "always on" looks like after deployment

Owner workflow once this lands:

1. **From Telegram:** open the existing bot chat, send a voice note, get
   a filing confirmation. Same as today, but the server is in the
   cloud, so the laptop can be off.
2. **From phone browser:** open the installed PWA from the home screen,
   record a voice memo, see it appear in the feed. Filter by tag.
   Listen back to past entries.
3. **From laptop browser:** same PWA, full feed view, drag MIDI files
   into a DAW.

Restarts: Fly auto-restarts the machine on crash and on deploy. Volume
and Postgres survive restarts. Conversation history survives restarts
(Postgres-backed). Cold start of the Whisper model is avoided by baking
it into the image and caching on the volume.

---

## Phase 2 (deferred — not part of this migration)

Captured here so the Phase 1 design doesn't block them later:

- **Audio bytes → R2.** When horizontal scale is needed, audio reads
  and writes move from the volume to Cloudflare R2 (or S3). The
  archive.py API does not change; only the file I/O backend swaps.
  Audio URLs are signed, short-lived presigned reads.
- **`BackgroundTasks` → Celery + Redis.** When job duration warrants a
  proper queue (real DSP, retries, dead-letter, observability).
- **Multiple Fly machines.** Once audio is on R2 and Postgres is
  replicated, the web process scales horizontally trivially. Telegram
  bot stays one machine (long-poll is single-consumer).
- **Per-user Telegram pairing.** Owner-only today; future users get
  their own bot tokens or a pairing-code flow.
- **Read replicas / search.** If feed reads slow down, add Postgres
  read replicas or a dedicated search index.

---

## Decisions locked in by the owner

1. **Telegram bot:** reuse the existing bot token in `.env`. Don't
   create a new one. Implication: the local laptop bot must be stopped
   before the cloud bot starts polling, or both will race for updates
   and one will see "Conflict: terminated by other getUpdates request"
   from Telegram. The cutover step in the migration plan handles this.
2. **Initial credentials:** seed a default username/password during
   first deploy so the owner can log in immediately. The owner rotates
   the password from the UI (or via `python -m services.users
   set-password`) afterward. Defaults:
   - username: `dhruv`
   - password: a random URL-safe 16-char string printed once to the
     deploy logs and also written to a file the owner can `flyctl ssh`
     to retrieve. Never committed to git.

## Open questions still pending (non-blocking — pick at deploy time)

1. **Domain:** `lila.fly.dev` (free, instant) vs custom domain (1-day
   DNS setup)?
2. **Region:** which Fly region — `iad` (US East), `sjc` (US West),
   `lhr` (UK)?
3. **Whisper model size:** stay on `base` (~140MB, fast, decent
   transcripts) or upgrade to `small`/`medium` for better accuracy?
   Larger models slow down audio ingest.
4. **Slash-command surface:** the existing slash commands in the web UI
   stay, or get retired? (Not a blocker; either is fine.)

# Lila — Handoff: web backend + auth + mobile

You're picking up a working single-user assistant and turning it into
a multi-user web backend that the owner can use from a phone via
username/password. This brief tells you what the code won't tell you:
the rationale behind recent design decisions, the invariants you must
preserve, and the open questions you need to resolve before writing code.

---

## Goal

Take Lila — currently a single-user Telegram bot + FastAPI server with
a static web UI — and make it a deployable, multi-tenant web backend.

End state the owner wants:
- Owner can open Lila on their phone (browser is fine; PWA is fine;
  native is not required).
- Auth via username/password — owner will be the only user initially,
  but the data model must support more without a rewrite.
- Existing Telegram transport still works for the owner; per-user
  Telegram pairing is a stretch goal, not a blocker.

---

## Read these first (in order)

1. `CLAUDE.md` — project rules. Comment style is non-negotiable.
2. `soul.md` — the assistant's prompt. Read it end-to-end; the tool
   surface is described here too.
3. `README.md` — user-facing overview, run instructions.
4. `services/pipeline.py` — the transport-agnostic boundary. Every
   transport (FastAPI, Telegram, CLI) routes through `handle_text` /
   `handle_audio`. New transports plug in here.
5. `services/archive.py` — the storage layer. Read the module docstring;
   it explains the on-disk layout and the append-only invariant.
6. `services/llm.py` — the tool loop. The chat tool surface is
   append-only: `file_text`, `file_system_note`, `queue_job`,
   `list_entries`, `read_entries`, plus per-turn `file_audio` for voice
   ingest.
7. `main.py` — current FastAPI entry. Note the in-memory
   `_conversations: dict[str, list[dict]]` per session — that is one of
   the things you will replace.
8. `static/index.html` — the existing web UI. Vanilla HTML/JS, no build
   step. It talks to the FastAPI endpoints. Decide whether to grow it
   in place or rewrite as a real frontend.

---

## Invariants you must preserve

These were arrived at through painful iteration. Don't quietly relax them.

1. **The chat-side archive is append-only.** The bot never mutates
   existing entries. Corrections become new text events tagged
   `system-note` (often inheriting the target's tags). A future
   "healing agent" reconciles them. The web UI's PATCH endpoint is the
   ONLY path that can mutate in place, and it snapshots prior state to
   `.last_action.json` for one-step undo.

2. **The pipeline is transport-agnostic.** `services/pipeline.py`
   returns `{message, segments, type, ...}`. Transports are pure I/O:
   they dispatch the parsed segments to native primitives (Telegram
   `reply_text` + `reply_document`; web JSON; etc.) and manage history.
   No routing or parsing logic in transports.

3. **The LLM is the sole intent router.** No Python keyword classifiers
   in front of the LLM. The eval-flag (3+ leading threes) is the one
   exception — it's a meta-channel that intercepts entirely, logs, and
   never reaches the LLM.

4. **Tool descriptions are part of the contract.** `services/llm.py`
   tool descriptions strongly shape behavior. If you change a tool, the
   description is part of the change. There is no separate spec.

5. **Comments read as a numbered story.** See `CLAUDE.md`. This applies
   to anything you touch.

---

## Recent design decisions (rationale not in code)

- **`edit_entries` was removed.** Earlier the bot could overwrite
  existing entries; it kept destroying lyrics. We replaced it with the
  append-only `file_system_note` pattern so corrections are never
  destructive. Do not re-introduce a mutating edit tool on the chat
  surface.

- **Eval-flag short-circuit.** A user message starting with `33333…` is
  intercepted before any LLM call, logged with `eval_candidate=True`,
  and never enters conversation history. Lets the owner drop
  meta-comments ("response was wrong, fix this") into the eval log
  without polluting future LLM context. Preserve this behavior across
  any transport you add.

- **Pipeline returns pre-parsed segments.** Audio markers
  (`[[audio:<8hex>]]`) are resolved in `services/render.py` and returned
  as typed segments, so transports never re-parse. New transports just
  dispatch segments to their native primitives.

- **Conversation history is in-memory per process.** `main.py` holds
  `_conversations: dict[str, list[dict]]` keyed by session_id. This is
  fine for one user on one process; it is one of the things you'll
  replace when introducing auth (history needs to be per-user-per-device
  or per-user-global, your call).

- **The slash-command surface in the web UI exists.** Look at
  `static/index.html` — there's a slash-command input pattern for
  triggering jobs. Decide if you keep it.

---

## Open eval flags (pending behavioral fixes)

These are in `logs/conversations.jsonl` (search for `eval_candidate: true`).
Not blocking your work, but the owner cares about them:

1. The placeholder `"….."` should be replaced with something like
   `"processing, give me a second…"` — wherever the assistant emits a
   wait/typing placeholder.
2. After `file_system_note` runs, the assistant should emit a brief
   natural confirmation ("noted: …") — not silence and not a misleading
   `"filed"` line. (The `file_audio` hallucination case was just fixed
   by tightening the prompt + tool description; verify your changes
   don't regress it.)

---

## Open architectural decisions you need to make

Don't pick silently. Surface these to the owner before committing to
an approach.

1. **Multi-tenant storage model.** Today there's one `archive/` tree
   with one `events.jsonl`, one `raw/`, one `jobs/`. Options:
   - Per-user subtree: `archive/<user_id>/events.jsonl` etc.
     Simplest, matches existing code.
   - Single shared `events.jsonl` with `user_id` field on every event.
     More flexible for cross-user features (none planned), but requires
     row-level filtering everywhere.
   - Per-user Postgres tenant. Larger lift; only worth it if you also
     need search / concurrency / backups.
   The codebase reads `events.jsonl` linearly in many places — pick a
   model that doesn't require rewriting every reader.

2. **Auth method.** Username/password is the requirement. Choose:
   session cookies vs JWT, password hashing (argon2id is fine), where
   to store users (a `users.json`, sqlite, Postgres). Owner is the only
   user near-term — don't over-build.

3. **Conversation history scoping.** Per-user-per-device, per-user-global,
   or shared with Telegram chat? Simplest answer: per-user-global (one
   history per authenticated user, regardless of which transport they
   used). Confirm with the owner.

4. **Hosting target.** Owner hasn't said. Likely candidates: Fly.io,
   Render, a small VPS. Affects file-storage choices (local disk vs S3
   for `raw/`). The model files for `faster-whisper` and the audio
   processing make this non-trivial — don't assume serverless will work.

5. **Mobile UX.** PWA over the existing `static/index.html`, or a real
   SPA rewrite? Voice memo recording from mobile browser is the trickiest
   piece — getUserMedia + MediaRecorder, then upload as audio file.
   The existing FastAPI `/upload` endpoint already accepts uploads.

6. **Telegram per-user.** Today the `.env` has one `TELEGRAM_BOT_TOKEN`
   for the owner. If other users join, do they get their own bot?
   Pair with their account via a code? Ignore until requested.

---

## Things that look like they should change but probably shouldn't

- The `Gemini Flash Lite` default model — it's been chosen for cost +
  latency. If you want to swap, ask first.
- The `[[audio:<8hex>]]` marker convention — both transports depend on
  it; don't change the format without updating both.
- The append-only invariant on the chat surface — see above.

---

## Verifying your work

- `pytest tests/` — 23 passing as of handoff.
- For prompt/behavior changes: there is no automated test for "the LLM
  picks the right tool." Verify by sending real messages through the
  bot or web UI and checking `logs/conversations.jsonl`.
- If you add new transports, mirror the existing pattern: pipeline
  returns segments, transport dispatches them. Don't bypass.

# Lila — Deploy & cutover runbook

One-time. After this, deploys are `flyctl deploy`.

## 0. Prerequisites

- `flyctl` installed: `brew install flyctl`
- Logged in: `flyctl auth login`
- Payment method on file: `flyctl auth signup` if new account.

## 1. Provision the Fly app + volume + Postgres

```bash
flyctl apps create lila --org personal
flyctl volumes create lila_data --region iad --size 5 --app lila
flyctl postgres create --name lila-db --region iad
flyctl postgres attach lila-db --app lila
# this prints DATABASE_URL into the app's secrets automatically
```

## 2. Set the rest of the secrets

```bash
flyctl secrets set --app lila \
    OPENROUTER_API_KEY='...' \
    MODEL='google/gemini-2.0-flash-lite-001' \
    TELEGRAM_BOT_TOKEN='...' \
    LILA_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
    LILA_OWNER_USERNAME='dhruv'
```

## 3. First deploy

```bash
flyctl deploy --app lila
```

Wait for the health check to go green. Watch logs:

```bash
flyctl logs --app lila
```

## 4. Apply migrations + seed the owner

```bash
flyctl ssh console --app lila
# inside the container:
python migrations/run.py
python scripts/seed_owner.py
cat /data/initial_password.txt   # save these
exit
```

## 5. Stop the laptop processes BEFORE bringing up the cloud Telegram bot

If both run simultaneously they fight for `getUpdates`:

```bash
# on your laptop
pkill -f telegram_bot.py
pkill -f 'uvicorn main:app'
```

Now the cloud Telegram process takes over polling on its own.

## 6. Migrate the existing archive

```bash
# upload the archive directory to the volume
flyctl ssh sftp shell --app lila
> mkdir /data/archive_import
> put -r ./archive /data/archive_import
> exit

flyctl ssh console --app lila
python scripts/migrate_jsonl_to_postgres.py \
    --user-id dhruv --source /data/archive_import/archive
exit
```

## 7. Link Telegram

Get your numeric Telegram chat id (e.g. send `/id` to `@userinfobot`).

```bash
flyctl ssh console --app lila
python -m services.users set-telegram --username dhruv --chat-id <YOUR_CHAT_ID>
exit
```

Restart the bot process so it picks up the new mapping:

```bash
flyctl machine restart --app lila --process telegram
```

## 8. Smoke tests

- Open `https://lila.fly.dev/login.html`. Sign in. See your feed.
- Send the bot a voice note from Telegram. Confirm filing.
- Send the bot a correction (e.g. "actually that's monastery, not underworld"). Confirm a system-note appears in the feed.
- Use the PWA install flow (Add to Home Screen) on your phone.

## 9. Rotate the seeded password

Open the web UI's account page (or run `set-password`):

```bash
flyctl ssh console --app lila
python -m services.users set-password --username dhruv --password 'YOUR_PASSWORD'
exit
```

Delete `/data/initial_password.txt`:

```bash
flyctl ssh console --app lila
rm /data/initial_password.txt
exit
```

## Day-2 ops cheatsheet

```bash
flyctl deploy            # ship code changes
flyctl logs              # tail logs
flyctl ssh console       # shell into a running machine
flyctl machine restart --process web
flyctl postgres connect --app lila-db
```

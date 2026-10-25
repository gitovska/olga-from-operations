# Olga from Operations

Olga from Operations sits in the HR Department of Coworking at 90mil. Her role
is to manage bookings in the office, reading the Coworking calendar and letting
the group know when the office will be used by other residents at 90mil.

She is a small CalDAV-to-Telegram service that sends one daily calendar
reminder. The service runs with Docker Compose and SQLite; no web server,
inbound ports, or external database required.

- Sends at **09:00 Europe/Berlin** by default.
- Includes timed and all-day events starting today, ordered by start time.
- Supports recurring events, exceptions, cancellations, and daylight saving.
- Sends nothing on empty days.
- Remembers completed days across restarts using a persistent SQLite ledger.

```text
The following events are taking place today:
- Win the Week: 09:30 to 10:30 in Coworking
- Art School: 14:00 to 16:00 in Coworking
- Plenum: 18:00 to 20:00 at 90mil
Thank you, Olga.
```

Single events use a sentence instead of a list:

```text
Art School is taking place in Coworking from 14:00 to 16:00 today.
Thank you, Olga.
```

## Quick start

Requires Docker Compose, an HTTPS CalDAV calendar, and a Telegram bot created
with `@BotFather`. Use a dedicated bot token, not another service's token.

### 1. Configure credentials

```sh
mkdir -p secrets data
cp .env.example .env
```

Using an editor, create these UTF-8 files with one credential per file:

```text
secrets/caldav_username.txt
secrets/caldav_password.txt
secrets/telegram_bot_token.txt
```

Do not put credentials in shell commands or commit them. The container runs as
UID/GID 1000: secret files must be readable and `data/` writable by that user.
If your host UID is 1000, use `chmod 700 secrets` and `chmod 600 secrets/*.txt`.
Otherwise arrange access for UID/GID 1000 without making secrets world-readable.

Edit `.env` to select the calendar and timezone, then build:

```sh
docker compose build olga
```

### 2. Find the destination chat

Add the bot to your group with permission to send messages, then run:

```sh
docker compose run --rm --no-deps olga \
  /app/.venv/bin/python -m olga discover-chat
```

When prompted, send `/chatid@YourBotUsername` in the group. For private testing,
open the bot's private chat, press Start, and send `/chatid` instead.
Copy the printed numeric ID into `.env` as `TELEGRAM_CHAT_ID`.
Group IDs are negative; private IDs are positive.

Discovery reads updates but never sends messages or changes delivery state.
It waits 120 seconds by default; use `discover-chat --timeout 300` to extend
this (maximum 600). Do not run another update consumer for the same token, and
remove any existing Bot API webhook before polling.

### 3. Preview without sending

```sh
docker compose run --rm --no-deps olga \
  /app/.venv/bin/python -m olga dry-run
```

**Dry-run never contacts Telegram or modifies delivery state.** It only needs
the CalDAV credentials and prints today's message.

### 4. Start the service

**Starting after 09:00 can send today's reminder immediately if the day has
not already completed.** Confirm the preview and destination first.

```sh
docker compose up -d olga
docker compose logs -f olga
```

Stop with `docker compose stop olga`. Run only one service instance.

## Configuration

Compose reads non-secret settings from `.env`. Local Python commands do not
automatically load it.

| Setting | Default / purpose |
| --- | --- |
| `CALDAV_URL` | HTTPS calendar URL; defaults to the coworking calendar in `.env.example` |
| `TIMEZONE` | `Europe/Berlin` |
| `TELEGRAM_CHAT_ID` | Required for `run`; numeric private or group chat ID |
| `STATE_PATH` | `/data/olga.sqlite3`; Compose mounts host `./data/` here |
| `CALDAV_USERNAME_FILE` | `/run/secrets/caldav_username` |
| `CALDAV_PASSWORD_FILE` | `/run/secrets/caldav_password` |
| `TELEGRAM_BOT_TOKEN_FILE` | `/run/secrets/telegram_bot_token` |

The `_FILE` settings select credential files for local processes. Compose
mounts its configured secrets at the default paths and fixes `STATE_PATH`
to `/data/olga.sqlite3`.

After changing a password, edit the relevant secret file and restart Olga.
Use `dry-run` to check CalDAV access before starting the sending service.

## Deployment

For updates to an **existing** deployment, run from the source checkout on
the deployment host:

```sh
sudo ./deploy-olga
```

The destination defaults to `/opt/stacks/hr/olga_from_operations`. To override:

```sh
sudo ./deploy-olga /path/to/existing/deployment
```

Requires Bash, Docker Compose, rsync, and standard GNU file utilities.
Source and destination must be separate, non-nested directories.

The script:

1. Requires an existing `data/olga.sqlite3` and stops Olga.
2. Backs up the whole state directory to a unique `data.backup-*` directory.
3. Copies code without deleting files or replacing deployed `data/`, backups,
   `secrets/`, or `.env`.
4. Builds the image and runs a non-sending preview.
5. Restarts Olga only after a successful preview, then prints status and logs.

A failed step aborts. Failures after stopping Olga leave it stopped for
investigation. Backups are retained. Update deployed passwords separately:
local secrets are deliberately not copied.

**The restart can send today's reminder if its daily run has not completed.**

### Delivery history and backups

Keep `data/` when rebuilding or upgrading. It stores the SQLite ledger and
journal/WAL files; deleting it can allow a same-day resend. To back up manually,
stop Olga and copy the entire directory. Restore with UID/GID 1000 write access.
A different calendar URL or chat ID has separate delivery history.

If migrating from an older named volume, stop Olga and copy the old container's
entire `/data/.` into the host `data/` directory before switching mounts.
Keep the old volume until the migrated history is verified.

Delivery is **not exactly once**: a crash or ambiguous timeout after Telegram
accepts a message but before the ledger records success can cause a duplicate.

## Local development

Requires Python 3.13 and `uv`:

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

To preview locally without sending anything:

```sh
CALDAV_USERNAME_FILE="$PWD/secrets/caldav_username.txt" \
CALDAV_PASSWORD_FILE="$PWD/secrets/caldav_password.txt" \
uv run python -m olga dry-run
```

A local secret-file `FileNotFoundError` usually means the process is still
using Docker's `/run/secrets/` defaults. Pass the local paths as shown above.

For local chat discovery, also set `TELEGRAM_BOT_TOKEN_FILE`. To run the actual
service locally, export `TELEGRAM_CHAT_ID`, set `STATE_PATH` to a writable local
path, and create its parent directory first. **Do not run it alongside the
deployed service or use a fresh ledger for the production group.**

Tests use synthetic calendars and mocked clients; they do not verify real
CalDAV or Telegram access.

## Event handling and failures

- Multi-day events are announced only on their start date. Recurring instances
  have their own start dates; events starting on earlier days are excluded.
- Times use the configured timezone; floating times are treated as local.
  Overnight finishes include the date. All-day end dates are exclusive.
- Missing locations default to `Coworking`. Messages use "in" for Coworking
  (case-insensitive) and "at" for other locations.
- Late startup catches up today only, not earlier days. Empty days also count
  as completed; later calendar changes do not update completed announcements.
- Temporary failures retry with backoff until local midnight. Permanent errors
  exit nonzero; Compose restarts the service under `unless-stopped`.
- Malformed events are skipped with errors in logs. Oversized messages fail
  rather than being split or silently truncated.
- Logs suppress dependency HTTP details to avoid exposing credentials.
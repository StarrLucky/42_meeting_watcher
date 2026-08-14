# 42 apply — online meeting watcher (HTTP, Dockerized)

Watches [apply.42.tech](https://apply.42.tech) for an **Online (REMOTE)** meeting
slot to open on your path/step, and books it automatically the moment one frees up.

## How it works (no browser)

The site sits behind **Cloudflare**, so a plain `curl`/`requests` call is blocked
(HTTP 403). Instead of driving a real Chromium (the old Playwright approach), the
watcher uses **[`curl_cffi`](https://github.com/lexiforest/curl_cffi)**, which
speaks TLS with a genuine Chrome fingerprint (JA3). Cloudflare lets it through
with just the login cookies — no browser, no headless Chrome.

1. **Login** — Keycloak (realm `42Next`) + Auth.js/next-auth. The watcher drives
   the exact redirect chain a browser would: GET a protected page → Keycloak
   login form → POST credentials → next-auth callback sets the
   `__Secure-authjs.session-token` cookie. The session is cached in
   `state/cookies.json` and reused across restarts.
2. **Read** — every ~60s it calls
   `GET /api/swr?href=/v1/apply/paths/<PATH_ID>/steps/<STEP>` (cookie auth) and
   parses `meetings[]`.
3. **Book** — when a meeting with `location.type == "REMOTE"` and `full == false`
   appears, it fires the site's **Next.js Server Action**:
   `POST /en/paths?pathId=<PATH_ID>` with the `Next-Action` header and body
   `[{"pathId":…,"meetingId":…}]`.
4. Notifies you (log + optional Telegram), writes a `state/BOOKED` sentinel, and
   stops so it never double-books.

## ⚠️ One booking at a time

The site only shows the meeting list while you have **no** meeting selected. If a
meeting is already booked, the API returns an empty list and the watcher just idles
(logs `meetings: (none)`). So **don't book anything manually while waiting** — keep
your slot free so the watcher can grab the Online meeting the moment it opens.

## Quick start (Docker)

```bash
cp .env.example .env
nano .env            # set EMAIL + PASSWORD (and Telegram creds if you want)

docker compose up -d --build
docker compose logs -f          # live logs
```

The container restarts automatically (`restart: unless-stopped`) and persists its
login session + `BOOKED` sentinel in `./state`.

To re-arm after a successful booking (or a test): `rm state/BOOKED` and
`docker compose restart`.

## Test before trusting it

```bash
# 1) Can it log in and read meetings?
docker compose run --rm watcher python watcher.py check

# 2) Dry run — detect the Online slot but DON'T send the booking action:
docker compose run --rm -e DRY_RUN=1 watcher python watcher.py run   # Ctrl-C to stop

# 3) Exercise the FULL booking flow against an On-site slot (you cancel after):
docker compose run --rm watcher python watcher.py book-test
```

Without Docker, the same commands work directly:
`pip install -r requirements.txt` then `python watcher.py check`.

## `Next-Action` ids: auto-discovered (self-healing)

Booking is a Next.js **Server Action**, identified by a hashed id (`Next-Action`
header). That id rotates whenever 42 rebuilds the app — but the underlying source
function **name** (`registerToMeeting`) is stable. So the watcher **auto-discovers
the current id at booking time**: it fetches the `/paths` route JS chunk
(`app/[locale]/(authenticated)/paths/page-<hash>.js`) and reads

```js
createServerReference("40d112…", callServer, void 0, findSourceMapURL, "registerToMeeting")
```

mapping the stable name → the current id. No manual maintenance on redeploys.

Inspect what it resolves right now:

```bash
docker compose run --rm watcher python watcher.py discover
```

Controls (in `.env`, all optional):
- `AUTO_DISCOVER_ACTIONS=1` — set `0` to disable and always use the fallback id.
- `BOOK_ACTION_NAME=registerToMeeting` / `CANCEL_ACTION_NAME=delistMeeting` — the
  stable names to look up (only change if 42 renames the action functions).
- `BOOK_ACTION_ID` / `CANCEL_ACTION_ID` — fallback ids used only if discovery fails.

The watcher also fingerprints the site build each run; if it changes (a redeploy)
it just logs that ids will be re-discovered. If a booking ever fails outright you
still get a loud Telegram alert to book manually.

## Telegram alerts (optional, recommended)

Message [@BotFather](https://t.me/BotFather) → `/newbot` → get the token. Get your
chat id from [@userinfobot](https://t.me/userinfobot). Put both in `.env`. You'll
get a ping when the watcher starts, when a slot is found, and when it books.

## Files

| file | purpose |
|------|---------|
| `watcher.py` | the service (modes: `run`, `login`, `check`, `book-test`) |
| `.env` | your config + credentials (git-ignored, `chmod 600`) |
| `Dockerfile` / `docker-compose.yml` | container build + run |
| `requirements.txt` | the single dependency: `curl_cffi` |
| `state/` | cached session, `BOOKED` sentinel, debug HTML dumps |

## Security note

The reverse-engineering was done from a HAR capture (`*.har`, git-ignored). HAR
files contain your **plaintext password and live session tokens** — never commit
them. `.env`, `*.har`, and `state/` are all in `.gitignore`.

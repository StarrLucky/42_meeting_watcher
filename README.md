# 42 apply — online meeting watcher

Watches [apply.42.tech](https://apply.42.tech) for an **Online (REMOTE)** meeting
slot to open on your path/step, and books it automatically the moment one frees up.

## How it works (and why a real browser)

The site is behind anti-bot protection — a plain `curl`/`requests` call to the
page or to `/api/swr` returns **HTTP 403**. So the watcher drives a real headless
**Chromium** via Playwright:

1. Logs in (Keycloak, from `.env`) — or reuses the saved session profile.
2. Every ~60s, calls the meeting JSON API via the browser context request stack
   (`page.request`, so it carries the login cookies) and parses `meetings[]`.
3. When a meeting with `location.type == "REMOTE"` and `full == false` appears,
   it opens the path page, clicks the slot's **SELECT MEETING** button and then
   **Confirm selection** in the dialog.
4. Notifies you (log + optional Telegram), writes a `state/BOOKED` sentinel, and
   stops so it never double-books.

## ⚠️ One booking at a time

The site only shows the meeting list while you have **no** meeting selected. If a
meeting is already booked, the API returns an empty list and the watcher just idles
(logs `meetings: (none)`). So: **don't book anything manually while waiting** — keep
your slot free so the watcher can grab the Online meeting the moment it opens. If you
ever book/cancel manually, the list reappears within a minute and the watcher resumes
automatically.

## Booking selectors (verified on the live logged-in page)

Both detection and booking use stable `data-testid`s discovered on the real page:

- **Slot button** (per meeting): `[data-testid="confirm-meeting-<MEETING_ID>-button"]`
  — the meeting id comes straight from the JSON API, so we always click the right
  slot. Full/unavailable slots render this button `disabled`.
- Clicking it opens a Radix confirm dialog.
- **Confirm** ("Confirm selection"): `[data-testid="confirm-modal-button"]`
- **Cancel** ("Back"): `[data-testid="close-modal-button"]`

Login is **Keycloak** (realm `42Next`); visiting the paths page redirects to the
login form, which the watcher fills from `.env`. The meeting API is called via the
browser context's request stack (`page.request`) so it works regardless of which
origin the page is currently on. Every booking attempt saves a screenshot + HTML
to `state/`.

## Setup on the Raspberry Pi

```bash
# from your Mac, copy the folder over (excludes .env/profile/state via rsync):
rsync -av --exclude .env --exclude profile --exclude state \
  ~/PycharmProjects/42-meeting-watcher/ ls@starrlucky.ddns.net:~/42-meeting-watcher/ -e 'ssh -p 33'

# then on the Pi:
ssh ls@starrlucky.ddns.net -p 33
cd ~/42-meeting-watcher
bash install.sh
nano .env        # set PASSWORD (and Telegram creds if you want phone alerts)
```

Requires **64-bit Raspberry Pi OS** (for `chromium-browser`). Check with `uname -m`
→ should be `aarch64`.

## Login options

- **Automatic** (default): `EMAIL`/`PASSWORD` in `.env`. The watcher signs in on
  start and re-auths if the session drops.
- **Manual, most robust** (handles CAPTCHA / 2FA): on a machine **with a display**,
  run `python watcher.py login`, sign in by hand; the session is saved into the
  `profile/` dir and reused. Copy `profile/` to the Pi if the Pi is headless.

## Test before trusting it

```bash
# 1) Can it log in and read meetings?
./.venv/bin/python watcher.py check

# 2) Dry run — locate the Online button but DON'T click confirm:
DRY_RUN=1 ./.venv/bin/python watcher.py run     # Ctrl-C to stop

# 3) Exercise the FULL click flow against an On-site slot (you cancel after):
./.venv/bin/python watcher.py book-test
```

## Run as a service

```bash
sudo cp 42-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now 42-watcher
journalctl -u 42-watcher -f            # live logs
```

To re-arm after a successful booking (or a test): `rm state/BOOKED` and restart.

## Telegram alerts (optional, recommended)

Message [@BotFather](https://t.me/BotFather) → `/newbot` → get the token. Get your
chat id from [@userinfobot](https://t.me/userinfobot). Put both in `.env`. You'll
get a ping when the watcher starts, when a slot is found, and when it books.

## Files

| file | purpose |
|------|---------|
| `watcher.py` | the service (modes: `run`, `login`, `check`, `book-test`) |
| `.env` | your config + credentials (git-ignored, `chmod 600`) |
| `install.sh` | Pi setup: system chromium + venv + Playwright |
| `42-watcher.service` | systemd unit |
| `state/` | logs, `BOOKED` sentinel, debug screenshots/HTML |

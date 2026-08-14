#!/usr/bin/env python3
"""
42 apply — online meeting slot watcher & auto-booker (pure HTTP, no browser).

Watches https://apply.42.tech for a REMOTE (Online) meeting slot to free up on a
given path/step, and books it automatically the moment one appears.

How it works (no Playwright / no Chromium):
  The site sits behind Cloudflare, so a plain `requests`/`curl` call is blocked
  (HTTP 403). We use `curl_cffi`, which speaks TLS with a real Chrome
  fingerprint (JA3), so Cloudflare lets us through with just the login cookies.

  * Login    — Keycloak (realm 42Next) + next-auth. We drive the exact redirect
               chain a browser would: GET a protected page -> Keycloak login
               form -> POST credentials -> next-auth callback sets the
               `next-auth.session-token` cookie. No PKCE/state involved.
  * Read     — GET /api/swr?href=/v1/apply/paths/<id>/steps/<step> (cookie auth).
  * Book     — a Next.js Server Action: POST /<locale>/paths?pathId=<id> with the
               `Next-Action` header and a JSON body [{"pathId","meetingId"}].

Usage:
  python watcher.py run          # main loop: watch + auto-book (default)
  python watcher.py check        # one-shot: print current meetings and exit
  python watcher.py login        # force a fresh login and save the session
  python watcher.py book-test    # force-book the first available ON_SITE slot
                                 #   (for testing the click flow; you cancel after)

All configuration is read from environment / a local .env file. See .env.example.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

from curl_cffi import requests as cffi

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency on python-dotenv)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_dotenv(HERE / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


BASE_URL = env("BASE_URL", "https://apply.42.tech").rstrip("/")
LOCALE = env("SITE_LOCALE", "en")
EMAIL = env("EMAIL", "")
PASSWORD = env("PASSWORD", "")
PATH_ID = env("PATH_ID", "af85d358-305d-4c7d-91df-7ed74b3ead50")
STEP = env("STEP", "PISCINE_MEETING")

# What we want to book: REMOTE (Online) by default. Use ON_SITE for testing.
TARGET_TYPE = env("TARGET_TYPE", "REMOTE").upper()

POLL_INTERVAL = int(env("POLL_INTERVAL", "60"))       # seconds between checks
POLL_JITTER = int(env("POLL_JITTER", "15"))           # +/- random seconds
DRY_RUN = env_bool("DRY_RUN", False)                  # detect but don't confirm

# curl_cffi browser impersonation target (TLS/JA3 + default headers).
IMPERSONATE = env("IMPERSONATE", "chrome")
HTTP_TIMEOUT = int(env("HTTP_TIMEOUT", "30"))

# Next.js Server Action ids. These are stable per site deployment but CHANGE
# when 42 rebuilds the app; if booking starts failing, re-capture them from the
# browser DevTools (Network -> the POST to /<locale>/paths -> "Next-Action"
# request header) and update .env.
BOOK_ACTION_ID = env("BOOK_ACTION_ID", "40d11247e4160267d637cc7e938b72b96033964518")
CANCEL_ACTION_ID = env("CANCEL_ACTION_ID", "40dfa9b6351b1482ff9ef246580415230e45575a08")
NEXT_STATE_TREE = env(
    "NEXT_STATE_TREE",
    "%5B%22%22%2C%7B%22children%22%3A%5B%5B%22locale%22%2C%22en%22%2C%22d%22%5D"
    "%2C%7B%22children%22%3A%5B%22(authenticated)%22%2C%7B%22children%22%3A%5B"
    "%22paths%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull"
    "%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D%7D"
    "%2Cnull%2Cnull%5D",
)

# Notifications (all optional)
TELEGRAM_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = env("TELEGRAM_CHAT_ID", "")

STATE_DIR = HERE / "state"
STATE_DIR.mkdir(exist_ok=True)
BOOKED_SENTINEL = STATE_DIR / "BOOKED"
COOKIES_FILE = STATE_DIR / "cookies.json"

PATHS_URL = f"{BASE_URL}/{LOCALE}/paths"
PATH_URL = f"{BASE_URL}/{LOCALE}/paths?pathId={PATH_ID}"
SWR_HREF = f"/v1/apply/paths/{PATH_ID}/steps/{STEP}"
SWR_URL = f"{BASE_URL}/api/swr?href={urllib.parse.quote(SWR_HREF, safe='')}"
SESSION_URL = f"{BASE_URL}/api/auth/session"


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def notify(msg: str) -> None:
    """Log everywhere + push to Telegram if configured."""
    log(f"NOTIFY: {msg}")
    try:
        (STATE_DIR / "last_notify.txt").write_text(msg, encoding="utf-8")
    except Exception:
        pass
    if TELEGRAM_TOKEN and TELEGRAM_CHAT:
        try:
            data = urllib.parse.urlencode(
                {"chat_id": TELEGRAM_CHAT, "text": msg}
            ).encode()
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
        except Exception as exc:  # notifications must never crash the watcher
            log(f"telegram notify failed: {exc}")


def fmt_meeting(m: dict) -> str:
    loc = (m.get("location") or {}).get("type", "?")
    return f"{m.get('startDate')} [{loc}] full={m.get('full')} id={m.get('id')}"


# --------------------------------------------------------------------------- #
# HTTP session
# --------------------------------------------------------------------------- #

def new_session() -> cffi.Session:
    """A curl_cffi session that impersonates Chrome (TLS + default headers) so
    Cloudflare lets us through, and keeps cookies across requests."""
    return cffi.Session(impersonate=IMPERSONATE, timeout=HTTP_TIMEOUT)


def save_cookies(s: cffi.Session) -> None:
    try:
        jar = {k: v for k, v in s.cookies.get_dict().items()}
        COOKIES_FILE.write_text(json.dumps(jar), encoding="utf-8")
    except Exception as exc:
        log(f"could not persist cookies: {exc}")


def load_cookies(s: cffi.Session) -> bool:
    if not COOKIES_FILE.exists():
        return False
    try:
        jar = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        domain = urllib.parse.urlparse(BASE_URL).hostname or "apply.42.tech"
        for k, v in jar.items():
            s.cookies.set(k, v, domain=domain)
        return bool(jar)
    except Exception as exc:
        log(f"could not load cookies: {exc}")
        return False


# --------------------------------------------------------------------------- #
# Authentication (Keycloak + next-auth, no browser)
# --------------------------------------------------------------------------- #

def _looks_like_login(text: str) -> bool:
    return "login-actions/authenticate" in text or "kc-form-login" in text


def _find_login_action(text: str) -> str | None:
    """The 42 Keycloak theme renders the form in JS, so there is no
    <form action=...> in the HTML. The submit URL lives in the embedded
    kcContext JSON as "loginAction". Fall back to a classic <form action>."""
    m = re.search(r'"loginAction"\s*:\s*"([^"]+)"', text)
    if m:
        return html.unescape(m.group(1)).replace("\\/", "/")
    m = re.search(r'action="([^"]*login-actions/authenticate[^"]*)"', text, re.I)
    return html.unescape(m.group(1)) if m else None


def check_session(s: cffi.Session) -> bool:
    """A valid session => /api/swr returns 200 JSON with a meetings payload."""
    try:
        r = s.get(SWR_URL, headers={"accept": "application/json"})
    except Exception as exc:
        log(f"session check request error: {exc}")
        return False
    if r.status_code != 200:
        log(f"session check: status={r.status_code} (need login)")
        return False
    try:
        json.loads(r.text)
        return True
    except Exception:
        return False


def do_login(s: cffi.Session) -> bool:
    """Drive the Keycloak login form the way a browser would."""
    if not EMAIL or not PASSWORD:
        log("no EMAIL/PASSWORD set; cannot log in.")
        return False

    log("logging in (Keycloak) ...")
    try:
        r = s.get(PATHS_URL, allow_redirects=True)
    except Exception as exc:
        log(f"failed to reach login page: {exc}")
        return False

    if not _looks_like_login(r.text):
        # Already authenticated (cookies still valid) — nothing to do.
        if check_session(s):
            log("already had a valid session")
            return True
        log("did not land on the Keycloak login form; unexpected page.")
        _dump_debug(r.text, "login-page")
        return False

    action = _find_login_action(r.text)
    if not action:
        log("could not find the Keycloak login form action.")
        _dump_debug(r.text, "login-page")
        return False

    try:
        r2 = s.post(
            action,
            data={"username": EMAIL, "password": PASSWORD, "credentialId": ""},
            headers={"content-type": "application/x-www-form-urlencoded"},
            allow_redirects=True,
        )
    except Exception as exc:
        log(f"credential POST failed: {exc}")
        return False

    if _looks_like_login(r2.text):
        log("login rejected (still on the login form) — check EMAIL/PASSWORD.")
        _dump_debug(r2.text, "login-rejected")
        return False

    if check_session(s):
        save_cookies(s)
        log("login OK, session saved")
        return True

    log("login flow finished but session is not valid.")
    _dump_debug(r2.text, "login-verify-failed")
    return False


def ensure_session(s: cffi.Session) -> bool:
    if check_session(s):
        return True
    return do_login(s)


def _dump_debug(text: str, tag: str) -> None:
    try:
        (STATE_DIR / f"debug-{tag}.html").write_text(text, encoding="utf-8")
        log(f"debug artifact saved: debug-{tag}.html")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Reading meetings
# --------------------------------------------------------------------------- #

def fetch_meetings(s: cffi.Session) -> tuple[int, list[dict], str]:
    try:
        r = s.get(SWR_URL, headers={"accept": "application/json"})
    except Exception as exc:
        return 0, [], f"request error: {exc}"
    if r.status_code != 200:
        return r.status_code, [], r.text
    try:
        data = json.loads(r.text)
        return 200, (data.get("meetings", []) or []), r.text
    except Exception:
        return 200, [], r.text


# --------------------------------------------------------------------------- #
# Booking (Next.js Server Action)
# --------------------------------------------------------------------------- #

def _server_action(s: cffi.Session, action_id: str, payload: list) -> cffi.Response:
    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": action_id,
        "next-router-state-tree": NEXT_STATE_TREE,
        "origin": BASE_URL,
        "referer": PATH_URL,
    }
    return s.post(PATH_URL, data=json.dumps(payload), headers=headers)


def attempt_booking(s: cffi.Session, meeting: dict) -> bool:
    """Book a specific meeting via the site's 'select meeting' server action."""
    mid = meeting.get("id")
    log(f"booking meeting {fmt_meeting(meeting)}")

    if DRY_RUN:
        notify(f"[DRY_RUN] Would POST book action for {fmt_meeting(meeting)}. "
               f"Nothing booked.")
        return False

    try:
        r = _server_action(
            s, BOOK_ACTION_ID, [{"pathId": PATH_ID, "meetingId": mid}]
        )
    except Exception as exc:
        notify(f"booking request errored for {fmt_meeting(meeting)}: {exc}")
        return False

    body = r.text or ""
    if r.status_code != 200:
        _dump_debug(body, "book-http-error")
        notify(f"Booking returned HTTP {r.status_code} for {fmt_meeting(meeting)}. "
               f"The Next-Action id may be stale — re-capture BOOK_ACTION_ID. "
               f"BOOK IT MANUALLY.")
        return False

    # The RSC stream sometimes carries a server error. Sanity-check by re-reading
    # the meeting list: the site hides the list once a meeting is selected, and
    # a booked slot is no longer offered as available.
    time.sleep(2)
    status, meetings, _ = fetch_meetings(s)
    still_available = any(
        m.get("id") == mid and not m.get("full", True) for m in meetings
    )
    if still_available:
        _dump_debug(body, "book-not-applied")
        notify(f"Sent booking for {fmt_meeting(meeting)} but the slot is still "
               f"listed as available. VERIFY MANUALLY. Response head: {body[:200]}")
        return False

    notify(f"✅ Booked {TARGET_TYPE} meeting {fmt_meeting(meeting)}. "
           f"Post-list has {len(meetings)} entries. VERIFY in your account.")
    return True


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def pick_target(meetings: list[dict]) -> dict | None:
    for m in meetings:
        loc = (m.get("location") or {}).get("type")
        if loc == TARGET_TYPE and not m.get("full", True):
            return m
    return None


def _bootstrap_session() -> cffi.Session | None:
    s = new_session()
    load_cookies(s)
    if not ensure_session(s):
        return None
    return s


def mode_run() -> int:
    if BOOKED_SENTINEL.exists():
        log(f"{BOOKED_SENTINEL} exists — already booked. Delete it to run again.")
        return 0

    notify(f"watcher started: target={TARGET_TYPE} path={PATH_ID} step={STEP} "
           f"interval={POLL_INTERVAL}s dry_run={DRY_RUN}")

    s = _bootstrap_session()
    if s is None:
        notify("Could not establish a login session. Check EMAIL/PASSWORD in .env.")
        return 2

    consecutive_errors = 0
    while True:
        try:
            status, meetings, _ = fetch_meetings(s)
            if status != 200:
                log(f"API status={status}; re-authenticating")
                if not do_login(s):
                    raise RuntimeError("re-auth failed")
                status, meetings, _ = fetch_meetings(s)

            consecutive_errors = 0
            summary = ", ".join(fmt_meeting(m) for m in meetings) or "(none)"
            log(f"meetings: {summary}")

            target = pick_target(meetings)
            if target:
                notify(f"🎯 {TARGET_TYPE} slot available: {fmt_meeting(target)} — booking!")
                ok = attempt_booking(s, target)
                if ok and not DRY_RUN:
                    BOOKED_SENTINEL.write_text(json.dumps(target, indent=2), encoding="utf-8")
                    log("booked (sentinel written) — stopping loop.")
                    return 0
                # DRY_RUN or failed booking: keep watching so we don't miss it.
        except Exception as exc:
            consecutive_errors += 1
            log(f"loop error ({consecutive_errors}): {exc}")
            if consecutive_errors >= 5:
                notify(f"watcher failing repeatedly: {exc}. Restarting process.")
                return 1  # let the container/systemd restart us fresh

        sleep_for = max(10, POLL_INTERVAL + random.randint(-POLL_JITTER, POLL_JITTER))
        time.sleep(sleep_for)


def mode_login() -> int:
    s = new_session()
    if do_login(s):
        return 0
    log("login failed.")
    return 1


def mode_check() -> int:
    s = _bootstrap_session()
    if s is None:
        log("not logged in.")
        return 2
    status, meetings, _ = fetch_meetings(s)
    print(json.dumps({"status": status, "meetings": meetings}, indent=2))
    return 0


def mode_book_test() -> int:
    """Force-book the first available ON_SITE slot to exercise the flow."""
    global TARGET_TYPE
    TARGET_TYPE = "ON_SITE"
    log("book-test: targeting first available ON_SITE slot (you cancel afterwards).")
    s = _bootstrap_session()
    if s is None:
        log("not logged in.")
        return 2
    status, meetings, _ = fetch_meetings(s)
    log("meetings: " + (", ".join(fmt_meeting(m) for m in meetings) or "(none)"))
    target = pick_target(meetings)
    if not target:
        log("no available ON_SITE slot to test with.")
        return 1
    attempt_booking(s, target)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="42 apply online-meeting watcher (HTTP)")
    parser.add_argument(
        "mode", nargs="?", default="run",
        choices=["run", "login", "check", "book-test"],
    )
    args = parser.parse_args()
    return {
        "run": mode_run,
        "login": mode_login,
        "check": mode_check,
        "book-test": mode_book_test,
    }[args.mode]()


if __name__ == "__main__":
    sys.exit(main())


#!/usr/bin/env python3
"""
42 apply — online meeting slot watcher & auto-booker.

Watches https://apply.42.tech for a REMOTE (Online) meeting slot to free up on a
given path/step, and books it automatically the moment one appears.

Why a real browser (Playwright) instead of plain HTTP requests?
  The site is behind anti-bot protection: a plain GET to the page or the
  /api/swr endpoint returns HTTP 403. A real Chromium instance passes those
  checks, keeps the login session, and lets us call the JSON API from inside
  the page (same cookies/headers as a normal user).

Usage:
  python watcher.py run          # main loop: watch + auto-book (default)
  python watcher.py login        # open a visible browser once to log in by hand
  python watcher.py check        # one-shot: print current meetings and exit
  python watcher.py book-test    # force-book the first available ON_SITE slot
                                 #   (for testing the click flow; you cancel after)

All configuration is read from environment / a local .env file. See .env.example.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import random
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_dotenv(HERE / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


BASE_URL = env("BASE_URL", "https://apply.42.tech")
LOCALE = env("SITE_LOCALE", "en")
EMAIL = env("EMAIL", "")
PASSWORD = env("PASSWORD", "")
PATH_ID = env("PATH_ID", "af85d358-305d-4c7d-91df-7ed74b3ead50")
STEP = env("STEP", "PISCINE_MEETING")

# What we want to book: REMOTE (Online) by default. Use ON_SITE for testing.
TARGET_TYPE = env("TARGET_TYPE", "REMOTE").upper()

POLL_INTERVAL = int(env("POLL_INTERVAL", "60"))       # seconds between checks
POLL_JITTER = int(env("POLL_JITTER", "15"))            # +/- random seconds
HEADLESS = env_bool("HEADLESS", True)
DRY_RUN = env_bool("DRY_RUN", False)                   # locate button but don't confirm
CHROMIUM_PATH = env("CHROMIUM_PATH", "")               # e.g. /usr/bin/chromium-browser
USER_DATA_DIR = env("USER_DATA_DIR", str(HERE / "profile"))
USER_AGENT = env(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
CONFIRM_TEXT = env(
    "CONFIRM_TEXT_REGEX",
    r"confirm|validate|valider|je confirme|book|register|s.inscrire|yes|ok",
)

# Notifications (all optional)
TELEGRAM_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = env("TELEGRAM_CHAT_ID", "")

STATE_DIR = HERE / "state"
STATE_DIR.mkdir(exist_ok=True)
BOOKED_SENTINEL = STATE_DIR / "BOOKED"

PATHS_URL = f"{BASE_URL}/{LOCALE}/paths"
PATH_URL = f"{BASE_URL}/{LOCALE}/paths?pathId={PATH_ID}"
SWR_HREF = f"/v1/apply/paths/{PATH_ID}/steps/{STEP}"
SWR_URL = f"{BASE_URL}/api/swr?href={urllib.parse.quote(SWR_HREF, safe='')}"


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def notify(msg: str) -> None:
    """Log everywhere + push to Telegram if configured."""
    log(f"NOTIFY: {msg}")
    (STATE_DIR / "last_notify.txt").write_text(msg, encoding="utf-8")
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
    return (
        f"{m.get('startDate')} [{loc}] "
        f"full={m.get('full')} id={m.get('id')}"
    )


def paris_parts(iso_utc: str, tz_name: str) -> list[str]:
    """Return human-readable substrings likely present on the rendered card
    (day number, hour:minute in the campus timezone), for disambiguation."""
    parts: list[str] = []
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if ZoneInfo and tz_name:
            dt = dt.astimezone(ZoneInfo(tz_name))
        parts.append(str(dt.day))
        parts.append(dt.strftime("%H:%M"))
    except Exception:
        pass
    return parts


# --------------------------------------------------------------------------- #
# Browser session
# --------------------------------------------------------------------------- #

def launch_context(pw, headless: bool):
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    kwargs = dict(
        headless=headless,
        args=args,
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="Europe/Paris",
        viewport={"width": 1366, "height": 900},
    )
    if CHROMIUM_PATH:
        kwargs["executable_path"] = CHROMIUM_PATH
    # Persistent context => cookies/login survive restarts and it looks like a
    # normal browser profile (better against anti-bot).
    return pw.chromium.launch_persistent_context(USER_DATA_DIR, **kwargs)


def fetch_meetings(page) -> tuple[int, list[dict], str]:
    """Call the JSON API via the browser context's request stack.

    We use page.request (not an in-page fetch) because the current page may be
    on the Keycloak login origin; a same-origin fetch would be CORS-blocked
    ("Failed to fetch"). page.request carries the context cookies and is not
    subject to page CORS. Returns (status, meetings, raw_body)."""
    try:
        r = page.request.get(SWR_URL, headers={"accept": "application/json"})
        status = r.status
        body = r.text()
    except Exception as exc:
        return 0, [], f"request error: {exc}"
    meetings: list[dict] = []
    if status == 200:
        try:
            data = json.loads(body)
            meetings = data.get("meetings", []) or []
        except Exception:
            pass
    return status, meetings, body


def is_logged_in(page) -> bool:
    status, _, body = fetch_meetings(page)
    if status == 200:
        return True
    log(f"session check: status={status} (need login)")
    return False


def do_login(page) -> bool:
    """Automated login. The site uses Keycloak (realm 42Next); visiting the
    paths page redirects straight to the Keycloak login form."""
    import re
    if not EMAIL or not PASSWORD:
        log("no EMAIL/PASSWORD set; cannot auto-login. Run: python watcher.py login")
        return False

    log("attempting automated sign-in (Keycloak) ...")
    page.goto(PATHS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    if is_logged_in(page):
        log("already had a valid session")
        return True

    # If we're not already on Keycloak, a "Sign in" control may lead there.
    url = page.url
    if "keycloak" not in url and "openid-connect" not in url:
        try:
            signin = page.get_by_role(
                "button", name=re.compile(r"sign\s*in|log\s*in|se\s*connecter", re.I)
            )
            if signin.count() == 0:
                signin = page.get_by_role(
                    "link", name=re.compile(r"sign\s*in|log\s*in|se\s*connecter", re.I)
                )
            if signin.count() > 0:
                signin.first.click()
                page.wait_for_timeout(3000)
        except Exception as exc:
            log(f"sign-in button click skipped: {exc}")

    # Fill the Keycloak form (id=username / id=password / id=kc-login).
    try:
        user_box = page.locator(
            "input#username, input[name='username'], input[type='email'], "
            "input[name*='email' i], input[id*='email' i]"
        ).first
        user_box.wait_for(state="visible", timeout=15000)
        user_box.fill(EMAIL)

        pass_box = page.locator(
            "input#password, input[type='password'], input[name='password']"
        ).first
        pass_box.fill(PASSWORD)
    except Exception as exc:
        _debug_dump(page, "login-fill-failed")
        log(f"could not fill credentials: {exc}")
        return False

    # Submit.
    try:
        submit = page.locator(
            "input#kc-login, button#kc-login, button[type='submit'], input[type='submit']"
        )
        if submit.count() > 0:
            submit.first.click()
        else:
            pass_box.press("Enter")
    except Exception as exc:
        log(f"submit click failed, trying Enter: {exc}")
        try:
            pass_box.press("Enter")
        except Exception:
            pass

    # Wait for the redirect back to apply.42.tech to settle.
    for _ in range(6):
        page.wait_for_timeout(2000)
        if is_logged_in(page):
            log("automated sign-in OK")
            return True
    _debug_dump(page, "login-verify-failed")
    log("automated sign-in did NOT establish a session")
    return False


def ensure_session(page) -> bool:
    if is_logged_in(page):
        return True
    return do_login(page)


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #

def _debug_dump(page, tag: str) -> None:
    try:
        png = STATE_DIR / f"debug-{tag}.png"
        html = STATE_DIR / f"debug-{tag}.html"
        page.screenshot(path=str(png), full_page=True)
        html.write_text(page.content(), encoding="utf-8")
        log(f"debug artifacts saved: {png.name}, {html.name}")
    except Exception as exc:
        log(f"debug dump failed: {exc}")


def attempt_booking(page, meeting: dict) -> bool:
    """Book a specific meeting using the site's stable test-ids (discovered on
    the live logged-in page):
      card button  -> [data-testid="confirm-meeting-<ID>-button"]
      dialog opens (Radix), confirm CTA "Confirm selection":
                      [data-testid="confirm-modal-button"]
      cancel        -> [data-testid="close-modal-button"]  (text "Back")
    """
    mid = meeting.get("id")
    card_sel = f'[data-testid="confirm-meeting-{mid}-button"]'
    confirm_sel = '[data-testid="confirm-modal-button"]'
    cancel_sel = '[data-testid="close-modal-button"]'

    log(f"opening booking page for meeting {fmt_meeting(meeting)}")
    page.goto(PATH_URL, wait_until="domcontentloaded")

    try:
        page.wait_for_selector(card_sel, timeout=30000)
    except PWTimeout:
        _debug_dump(page, "no-card")
        notify(f"Slot {fmt_meeting(meeting)} is free but its button was not found "
               f"({card_sel}). Book it MANUALLY now.")
        return False

    card_btn = page.locator(card_sel).first
    if card_btn.is_disabled():
        log("card button is disabled — slot not actually bookable yet; will retry.")
        return False

    card_btn.click()

    try:
        page.wait_for_selector(confirm_sel, timeout=15000)
    except PWTimeout:
        _debug_dump(page, "no-confirm-dialog")
        notify(f"Clicked the meeting for {fmt_meeting(meeting)} but the confirm dialog "
               f"did not appear. CHECK MANUALLY.")
        return False

    if DRY_RUN:
        _debug_dump(page, "dry-run-dialog")
        try:
            page.click(cancel_sel)
        except Exception:
            pass
        notify(f"[DRY_RUN] Opened the confirm dialog for {fmt_meeting(meeting)} and "
               f"clicked Back. Nothing booked.")
        return False

    page.click(confirm_sel)
    page.wait_for_timeout(4000)
    _debug_dump(page, "after-confirm")

    status, meetings, _ = fetch_meetings(page)
    post = next((m for m in meetings if m.get("id") == mid), None)
    log(f"post-booking API status={status} target_state={post}")
    notify(
        f"✅ Booked {TARGET_TYPE} meeting {fmt_meeting(meeting)} "
        f"(clicked 'Confirm selection'). Post-state: {post}. VERIFY in your account."
    )
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


def mode_run() -> int:
    if BOOKED_SENTINEL.exists():
        log(f"{BOOKED_SENTINEL} exists — already booked. Delete it to run again. Exiting.")
        return 0

    notify(f"watcher started: target={TARGET_TYPE} path={PATH_ID} step={STEP} "
           f"interval={POLL_INTERVAL}s headless={HEADLESS} dry_run={DRY_RUN}")

    with sync_playwright() as pw:
        ctx = launch_context(pw, headless=HEADLESS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PATHS_URL, wait_until="domcontentloaded")

        if not ensure_session(page):
            notify("Could not establish a login session. "
                   "Run `python watcher.py login` on a machine with a display.")
            ctx.close()
            return 2

        consecutive_errors = 0
        while True:
            try:
                status, meetings, body = fetch_meetings(page)
                if status != 200:
                    log(f"API status={status}; re-authenticating")
                    if not ensure_session(page):
                        raise RuntimeError("re-auth failed")
                    status, meetings, body = fetch_meetings(page)

                consecutive_errors = 0
                summary = ", ".join(fmt_meeting(m) for m in meetings) or "(none)"
                log(f"meetings: {summary}")

                target = pick_target(meetings)
                if target:
                    notify(f"🎯 {TARGET_TYPE} slot available: {fmt_meeting(target)} — booking!")
                    ok = attempt_booking(page, target)
                    if ok and not DRY_RUN:
                        BOOKED_SENTINEL.write_text(
                            json.dumps(target, indent=2), encoding="utf-8"
                        )
                        log("booked (sentinel written) — stopping loop.")
                        ctx.close()
                        return 0
                    # DRY_RUN or failed booking: keep watching so we don't miss it.
            except Exception as exc:
                consecutive_errors += 1
                log(f"loop error ({consecutive_errors}): {exc}")
                _debug_dump(page, "loop-error")
                if consecutive_errors >= 5:
                    notify(f"watcher failing repeatedly: {exc}. Restarting process.")
                    ctx.close()
                    return 1  # let systemd restart us fresh

            sleep_for = POLL_INTERVAL + random.randint(-POLL_JITTER, POLL_JITTER)
            sleep_for = max(10, sleep_for)
            time.sleep(sleep_for)


def mode_login() -> int:
    """Open a visible browser so the user can log in by hand (solves CAPTCHA/2FA).
    The session is saved into the persistent profile dir for `run` to reuse."""
    log("opening a visible browser — sign in, then close the window.")
    with sync_playwright() as pw:
        ctx = launch_context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PATHS_URL, wait_until="domcontentloaded")
        log("Waiting up to 5 minutes for you to sign in...")
        ok = False
        for _ in range(60):
            page.wait_for_timeout(5000)
            try:
                if is_logged_in(page):
                    ok = True
                    break
            except Exception:
                pass
        if ok:
            notify("login session saved to profile. You can now run the watcher.")
        else:
            log("did not detect a logged-in session (you can still try `run`).")
        ctx.close()
    return 0 if ok else 1


def mode_check() -> int:
    with sync_playwright() as pw:
        ctx = launch_context(pw, headless=HEADLESS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PATHS_URL, wait_until="domcontentloaded")
        if not ensure_session(page):
            log("not logged in.")
            ctx.close()
            return 2
        status, meetings, body = fetch_meetings(page)
        print(json.dumps({"status": status, "meetings": meetings}, indent=2))
        ctx.close()
    return 0


def mode_book_test() -> int:
    """Force-book the first available ON_SITE slot to exercise the click flow."""
    global TARGET_TYPE
    TARGET_TYPE = "ON_SITE"
    log("book-test: targeting first available ON_SITE slot (you cancel afterwards).")
    with sync_playwright() as pw:
        ctx = launch_context(pw, headless=HEADLESS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PATHS_URL, wait_until="domcontentloaded")
        if not ensure_session(page):
            log("not logged in.")
            ctx.close()
            return 2
        status, meetings, body = fetch_meetings(page)
        log(f"meetings: " + (", ".join(fmt_meeting(m) for m in meetings) or "(none)"))
        target = pick_target(meetings)
        if not target:
            log("no available ON_SITE slot to test with.")
            ctx.close()
            return 1
        attempt_booking(page, target)
        ctx.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="42 apply online-meeting watcher")
    parser.add_argument(
        "mode",
        nargs="?",
        default="run",
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

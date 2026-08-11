#!/usr/bin/env bash
# One-shot setup on the Raspberry Pi. Run from the project directory.
set -euo pipefail

echo "==> Installing system Chromium (reliable on Raspberry Pi OS / ARM)"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip chromium-browser || \
  sudo apt-get install -y python3-venv python3-pip chromium

echo "==> Locating chromium"
CHROMIUM_BIN="$(command -v chromium-browser || command -v chromium || true)"
echo "chromium: ${CHROMIUM_BIN:-NOT FOUND}"

echo "==> Python venv + Playwright"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
# We use the *system* chromium, so we only need Playwright's python driver,
# not its bundled browser download. (Bundled ARM chromium is unreliable.)

if [ ! -f .env ]; then
  echo "==> Creating .env from template — EDIT IT and set PASSWORD"
  cp .env.example .env
  chmod 600 .env
fi

if [ -n "${CHROMIUM_BIN:-}" ]; then
  echo "==> Pointing .env at system chromium: $CHROMIUM_BIN"
  if grep -q '^CHROMIUM_PATH=' .env; then
    sed -i "s#^CHROMIUM_PATH=.*#CHROMIUM_PATH=${CHROMIUM_BIN}#" .env
  else
    echo "CHROMIUM_PATH=${CHROMIUM_BIN}" >> .env
  fi
fi

mkdir -p state
echo
echo "==> Done. Next steps:"
echo "   1) Edit .env  (set PASSWORD, and Telegram token/chat id if you want alerts)"
echo "   2) One-shot check:   ./.venv/bin/python watcher.py check"
echo "   3) Test the click:   DRY_RUN=1 ./.venv/bin/python watcher.py run   (Ctrl-C to stop)"
echo "   4) Install service:  sudo cp 42-watcher.service /etc/systemd/system/"
echo "                        sudo systemctl daemon-reload"
echo "                        sudo systemctl enable --now 42-watcher"
echo "   5) Watch logs:       journalctl -u 42-watcher -f   (or tail state/watcher.log)"

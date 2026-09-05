#!/usr/bin/env bash
#
# One-shot installer for a Raspberry Pi.
#
#   ./deploy/install.sh
#
# Creates the venv, installs deps, seeds config.yaml and .env if they are
# missing, and installs the systemd units. It does NOT start the service — you
# should run a paper cycle by hand first.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PYTHON="${PYTHON:-python3}"

echo "==> t212-bot install"
echo "    directory: $APP_DIR"
echo "    user:      $RUN_USER"

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "!! Python 3.11+ is required (found: $($PYTHON --version))" >&2
    exit 1
fi

# ---------------------------------------------------------------- virtualenv
echo "==> creating the virtualenv"
"$PYTHON" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "    done"

# -------------------------------------------------------------------- config
mkdir -p "$APP_DIR/data"

if [ ! -f "$APP_DIR/config.yaml" ]; then
    cp "$APP_DIR/config.yaml.example" "$APP_DIR/config.yaml"
    echo "==> created config.yaml from the example — EDIT THE WATCH-LIST before running"
fi

if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "==> created .env from the example — ADD YOUR KEYS"
else
    chmod 600 "$APP_DIR/.env"
fi

# ------------------------------------------------------------------- systemd
if [ "$(id -u)" -ne 0 ]; then
    echo
    echo "==> not running as root; skipping systemd installation."
    echo "    Re-run with sudo to install the services:  sudo ./deploy/install.sh"
else
    for unit in t212-bot t212-bot-dashboard; do
        sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" \
            "$APP_DIR/deploy/$unit.service" > "/etc/systemd/system/$unit.service"
        echo "==> installed /etc/systemd/system/$unit.service"
    done
    systemctl daemon-reload
    echo "    systemd reloaded (services are NOT enabled or started yet)"
fi

cat <<EOF

Next steps, in this order:

  1. Edit .env             — add T212_API_KEY / T212_API_SECRET and an AI key.
                             Leave MODE=paper.
  2. Edit config.yaml      — set your watch-list. Find exact tickers with:
                               $APP_DIR/.venv/bin/python -m scripts.list_instruments vusa
  3. Check credentials:
                               $APP_DIR/.venv/bin/python -m scripts.check_auth
  4. Run one cycle by hand, placing nothing:
                               $APP_DIR/.venv/bin/python -m t212bot.main --dry-run --force
  5. Run one real paper cycle:
                               $APP_DIR/.venv/bin/python -m t212bot.main --once --force
  6. When you are happy, start the scheduler:
                               sudo systemctl enable --now t212-bot
                               sudo systemctl enable --now t212-bot-dashboard
  7. Watch it:               journalctl -u t212-bot -f
                             http://\$(hostname -i | awk '{print \$1}'):8080/

Kill switch:  touch $APP_DIR/STOP        (halts every cycle, service still runs)
Resume:       rm $APP_DIR/STOP
Status:       $APP_DIR/.venv/bin/python -m t212bot.main --status
EOF

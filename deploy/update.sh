#!/usr/bin/env bash
#
# Pull the latest code and restart the running services so config.yaml/.env
# and code changes actually take effect.
#
#   ./deploy/update.sh
#
# Config (config.yaml, .env) is only read once, at process start-up — editing
# it does nothing to an already-running service until it is restarted. This
# script exists so "git pull" and "restart" always happen together.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> t212-bot update ($APP_DIR)"

# ------------------------------------------------------------------- update
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "!! local changes to tracked files — refusing to pull over them:" >&2
    git status --short --untracked-files=no >&2
    echo "!! commit, stash, or discard them first, then re-run." >&2
    exit 1
fi

echo "==> fetching"
git fetch origin

echo "==> fast-forwarding to origin/$(git rev-parse --abbrev-ref HEAD)"
if ! git pull --ff-only; then
    echo "!! could not fast-forward (local and remote have diverged)." >&2
    echo "!! resolve this by hand (git log, git merge/rebase), then re-run." >&2
    exit 1
fi

echo "==> installing/updating dependencies"
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> synchronizing environment and configuration"
"$APP_DIR/.venv/bin/python" -m scripts.sync_config_env

# ------------------------------------------------------------------ sanity
echo "==> checking config.yaml loads cleanly before touching the service"
if ! "$APP_DIR/.venv/bin/python" -m t212bot.main --status >/tmp/t212bot-update-status.log 2>&1; then
    echo "!! config.yaml/.env failed to load — NOT restarting the service." >&2
    cat /tmp/t212bot-update-status.log >&2
    exit 1
fi

# -------------------------------------------------------------------- restart
echo "==> restarting services (needs sudo)"
sudo systemctl restart t212-bot t212-bot-dashboard

sleep 2
echo
echo "==> status"
systemctl --no-pager --lines=0 status t212-bot t212-bot-dashboard || true
echo
echo "Follow logs with:  journalctl -u t212-bot -f"

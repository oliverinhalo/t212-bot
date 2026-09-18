#!/usr/bin/env bash
#
# Update everything: code, dependencies, config, the bot service and the
# dashboard ("the website"), then prove all of it came back up.
#
#   ./deploy/update.sh                # update the branch you are on
#   ./deploy/update.sh main           # switch to main first, then update
#
# Config (config.yaml, .env) is only read once, at process start-up — editing
# it does nothing to an already-running service until it is restarted. This
# script exists so "git pull", "edit config" and "restart" always happen
# together, and so a broken config is caught *before* anything is restarted.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

PY="$APP_DIR/.venv/bin/python"
PIP="$APP_DIR/.venv/bin/pip"
TARGET_BRANCH="${1:-}"
SERVICES=(t212-bot t212-bot-dashboard)

fail() { echo "!! $*" >&2; exit 1; }

[ -x "$PY" ] || fail "no virtualenv at $APP_DIR/.venv — run ./deploy/install.sh first"

echo "==> t212-bot update ($APP_DIR)"

# ------------------------------------------------------------------- update
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "!! local changes to tracked files — refusing to pull over them:" >&2
    git status --short --untracked-files=no >&2
    fail "commit, stash, or discard them first, then re-run."
fi

echo "==> fetching"
git fetch origin --prune

if [ -n "$TARGET_BRANCH" ] && [ "$TARGET_BRANCH" != "$(git rev-parse --abbrev-ref HEAD)" ]; then
    echo "==> switching to $TARGET_BRANCH"
    git checkout "$TARGET_BRANCH" || fail "could not check out $TARGET_BRANCH"
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> fast-forwarding to origin/$BRANCH"
if ! git pull --ff-only; then
    echo "!! could not fast-forward (local and remote have diverged)." >&2
    fail "resolve this by hand (git log, git merge/rebase), then re-run."
fi
echo "    now at $(git log --oneline -1)"

echo "==> installing/updating dependencies"
"$PIP" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> synchronizing environment and configuration"
# Only ever ADDS keys that are new in the .example files; your own values and
# comments are left alone.
"$PY" -m scripts.sync_config_env

# ------------------------------------------------------------------ sanity
# A bad config must stop the update here, while the old (working) processes
# are still running, rather than after they have been restarted into it.
echo "==> checking config.yaml loads cleanly before touching the services"
STATUS_LOG="$(mktemp)"
if ! "$PY" -m t212bot.main --status >"$STATUS_LOG" 2>&1; then
    cat "$STATUS_LOG" >&2
    rm -f "$STATUS_LOG"
    fail "config.yaml/.env failed to load — NOTHING was restarted."
fi
sed 's/^/    /' "$STATUS_LOG"
rm -f "$STATUS_LOG"

# -------------------------------------------------------------------- restart
echo "==> restarting services (needs sudo)"
sudo systemctl restart "${SERVICES[@]}"

# --------------------------------------------------------------------- verify
echo "==> verifying"
sleep 3
failed=0
for unit in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$unit"; then
        echo "    $unit: active"
    else
        echo "    $unit: NOT RUNNING" >&2
        journalctl -u "$unit" --no-pager --lines=20 >&2 || true
        failed=1
    fi
done

# The dashboard is only useful if it actually answers, so ask it.
# Read the port straight from the YAML rather than through config.load(), so
# this still works if the secrets it would validate are not in this shell.
PORT="$("$PY" -c "import yaml; print((yaml.safe_load(open('config.yaml')) or {}).get('dashboard', {}).get('port', 8080))" 2>/dev/null || echo 8080)"
if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "    dashboard: answering on port ${PORT}"
    echo
    echo "Dashboard: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/"
else
    echo "    dashboard: no answer on port ${PORT}" >&2
    failed=1
fi

echo
if [ "$failed" -ne 0 ]; then
    fail "update finished but something is not healthy — see above."
fi

echo "==> done. Follow the bot with:  journalctl -u t212-bot -f"

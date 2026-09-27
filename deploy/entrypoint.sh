#!/usr/bin/env bash
# Container entrypoint. Runs as root under tini, only long enough to hand the
# volume to unprivileged users, then starts two processes:
#
#   phil  deploy/supervisor.py  schedules Claude Code sessions via loop.sh
#                                (Claude Code refuses to skip permissions as
#                                root, and nothing here needs root anyway)
#   web   dashboard/server.py   the public page: its own user, read-only
#                                access to the run, and no secrets in its
#                                environment
#
# If either exits, the container exits and Railway's restart policy brings it
# back. SIGTERM (a redeploy) is passed on; the supervisor then drains.
set -euo pipefail

DATA="${RAILWAY_VOLUME_MOUNT_PATH:-/data}"
export PHIL_HOME="${PHIL_HOME:-$DATA/phil}"
export PHIL_RUNTIME="${PHIL_RUNTIME:-$DATA/runtime}"

mkdir -p "$DATA"
# Railway mounts volumes root-owned. Hand it to phil once; later boots skip
# the recursive walk because the owner already matches.
if [ "$(stat -c %U "$DATA")" != phil ]; then
  chown -R phil:phil "$DATA"
fi
chmod 755 "$DATA"
# Runtime dirs always end up phil-owned and world-readable (the dashboard
# reads them as web), even if one was deleted and is recreated here as root.
install -d -o phil -g phil -m 755 "$PHIL_RUNTIME" "$PHIL_RUNTIME/logs"

# The dashboard sees only what it needs: no Claude or GitHub tokens.
env -i PATH="$PATH" HOME=/tmp LANG=C.UTF-8 RAILWAY_ENVIRONMENT="${RAILWAY_ENVIRONMENT:-}" \
  PORT="${PORT:-8080}" PHIL_HOME="$PHIL_HOME" PHIL_RUNTIME="$PHIL_RUNTIME" \
  DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-}" MTM_REFRESH_S="${MTM_REFRESH_S:-180}" \
  PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  setpriv --reuid=web --regid=web --clear-groups \
  python3 /app/dashboard/server.py &
DASH=$!

HOME=/home/phil USER=phil LOGNAME=phil \
  setpriv --reuid=phil --regid=phil --init-groups \
  python3 /app/deploy/supervisor.py &
SUP=$!

trap 'kill -TERM "$SUP" "$DASH" 2>/dev/null || true' TERM INT
set +e
wait -n "$SUP" "$DASH"
code=$?
kill -TERM "$SUP" "$DASH" 2>/dev/null
wait
exit "$code"

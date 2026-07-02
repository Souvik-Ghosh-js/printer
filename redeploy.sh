#!/usr/bin/env bash
# ============================================================
#  Redeploy the printer app (called by CI/CD on every push to main).
#  Pulls latest code, installs deps, restarts the service.
#  Does NOT touch the DB/schema. Safe to run repeatedly.
#
#  Manual use:  bash /home/ubuntu/printer/redeploy.sh
# ============================================================
set -euo pipefail

APP_DIR="/home/ubuntu/printer"
BRANCH="main"
SERVICE="printer-app.service"

echo "==> Pulling latest code..."
git -C "$APP_DIR" fetch origin "$BRANCH"
git -C "$APP_DIR" reset --hard "origin/$BRANCH"

echo "==> Installing dependencies..."
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Restarting service..."
sudo systemctl restart "$SERVICE"

echo "==> Status:"
sudo systemctl is-active "$SERVICE"
echo "✅ Redeploy complete."

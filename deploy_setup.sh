#!/usr/bin/env bash
# ============================================================
#  Build & deploy the printer app on a Lightsail Ubuntu instance
#  that already runs MySQL and one other app.
#
#  USAGE (run on the instance, over SSH):
#     curl -fsSL https://raw.githubusercontent.com/Souvik-Ghosh-js/printer/main/deploy_setup.sh -o deploy_setup.sh
#     bash deploy_setup.sh
#
#  ...or just clone the repo and run it from inside:
#     git clone https://github.com/Souvik-Ghosh-js/printer.git
#     bash printer/deploy_setup.sh
#
#  It is idempotent: safe to re-run after a `git pull` to redeploy.
# ============================================================
set -euo pipefail

# ---------------- CONFIG (edit if needed) ----------------
REPO_URL="https://github.com/Souvik-Ghosh-js/printer.git"
BRANCH="main"
APP_DIR="/home/ubuntu/printer"                # where the code is cloned on the instance
UPLOAD_DIR="/var/lib/printer/uploads"         # PDF storage on the SSD
PORT="5001"                                   # second app -> different port from your existing app

# DB password — MUST match DB_PASSWORD hardcoded in db.py
DB_NAME="printer"
DB_USER="printer_app"
DB_PASSWORD="CHANGE_ME_strong_db_password"
# ---------------------------------------------------------

echo "==> [1/7] System packages..."
sudo apt-get update -y
sudo apt-get install -y git python3-venv python3-pip poppler-utils libgl1 libglib2.0-0

echo "==> [2/7] Clone or update the repo..."
if [ -d "$APP_DIR/.git" ]; then
  sudo git -C "$APP_DIR" fetch origin "$BRANCH"
  sudo git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  sudo git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
sudo chown -R "$USER":"$USER" "$APP_DIR"

echo "==> [3/7] Storage directory on the SSD..."
sudo mkdir -p "$UPLOAD_DIR"
sudo chown -R "$USER":"$USER" "$UPLOAD_DIR"

echo "==> [4/7] Python venv + dependencies..."
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> [5/7] Create database, table, and app user..."
sudo mysql < "$APP_DIR/schema.sql"
sudo mysql <<SQL
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "==> [6/7] systemd service (gunicorn)..."
sudo tee /etc/systemd/system/printer-app.service >/dev/null <<UNIT
[Unit]
Description=Printer Flask app
After=network.target mysql.service

[Service]
User=${USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/gunicorn --workers 3 --timeout 120 --bind 127.0.0.1:${PORT} app:app
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable printer-app.service
sudo systemctl restart printer-app.service

echo "==> [7/7] Status:"
sudo systemctl status printer-app.service --no-pager || true

cat <<NOTE

============================================================
 ✅ App is built and running on 127.0.0.1:${PORT}

 Keys are hardcoded in the code (as requested). The ONLY value
 you must keep in sync is the DB password:
   - DB_PASSWORD in THIS script  ==  DB_PASSWORD in db.py

 MANUAL STEPS LEFT (one time):
 1. nginx: add a server block for print.mohiniprintshop.org proxying
    to http://127.0.0.1:${PORT}, then run certbot for HTTPS.
    Your existing app keeps its own block/port — they coexist.
 2. Set FILE_TOKEN in db.py to a random token (then redeploy).
 3. Cashfree dashboard: webhook URL ->
    https://print.mohiniprintshop.org/payment-callback
 4. Shop PC worker.py: needs MySQL reachable. Either open port 3306
    in Lightsail firewall to the shop's IP (+ a host-scoped DB user),
    or have the worker talk to the app over HTTP instead.

 To redeploy after pushing new code to GitHub:
     bash deploy_setup.sh        # pulls latest + restarts
============================================================
NOTE

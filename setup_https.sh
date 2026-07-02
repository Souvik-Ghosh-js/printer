#!/usr/bin/env bash
# ============================================================
#  Steps 4-6: nginx site + HTTPS cert + lock app to loopback
#  for print.mohiniprintshop.org
#
#  PREREQS (do these FIRST):
#    - DNS A record: print.mohiniprintshop.org -> this instance IP
#      verify:  dig +short print.mohiniprintshop.org
#    - Lightsail firewall: ports 80 and 443 open
#    - nginx + certbot installed (Step 3):
#        sudo apt-get install -y nginx certbot python3-certbot-nginx
#
#  RUN:  bash setup_https.sh
# ============================================================
set -euo pipefail

DOMAIN="print.mohiniprintshop.org"
PORT="5001"
SERVICE="printer-app.service"
CERTBOT_EMAIL="souvikghoshk@gmail.com"   # used for cert expiry notices

echo "==> [4] nginx site for $DOMAIN -> 127.0.0.1:$PORT"
sudo tee /etc/nginx/sites-available/printer >/dev/null <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    client_max_body_size 50M;   # allow larger PDF uploads

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/printer /etc/nginx/sites-enabled/printer
sudo nginx -t
sudo systemctl reload nginx

echo "==> [5] HTTPS certificate via certbot (with HTTP->HTTPS redirect)"
sudo certbot --nginx -d "$DOMAIN" \
  --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect

echo "==> [6] Lock the app back to loopback (nginx now fronts it)"
sudo sed -i "s|0.0.0.0:${PORT}|127.0.0.1:${PORT}|" /etc/systemd/system/${SERVICE}
sudo systemctl daemon-reload
sudo systemctl restart ${SERVICE}

echo "==> Verify"
sleep_ok=$(curl -sI "https://${DOMAIN}" | head -n1 || true)
echo "    https://${DOMAIN} -> ${sleep_ok:-no response}"

cat <<NOTE

============================================================
 ✅ Done. App should be live at: https://${DOMAIN}

 REMAINING (manual, not done by this script):
   - Remove the temporary port-${PORT} rule from the Lightsail
     firewall (only 80 + 443 are needed now).
   - Cashfree webhook -> https://${DOMAIN}/payment-callback
   - Make sure FILE_TOKEN in db.py is a real random value, then
     redeploy: bash /opt/printer/deploy_setup.sh
============================================================
NOTE

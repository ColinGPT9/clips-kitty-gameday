#!/usr/bin/env bash
# Set up Clips Kitty Game Day on a fresh Ubuntu 24.04 server, such as AWS Lightsail.
#
#   sudo bash scripts/setup-lightsail.sh <domain> [max_upload_gb]
#
# <domain> is the dashboard's address. With a static IP of 203.0.113.7, use
# 203-0-113-7.sslip.io: it needs no DNS setup, and Caddy gets an HTTPS
# certificate for it. [max_upload_gb] is the biggest game you can upload
# (default 4). Keep it well under the server's free disk. To update later,
# git pull and run this script again.
#
# It makes two passwords once, in /etc/gameday.env, and never prints them: the
# owner's, and the judge login's (user judge, for the event's judges: it can
# upload and look, but not change settings or post). Running it again keeps both.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run it with sudo: sudo bash scripts/setup-lightsail.sh <domain>" >&2
  exit 1
fi
DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "Usage: sudo bash scripts/setup-lightsail.sh <domain>   (for example 203-0-113-7.sslip.io)" >&2
  exit 1
fi

MAX_GB="${2:-4}"
case "$MAX_GB" in
  ''|*[!0-9]*) echo "max_upload_gb must be a whole number of gigabytes, like 4 or 20" >&2; exit 1 ;;
esac
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-root}"
DATA_DIR=/var/lib/gameday
WATCH_DIR=/srv/gameday-watch
ENV_FILE=/etc/gameday.env

echo "== Installing ffmpeg, Python and Caddy"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends python3 python3-venv ffmpeg fonts-dejavu-core caddy curl ca-certificates

echo "== Installing the app"
sudo -H -u "$RUN_USER" python3 -m venv "$REPO/.venv"
sudo -H -u "$RUN_USER" "$REPO/.venv/bin/pip" install -q --upgrade pip
sudo -H -u "$RUN_USER" "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
install -d -o "$RUN_USER" -m 700 "$DATA_DIR"
install -d -o "$RUN_USER" -m 755 "$WATCH_DIR"

# The owner password is made once and kept, so running this again doesn't change it.
if [ ! -f "$ENV_FILE" ]; then
  (umask 077 && printf 'GAMEDAY_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')" > "$ENV_FILE")
fi
# So is the judge login's (user judge, docs/API.md). An env file made before the judge
# login existed gets one too. Neither password is ever printed.
if ! grep -q '^GAMEDAY_JUDGE_PASSWORD=' "$ENV_FILE"; then
  if [ -n "$(tail -c 1 "$ENV_FILE")" ]; then echo >> "$ENV_FILE"; fi
  (umask 077 && printf 'GAMEDAY_JUDGE_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')" >> "$ENV_FILE")
fi
chmod 600 "$ENV_FILE"

cat > /etc/systemd/system/gameday.service <<EOF
[Unit]
Description=Clips Kitty Game Day
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$REPO
EnvironmentFile=$ENV_FILE
Environment=DATA_DIR=$DATA_DIR
Environment=WATCH_DIR=$WATCH_DIR
Environment=HOST=127.0.0.1
Environment=PORT=8080
Environment=MAX_UPLOAD_MB=$((MAX_GB * 1024))
ExecStart=$REPO/.venv/bin/python -m gameday
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# Caddy is the only thing facing the internet: HTTPS on 443, and big uploads.
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	request_body {
		max_size ${MAX_GB}GB
	}
	header Strict-Transport-Security "max-age=31536000"
	reverse_proxy 127.0.0.1:8080
}
EOF

systemctl daemon-reload
systemctl enable -q gameday caddy
systemctl restart gameday
systemctl restart caddy

echo "== Waiting for the app"
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
  echo "The app didn't start. Its last log lines:" >&2
  journalctl -u gameday -n 40 --no-pager >&2
  exit 1
fi

echo "== Waiting for HTTPS on $DOMAIN"
https_ok=""
for _ in $(seq 1 45); do
  if curl -fsS "https://$DOMAIN/health" >/dev/null 2>&1; then https_ok=1; break; fi
  sleep 2
done

echo
echo "Clips Kitty Game Day is running."
echo "  Dashboard:  https://$DOMAIN/   (user: owner)"
echo "  Password:   sudo grep GAMEDAY_PASSWORD $ENV_FILE"
echo "  Judges:     user judge, which can upload and look but not change settings or post"
echo "              its password: sudo grep GAMEDAY_JUDGE_PASSWORD $ENV_FILE"
echo "  Games:      upload them on the dashboard, or copy them into $WATCH_DIR"
echo "  Logs:       journalctl -u gameday -f"
echo "  Update:     git pull, then run this script again"
if [ -z "$https_ok" ]; then
  echo
  echo "HTTPS isn't answering yet. Check that the server's firewall allows ports 80 and 443,"
  echo "and that $DOMAIN points at this server. Caddy's log: journalctl -u caddy -n 40"
fi

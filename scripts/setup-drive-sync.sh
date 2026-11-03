#!/usr/bin/env bash
# Copy new games from a Google Drive folder into the server's watch folder,
# every minute, so a game dropped into Drive gets clipped on its own.
#
#   sudo bash scripts/setup-drive-sync.sh "<folder in your Google Drive>"
#
# Run scripts/setup-lightsail.sh first. Before this one, on a computer with a
# browser, run
#   rclone authorize "drive" "eyJzY29wZSI6ImRyaXZlLnJlYWRvbmx5In0="
# sign in to Google, and copy the token it prints. This script asks for it.
# The token is read-only: the server can download from your Drive, never
# change or delete anything in it. Files are only copied, never removed.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run it with sudo: sudo bash scripts/setup-drive-sync.sh \"Game Day\"" >&2
  exit 1
fi
FOLDER="${1:-Game Day}"
RUN_USER="${SUDO_USER:-root}"
REMOTE="${GAMEDAY_DRIVE_REMOTE:-gdrive}"
STAGING=/srv/gameday-drive
WATCH_DIR=/srv/gameday-watch

echo "== Installing rclone"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q --no-install-recommends rclone

as_user() { sudo -H -u "$RUN_USER" "$@"; }

if ! as_user rclone listremotes | grep -qx "$REMOTE:"; then
  echo
  echo "Paste the token that 'rclone authorize' printed (it starts with {\"access_token\")."
  echo "Nothing shows while you paste. Then press Enter."
  read -rs TOKEN
  echo
  if [ -z "$TOKEN" ]; then
    echo "No token given." >&2
    exit 1
  fi
  as_user rclone config create "$REMOTE" drive scope=drive.readonly token="$TOKEN" --non-interactive >/dev/null
  unset TOKEN
fi

echo "== Checking the Drive folder \"$FOLDER\""
if ! as_user rclone lsf "$REMOTE:$FOLDER" --max-depth 1 >/dev/null; then
  echo "Can't read \"$FOLDER\" in your Drive. Check the folder's exact name, then run this again." >&2
  exit 1
fi

install -d -o "$RUN_USER" -m 755 "$STAGING" "$WATCH_DIR"

# Download into a staging folder first, then hard-link each finished file into
# the watch folder, so the app never sees a half-downloaded game.
cat > /usr/local/bin/gameday-drive-sync <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
rclone copy "$GAMEDAY_DRIVE_REMOTE:$GAMEDAY_DRIVE_FOLDER" /srv/gameday-drive --max-depth 1 \
  --include "*.{mp4,MP4,mov,MOV,m4v,M4V,mkv,MKV,webm,WEBM,ts,TS,mts,MTS,m2ts,M2TS}" \
  --transfers 2 --log-level NOTICE
for f in /srv/gameday-drive/*; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  [ -e "/srv/gameday-watch/$name" ] || ln "$f" "/srv/gameday-watch/$name"
done
EOF
chmod 755 /usr/local/bin/gameday-drive-sync

cat > /etc/systemd/system/gameday-drive.service <<EOF
[Unit]
Description=Copy new games from Google Drive into the Game Day watch folder
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Environment="GAMEDAY_DRIVE_REMOTE=$REMOTE"
Environment="GAMEDAY_DRIVE_FOLDER=$FOLDER"
ExecStart=/usr/local/bin/gameday-drive-sync
EOF

cat > /etc/systemd/system/gameday-drive.timer <<EOF
[Unit]
Description=Check Google Drive for new games every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable -q --now gameday-drive.timer
systemctl start gameday-drive.service

echo
echo "Google Drive sync is on."
echo "  Drive folder:  $FOLDER (checked every minute, read-only)"
echo "  New games are copied into $WATCH_DIR, and the app clips them."
echo "  Only put games you want clipped in that folder: each video in it becomes a job."
echo "  Log:  journalctl -u gameday-drive -n 20"
echo "  Stop: sudo systemctl disable --now gameday-drive.timer"

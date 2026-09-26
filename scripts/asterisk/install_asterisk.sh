#!/usr/bin/env bash
# Install and configure Asterisk inside WSL (Ubuntu) for the Mouth Care Solutions receptionist.
# Run as root:  wsl -d Ubuntu -u root -- bash /mnt/e/.../scripts/asterisk/install_asterisk.sh /mnt/e/.../telephony/asterisk/generated
# Idempotent: re-running updates the configuration and restarts Asterisk.
set -euo pipefail

GENERATED="${1:?usage: install_asterisk.sh <generated-config-dir>}"
RECORDINGS="${MCS_RECORDING_DIR:-/var/spool/asterisk/mouthcare-recordings}"

if [ "$(id -u)" -ne 0 ]; then echo "must run as root" >&2; exit 1; fi
for f in asterisk.conf modules.conf manager.conf http.conf ari.conf extensions.conf pjsip.conf; do
  [ -f "$GENERATED/$f" ] || { echo "missing $GENERATED/$f (run scripts/asterisk/render_config.py)" >&2; exit 1; }
done

if ! command -v asterisk >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends asterisk asterisk-core-sounds-en
fi

# Back up the packaged configuration once, then install ours (secrets readable by asterisk only).
if [ ! -d /etc/asterisk.dist ]; then cp -a /etc/asterisk /etc/asterisk.dist; fi
for f in "$GENERATED"/*.conf; do
  install -o asterisk -g asterisk -m 0640 "$f" "/etc/asterisk/$(basename "$f")"
done

# Caller turns are recorded here and read (then deleted) by the Windows-side AGI server
# through \\wsl.localhost; the directory is shared, the files are short-lived.
install -d -o asterisk -g asterisk -m 1777 "$RECORDINGS"

# systemd is the default in current WSL images; fall back to the init script otherwise.
if [ -d /run/systemd/system ]; then
  systemctl enable asterisk >/dev/null 2>&1 || true
  systemctl restart asterisk
else
  service asterisk restart
fi

for i in $(seq 1 20); do
  if asterisk -rx "core show version" >/dev/null 2>&1; then break; fi
  sleep 1
done
asterisk -rx "core show version"
asterisk -rx "manager show settings" | grep -Ei "enabled|bind|port" || true
asterisk -rx "dialplan show mouthcare-receptionist" | head -n 12
asterisk -rx "module show like format_wav"
asterisk -rx "pjsip show registrations" || true
echo "ASTERISK_INSTALL_OK"

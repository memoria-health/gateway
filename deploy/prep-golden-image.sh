#!/usr/bin/env bash
# prep-golden-image.sh — turn THIS box into a fresh golden TEMPLATE, ready to
# power off and Clonezilla-capture. After capture, every clone of the image gets
# a unique identity on first boot (via the firstboot service installed here) and
# raises the Wi-Fi onboarding hotspot (because no saved Wi-Fi is left).
#
#   ┌─ IMAGE-A (a plain clone of a live box) → boots as a TWIN. Not a template.
#   └─ IMAGE-B (this box after prep, captured) → boots blank, self-onboards.
#
# RUN AS ROOT, AT THE CONSOLE (not over Wi-Fi SSH — this DELETES the Wi-Fi it's
# connected on), on the WORKBENCH box only — NEVER the live OptiPlex, since it
# wipes identity + Wi-Fi:
#     sudo bash deploy/prep-golden-image.sh
#
# Then: POWER OFF (do NOT reboot — a reboot runs firstboot and un-arms the
# template), boot the Clonezilla USB, and `savedisk`.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash deploy/prep-golden-image.sh"; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/memoria-gateway
say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }

say "Stopping services"
systemctl stop memoria-gateway memoria-setup memoria-tunnel memoria-wifi-portal 2>/dev/null || true

say "Installing the first-boot uniqueness service (absent on hand-built boxes)"
install -m 0755 "$SCRIPT_DIR/memoria-firstboot.sh" /usr/local/sbin/memoria-firstboot.sh
cp "$SCRIPT_DIR/memoria-firstboot.service" /etc/systemd/system/memoria-firstboot.service
systemctl daemon-reload
systemctl enable memoria-firstboot.service

say "Seeding ops SSH keys (fleet access without a shared password)"
# Without this, a box cut from this image accepts only the account
# PASSWORD — which is why the password has been load-bearing for fleet
# access, and why it could not simply be retired. The reverse tunnel
# gets you TO port 22 on any box; this is what gets you THROUGH it.
#
# Deliberately additive and idempotent: an operator who has already
# added a key by hand keeps it, and re-running prep never duplicates a
# line. Passwords stay enabled here on purpose — turning them off is a
# SEPARATE step, taken only after key access is verified on a real
# clone, because getting it wrong bricks remote access to the fleet.
seed_ops_keys() {
  local user="${BOX_USER:-potaton00b}"
  local src="$SCRIPT_DIR/ops_authorized_keys"
  local home ak
  home="$(getent passwd "$user" | cut -d: -f6)"
  if [ -z "$home" ] || [ ! -d "$home" ]; then
    echo "   ! no home for user '$user' — SKIPPED (set BOX_USER=)"; return
  fi
  if [ ! -f "$src" ]; then
    echo "   ! $src missing — SKIPPED"; return
  fi
  ak="$home/.ssh/authorized_keys"
  install -d -m 0700 -o "$user" -g "$user" "$home/.ssh"
  touch "$ak"
  local added=0
  while IFS= read -r key; do
    case "$key" in ''|'#'*) continue;; esac
    if ! grep -qxF "$key" "$ak"; then
      printf '%s\n' "$key" >> "$ak"
      added=$((added + 1))
    fi
  done < "$src"
  chown "$user":"$user" "$ak"
  chmod 0600 "$ak"
  echo "   $added key(s) added, $(grep -cvE '^\s*(#|$)' "$ak") now authorized for $user"
}
seed_ops_keys

say "Installing the dynamic reverse-tunnel unit (port assigned by the backend)"
# Replaces any hardcoded-port tunnel unit: the port now comes from .tunnel-port
# (publisher.py writes it from the roster), so every box gets a UNIQUE off-LAN
# port with no collisions. tunnel.sh ships in the repo at deploy/tunnel.sh.
chmod +x "$INSTALL_DIR/deploy/tunnel.sh" 2>/dev/null || true
cp "$SCRIPT_DIR/memoria-tunnel.service" /etc/systemd/system/memoria-tunnel.service
systemctl daemon-reload
systemctl enable memoria-tunnel.service

say "Deleting saved Wi-Fi so a clone raises the onboarding hotspot"
# Every wireless NM connection (e.g. 'Suburban Living', 'jesstan') — gone, so a
# clone boots offline and memoria-wifi-portal broadcasts Memoria-Setup-XXXX.
while IFS=: read -r name typ; do
  case "$typ" in *wireless*) echo "   deleting Wi-Fi: $name"; nmcli con delete "$name" || true;; esac
done < <(nmcli -t -f NAME,TYPE con show)
rm -f /etc/netplan/90-NM-*.yaml   # the netplan exports of those NM connections
# Keep 01-network-manager-all.yaml (renderer=NetworkManager) — wifi-connect needs it.

say "Arming firstboot: clearing the run-once flag + baked identity"
rm -f "$INSTALL_DIR/.provisioned" "$INSTALL_DIR/.tunnel-port" "$INSTALL_DIR/portal-cert.pem" "$INSTALL_DIR/portal-key.pem"
# machine-id + SSH host keys: blank now (systemd/ssh also regenerate on boot;
# firstboot regenerates too — belt and suspenders so the image carries neither).
truncate -s 0 /etc/machine-id || true
rm -f /var/lib/dbus/machine-id /etc/ssh/ssh_host_*
# cameras.json is LEFT as the config template (right backend/livekit); firstboot
# blanks its cameras + mints a unique token per box.

say "Clearing logs + shell history"
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
rm -f /root/.bash_history /home/*/.bash_history 2>/dev/null || true
find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true

cat <<'DONE'

✅ TEMPLATE READY.
   1. POWER OFF now:  sudo poweroff        (do NOT reboot — that runs firstboot)
   2. Boot the Clonezilla USB and `savedisk` -> that image is your fleet template.

   Each box flashed from it will, on FIRST boot only:
     • raise the Memoria-Setup-XXXX Wi-Fi hotspot (enter the site Wi-Fi there),
     • mint a unique gateway_token + machine-id + SSH keys + tunnel port,
     • then disable firstboot so every later boot goes straight to running.
DONE

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

say "Deleting saved Wi-Fi so a clone raises the onboarding hotspot"
# Every wireless NM connection (e.g. 'Suburban Living', 'jesstan') — gone, so a
# clone boots offline and memoria-wifi-portal broadcasts Memoria-Setup-XXXX.
while IFS=: read -r name typ; do
  case "$typ" in *wireless*) echo "   deleting Wi-Fi: $name"; nmcli con delete "$name" || true;; esac
done < <(nmcli -t -f NAME,TYPE con show)
rm -f /etc/netplan/90-NM-*.yaml   # the netplan exports of those NM connections
# Keep 01-network-manager-all.yaml (renderer=NetworkManager) — wifi-connect needs it.

say "Arming firstboot: clearing the run-once flag + baked identity"
rm -f "$INSTALL_DIR/.provisioned" "$INSTALL_DIR/.tunnel-port"
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

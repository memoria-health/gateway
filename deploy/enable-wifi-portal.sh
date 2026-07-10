#!/usr/bin/env bash
# THE FLIP — hand Wi-Fi to NetworkManager and raise the onboarding portal.
#
# This DROPS the box's current Wi-Fi (and any SSH over it) ON PURPOSE: with no
# saved NetworkManager connection, the box goes offline and wifi-connect
# broadcasts its captive-portal hotspot (Memoria-Setup-XXXX).
#
# SAFETY: a timer auto-reverts to networkd + the old Wi-Fi in 15 min if the
# portal is never completed — so a headless box always comes back on its own.
# After a successful test, cancel it:  systemctl stop memoria-autorevert.timer
#
# Run detached so it survives SSH dropping:
#   sudo systemd-run --unit=memoria-flip bash /opt/memoria-gateway/deploy/enable-wifi-portal.sh
set -e
# NOTE: arm the safety auto-revert EXTERNALLY before running this (a systemd-run
# timer nested inside this script's own transient unit gets cleaned up when it
# exits — that's why the first attempt had no timer). Arm it over SSH first:
#   sudo systemd-run --on-active=600 --unit=memoria-safety \
#     /bin/bash /opt/memoria-gateway/deploy/revert-wifi.sh
#   systemctl list-timers memoria-safety   # verify it's listed BEFORE flipping
# hand Wi-Fi to NetworkManager
cat > /etc/netplan/01-network-manager-all.yaml <<'NP'
network:
  version: 2
  renderer: NetworkManager
NP
chmod 600 /etc/netplan/01-network-manager-all.yaml
rm -f /etc/netplan/90-wifi.yaml           # drop the networkd Wi-Fi -> forces portal
systemctl enable --now NetworkManager >/dev/null 2>&1 || true
systemctl enable --now memoria-wifi-portal.service >/dev/null 2>&1 || true
# 3. apply — the moment Wi-Fi/SSH drops and the portal comes up
netplan apply

#!/usr/bin/env bash
# CONSOLE RECOVERY — run this AT THE MONITOR/KEYBOARD if the Wi-Fi portal test
# leaves the box offline (i.e. NetworkManager took over and didn't reconnect).
#
# It undoes the NetworkManager switch and puts the box back on plain
# systemd-networkd joined to the known Wi-Fi, exactly like before the test.
#
#   sudo bash /opt/memoria-gateway/deploy/revert-wifi.sh                 # jesstan
#   sudo bash /opt/memoria-gateway/deploy/revert-wifi.sh MySSID MyPass   # other net
set -e
SSID="${1:-jesstan}"
PASS="${2:-wolfe3891}"
IFACE="${3:-$(ls /sys/class/net | grep -E '^wl' | head -1)}"
[ -n "$IFACE" ] || { echo "no wireless interface found"; exit 1; }
echo "reverting $IFACE -> networkd + SSID '$SSID'"

# 1. remove the NetworkManager renderer netplan the portal installed
rm -f /etc/netplan/01-network-manager-all.yaml

# 2. (re)write the known-good networkd Wi-Fi config
cat > /etc/netplan/90-wifi.yaml <<NP
network:
  version: 2
  renderer: networkd
  wifis:
    $IFACE:
      dhcp4: true
      access-points:
        "$SSID":
          password: "$PASS"
NP
chmod 600 /etc/netplan/90-wifi.yaml

# 3. hand control back to networkd + wpa_supplicant
systemctl disable --now NetworkManager 2>/dev/null || true
systemctl unmask systemd-networkd wpa_supplicant 2>/dev/null || true
systemctl enable --now systemd-networkd wpa_supplicant 2>/dev/null || true
netplan apply

echo "applied. waiting for DHCP…"
for i in $(seq 1 15); do
  ip -4 addr show "$IFACE" | grep -q inet && { echo "ONLINE:"; ip -4 addr show "$IFACE" | grep inet; exit 0; }
  sleep 2
done
echo "no IP yet — give it a few more seconds, then: ip a show $IFACE"

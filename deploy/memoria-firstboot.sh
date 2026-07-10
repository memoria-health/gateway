#!/usr/bin/env bash
# Memoria first-boot uniqueness — runs ONCE on a freshly-flashed clone, then
# disables itself. It gives each box its own identity so clones of the golden
# image aren't twins: new machine-id, new SSH host keys, a fresh gateway_token,
# an empty camera map, and a unique reverse-SSH tunnel port.
#
# It does NOT touch Wi-Fi or account data — the site Wi-Fi is entered at the
# captive-portal hotspot (memoria-wifi-portal), and the token is tied to a
# caregiver later, in the field, by email via the :8088 setup portal. Nothing
# account-specific is baked into the image.
#
# Armed by prep-golden-image.sh (which removes the .provisioned flag + enables
# this unit). Guarded TWO ways so it can never run on an already-set-up box:
#   1. the unit's ConditionPathExists=!/opt/memoria-gateway/.provisioned
#   2. this script writes that flag + `systemctl disable`s itself at the end
set -e
LOG=/var/log/memoria-firstboot.log
exec >>"$LOG" 2>&1
echo "=== memoria-firstboot $(date -u 2>/dev/null || echo) ==="
INSTALL_DIR=/opt/memoria-gateway

# 1. unique host identity — so two clones don't share a machine-id / SSH key
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A
rm -f /etc/machine-id /var/lib/dbus/machine-id
systemd-machine-id-setup
[ -e /var/lib/dbus/machine-id ] || ln -sf /etc/machine-id /var/lib/dbus/machine-id

# 2. fresh gateway_token + empty camera map. Rewriting the existing file in
#    place preserves its owner. The token becomes valid in the backend when the
#    installer provisions the first camera via the :8088 portal (which creates
#    the gateways row for this token).
CJ="$INSTALL_DIR/cameras.json"
if [ -f "$CJ" ]; then
  python3 - "$CJ" <<'PY'
import json, secrets, sys
p = sys.argv[1]
d = json.load(open(p))
d["cameras"] = []
d["gateway_token"] = "gw-" + secrets.token_urlsafe(24)
json.dump(d, open(p, "w"), indent=2)
PY
fi

# 3. unique reverse-SSH tunnel port so boxes don't collide on the relay VPS
#    (the baked unit hardcodes 22001). Derived from the NEW machine-id, range
#    22001-22999, recorded to .tunnel-port so the relay operator can map it.
TUNIT=/etc/systemd/system/memoria-tunnel.service
if [ -f "$TUNIT" ]; then
  PORT=$(( 22001 + ( 0x$(cut -c1-4 /etc/machine-id) % 999 ) ))
  sed -i -E "s/-R [0-9]+:localhost:22/-R ${PORT}:localhost:22/" "$TUNIT"
  echo "$PORT" > "$INSTALL_DIR/.tunnel-port"
  echo "assigned reverse-tunnel port $PORT"
fi

# 4. drop the template's accumulated logs
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true

# 5. run-once: mark done + disable so this never fires again
touch "$INSTALL_DIR/.provisioned"
systemctl daemon-reload
systemctl disable memoria-firstboot.service || true
echo "=== firstboot done (token minted, tunnel port set, disabled) ==="

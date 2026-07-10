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
  # 2b. keep the SETUP PORTAL's token in sync with the freshly-minted one. The
  #     :8088 portal (memoria-setup) has GATEWAY_TOKEN baked into its unit from
  #     the SOURCE image. If we don't update it, the portal provisions under the
  #     source box's token -> cameras land on the WRONG gateway, and this box's
  #     own token stays unregistered (publisher 401). Sync it to cameras.json.
  NEWTOK=$(python3 -c "import json;print(json.load(open('$CJ'))['gateway_token'])" 2>/dev/null || true)
  SU=/etc/systemd/system/memoria-setup.service
  if [ -n "$NEWTOK" ] && [ -f "$SU" ]; then
    sed -i -E "s|GATEWAY_TOKEN=[^[:space:]\"]+|GATEWAY_TOKEN=${NEWTOK}|g" "$SU"
    systemctl daemon-reload
  fi
fi

# 3. reverse-SSH tunnel port is assigned by the BACKEND (unique per box, in the
#    gateways table), delivered in the roster and written to .tunnel-port by
#    publisher.py AFTER this box is provisioned. So firstboot just clears any
#    stale port — the tunnel (deploy/tunnel.sh) idles until the backend assigns
#    one, then reconnects on the new port automatically (no hashing, no collisions).
rm -f "$INSTALL_DIR/.tunnel-port"

# 4. drop the template's accumulated logs
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true

# 5. run-once: mark done + disable so this never fires again
touch "$INSTALL_DIR/.provisioned"
systemctl daemon-reload
systemctl disable memoria-firstboot.service || true
echo "=== firstboot done (token minted, tunnel port set, disabled) ==="

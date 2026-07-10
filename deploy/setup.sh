#!/usr/bin/env bash
# Memoria Gateway — one-shot provisioner.
#
# Turns a fresh Ubuntu Server box into a working gateway appliance, codifying
# everything that was done by hand on the first unit:
#   - ffmpeg + Python venv + the LiveKit SDK
#   - the roster-driven publisher at /opt/memoria-gateway
#   - the always-on gateway systemd service (auto-start + auto-restart)
#   - the on-demand camera SETUP PORTAL (web form, no CLI)
#   - the phone WI-FI PORTAL (captive-portal onboarding, no CLI)  [optional]
#   - a FIRST-BOOT step that makes each clone unique + blank
#
# Run as root ON the box, from inside the repo:   sudo deploy/setup.sh
# Idempotent — safe to re-run to update code/deps/services.
#
# What is NOT set here (it differs per site, so it's done on-site via the two
# portals): the Wi-Fi credentials, and which cameras exist.
set -euo pipefail

# ── CONFIG — change these per environment (sandbox / dev / prod) ─────────────
INSTALL_DIR="${INSTALL_DIR:-/opt/memoria-gateway}"
# Sandbox defaults (this first unit). For prod, pass a STABLE domain, e.g.
#   BACKEND_URL=https://api.memoria.care  LIVEKIT_URL=wss://memoria.livekit.cloud
BACKEND_URL="${BACKEND_URL:-http://10.0.0.20:8001}"
LIVEKIT_URL="${LIVEKIT_URL:-ws://10.0.0.20:7880}"
LIVEKIT_KEY="${LIVEKIT_KEY:-devkey}"
LIVEKIT_SECRET="${LIVEKIT_SECRET:-secret}"
SUPABASE_URL="${SUPABASE_URL:-http://10.0.0.20:54351}"
SUPABASE_ANON="${SUPABASE_ANON:-sb_publishable_ACJWlzQHlZjBrEguHvfOxg_3BJgxAaH}"
GATEWAY_TOKEN="${GATEWAY_TOKEN:-gw-site-token-2026}"
# The unprivileged account the services run as (must exist; created if missing).
RUN_USER="${RUN_USER:-memoria}"
# Wi-Fi portal switches networking to NetworkManager. Set 0 to skip it — e.g.
# when re-running on a LIVE remote box whose Wi-Fi you don't want disturbed.
ENABLE_WIFI_PORTAL="${ENABLE_WIFI_PORTAL:-1}"
WIFI_CONNECT_VERSION="${WIFI_CONNECT_VERSION:-v4.11.84}"
PORTAL_SSID_PREFIX="${PORTAL_SSID_PREFIX:-Memoria-Setup}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo deploy/setup.sh"; exit 1; }

# ── 1. system packages ──────────────────────────────────────────────────────
say "Installing system packages (ffmpeg, python, net-tools…)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg python3-venv python3-pip git net-tools curl \
                       avahi-daemon libnss-mdns
#   net-tools    = `arp`, so discover.py can read camera MACs (IP-drift recovery)
#   avahi-daemon = advertise this box as <hostname>.local on the LAN, so it's
#                  reachable by NAME (IP-agnostic) — e.g. http://memoria-gateway.local:8088
#   libnss-mdns  = resolve OTHER *.local names (e.g. the backend's Bonjour name)

# ── 2. run user ─────────────────────────────────────────────────────────────
if ! id "$RUN_USER" >/dev/null 2>&1; then
  say "Creating service user '$RUN_USER'"
  useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER"
fi

# ── 3. deploy the gateway code ──────────────────────────────────────────────
say "Deploying gateway code -> $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.env' --exclude 'clips' --exclude 'cameras.json' \
  "$REPO_DIR"/ "$INSTALL_DIR"/
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

# ── 4. python env ───────────────────────────────────────────────────────────
say "Building Python venv + installing LiveKit SDK"
sudo -u "$RUN_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade -q pip
#   livekit (rtc) and livekit-api are SEPARATE packages — both are required.
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install -q livekit livekit-api aiohttp

# ── 5. cameras.json — config only, NO cameras (added on-site via the portal) ─
if [ ! -f "$INSTALL_DIR/cameras.json" ]; then
  say "Writing blank cameras.json (config only)"
  cat > "$INSTALL_DIR/cameras.json" <<JSON
{
  "_comment": "Config only. Cameras are added ON-SITE via the setup portal (:8088), which appends {livekit_identity,rtsp,mac} here. The backend roster decides which run.",
  "livekit_url": "$LIVEKIT_URL",
  "livekit_api_key": "$LIVEKIT_KEY",
  "livekit_api_secret": "$LIVEKIT_SECRET",
  "backend_url": "$BACKEND_URL",
  "poll_seconds": 1,
  "gateway_token": "$GATEWAY_TOKEN",
  "cameras": []
}
JSON
  chown "$RUN_USER":"$RUN_USER" "$INSTALL_DIR/cameras.json"
fi

# ── 6. the always-on gateway service ────────────────────────────────────────
say "Installing gateway service (auto-start + auto-restart)"
cat > /etc/systemd/system/memoria-gateway.service <<UNIT
[Unit]
Description=Memoria Gateway (Tapo RTSP -> LiveKit)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python publisher.py
Restart=always
RestartSec=2
RestartSteps=4
RestartMaxDelaySec=30

[Install]
WantedBy=multi-user.target
UNIT

# ── 7. the on-demand camera setup portal ────────────────────────────────────
say "Installing camera setup portal service (on-demand, :8088)"
cat > /etc/systemd/system/memoria-setup.service <<UNIT
[Unit]
Description=Memoria Gateway Setup Portal (camera provisioning)
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=SUPABASE_URL=$SUPABASE_URL
Environment=SUPABASE_ANON_KEY=$SUPABASE_ANON
Environment=BACKEND_URL=$BACKEND_URL
Environment=GATEWAY_TOKEN=$GATEWAY_TOKEN
Environment=SETUP_PORT=8088
ExecStart=$INSTALL_DIR/.venv/bin/python setup_server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

# ── 8. phone Wi-Fi onboarding portal (optional) ─────────────────────────────
# Uses balena wifi-connect: when the box has no internet, it broadcasts its own
# hotspot + a captive portal so an installer picks the site Wi-Fi from a phone.
# Requires NetworkManager to own the wifi device.
if [ "$ENABLE_WIFI_PORTAL" = "1" ]; then
  say "Installing Wi-Fi onboarding portal (NetworkManager + wifi-connect)"
  apt-get install -y -qq network-manager
  # NM auto-runs its OWN dnsmasq for the shared hotspot (on 192.168.42.1:53),
  # which clashes with wifi-connect's dnsmasq. Instead of fighting it, tell NM's
  # dnsmasq to redirect ALL DNS to the portal so the captive page auto-pops —
  # then wifi-connect's redundant dnsmasq failing is harmless. (Proven on the
  # AX210 + Ubuntu 26.04: NM's dnsmasq already serves DHCP fine.)
  mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  printf 'address=/#/192.168.42.1\n' > /etc/NetworkManager/dnsmasq-shared.d/010-captive-redirect.conf
  # Hand wifi to NetworkManager (this is the line that would drop a live SSH
  # session — hence ENABLE_WIFI_PORTAL=0 when re-running on a remote box).
  cat > /etc/netplan/01-network-manager-all.yaml <<'NP'
network:
  version: 2
  renderer: NetworkManager
NP
  chmod 600 /etc/netplan/01-network-manager-all.yaml

  WC_DIR="$INSTALL_DIR/wifi-connect"
  mkdir -p "$WC_DIR"
  # (a) the binary — the release tarball ships ONLY this (no UI).
  if [ ! -x "$WC_DIR/wifi-connect" ]; then
    curl -Ls "https://github.com/balena-os/wifi-connect/releases/download/${WIFI_CONNECT_VERSION}/wifi-connect-x86_64-unknown-linux-gnu.tar.gz" \
      | tar -xz -C "$WC_DIR"
    chmod +x "$WC_DIR/wifi-connect"
  fi
  # (b) the web UI — balena dropped it from the binary tarball, so we vendor a
  # built copy in the repo (deploy/wifi-connect-ui/) and copy it in. If it's
  # missing, fail loudly rather than ship a portal that serves a blank page.
  if [ ! -f "$WC_DIR/ui/index.html" ]; then
    if [ -f "$REPO_DIR/deploy/wifi-connect-ui/index.html" ]; then
      rm -rf "$WC_DIR/ui"; cp -r "$REPO_DIR/deploy/wifi-connect-ui" "$WC_DIR/ui"
    else
      echo "!! wifi-connect UI missing (deploy/wifi-connect-ui/). Portal will not"
      echo "!! serve a page until it's vendored. Set ENABLE_WIFI_PORTAL=0 to skip."
      exit 1
    fi
  fi

  cat > "$WC_DIR/wifi-portal.sh" <<PORTAL
#!/usr/bin/env bash
# Only raise the captive portal when the box is actually offline.
sleep 25                              # let NM try known networks first
if nm-online -t 45 -q; then exit 0; fi
SUFFIX=\$(cat /sys/class/net/wl*/address 2>/dev/null | head -1 | tr -d ':' | tail -c 5)
exec "$WC_DIR/wifi-connect" \\
  --portal-ssid "${PORTAL_SSID_PREFIX}-\${SUFFIX}" \\
  --ui-directory "$WC_DIR/ui"
PORTAL
  chmod +x "$WC_DIR/wifi-portal.sh"

  cat > /etc/systemd/system/memoria-wifi-portal.service <<UNIT
[Unit]
Description=Memoria Wi-Fi onboarding portal (captive portal when offline)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
ExecStart=$WC_DIR/wifi-portal.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
  systemctl enable NetworkManager >/dev/null 2>&1 || true
  systemctl enable memoria-wifi-portal.service >/dev/null 2>&1 || true
else
  say "Skipping Wi-Fi portal (ENABLE_WIFI_PORTAL=0)"
fi

# ── 9. first-boot: make each clone unique + blank ───────────────────────────
say "Installing first-boot uniqueness step"
cat > /usr/local/sbin/memoria-firstboot.sh <<'FB'
#!/usr/bin/env bash
set -e
# Regenerate host identity so clones aren't twins.
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A
rm -f /etc/machine-id /var/lib/dbus/machine-id
systemd-machine-id-setup
[ -e /var/lib/dbus/machine-id ] || ln -s /etc/machine-id /var/lib/dbus/machine-id
# Start every clone with an empty camera map + a UNIQUE per-box gateway_token
# (so two homes never collide on the gateways table; the token gets tied to a
# caregiver at setup, by email, in the field — nothing account-specific baked in).
CJ=/opt/memoria-gateway/cameras.json
[ -f "$CJ" ] && python3 -c "import json,secrets; d=json.load(open('$CJ')); d['cameras']=[]; d['gateway_token']='gw-'+secrets.token_urlsafe(24); json.dump(d,open('$CJ','w'),indent=2)" || true
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
systemctl disable memoria-firstboot.service
FB
chmod +x /usr/local/sbin/memoria-firstboot.sh
cat > /etc/systemd/system/memoria-firstboot.service <<'UNIT'
[Unit]
Description=Memoria first-boot uniqueness (runs once)
Before=memoria-gateway.service
ConditionPathExists=/usr/local/sbin/memoria-firstboot.sh

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/memoria-firstboot.sh

[Install]
WantedBy=multi-user.target
UNIT

# ── 10. enable everything ───────────────────────────────────────────────────
say "Enabling services"
systemctl daemon-reload
systemctl enable memoria-gateway.service >/dev/null
systemctl enable memoria-firstboot.service >/dev/null
systemctl enable memoria-setup.service >/dev/null   # always-on camera portal: http://<host>.local:8088
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true   # advertise <hostname>.local (mDNS)

say "Done. Gateway will start on next boot (or: systemctl start memoria-gateway)."
echo "   backend : $BACKEND_URL"
echo "   livekit : $LIVEKIT_URL"
echo "   portal  : sudo systemctl start memoria-setup   (http://<box>:8088)"

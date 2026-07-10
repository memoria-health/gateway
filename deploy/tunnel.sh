#!/usr/bin/env bash
# Reverse-SSH tunnel wrapper. The reverse port is NOT hardcoded — it's read from
# /opt/memoria-gateway/.tunnel-port, which publisher.py writes from the backend
# roster (the DB assigns each box a UNIQUE port). We watch that file and reconnect
# autossh when it changes, so a freshly-provisioned box gets its off-LAN port with
# no root and no restart. Before provisioning there's no port yet → we idle (the
# box is set up on-site, so remote access isn't needed until it has an identity).
set -u
DIR=/opt/memoria-gateway
PF="$DIR/.tunnel-port"
KEY=/home/potaton00b/.ssh/memoria_tunnel_key
RELAY=tunnel@144.126.211.110
export AUTOSSH_GATETIME=0
child=0
start() {
  autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
    -i "$KEY" -R "${1}:localhost:22" "$RELAY" &
  child=$!
}
cur=""
while true; do
  port=""
  [ -f "$PF" ] && port="$(tr -dc 0-9 < "$PF")"
  if [ -n "$port" ] && [ "$port" != "$cur" ]; then
    [ "$child" -ne 0 ] && kill "$child" 2>/dev/null || true
    start "$port"; cur="$port"
    echo "tunnel: (re)connected on reverse port $port"
  elif [ "$child" -ne 0 ] && ! kill -0 "$child" 2>/dev/null; then
    exit 1   # autossh died unexpectedly → let systemd (Restart=always) respawn us
  fi
  sleep 10
done

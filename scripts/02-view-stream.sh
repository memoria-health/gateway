#!/usr/bin/env bash
# M1 — eyeball the live stream in a window to confirm it actually plays.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${CAM_IP:?Set CAM_IP in .env first}"

URL="rtsp://${CAM_USER}:${CAM_PASS}@${CAM_IP}:554/${CAM_PATH}"
echo "Opening live view (close the window or Ctrl-C to stop)..."
ffplay -hide_banner -rtsp_transport tcp -fflags nobuffer "$URL"

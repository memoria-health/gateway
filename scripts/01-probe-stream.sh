#!/usr/bin/env bash
# M1 — can we even access the Tapo RTSP stream, and is it gateway-friendly?
# Prints codec, resolution, fps, and whether there's an audio track.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${CAM_IP:?Set CAM_IP in .env (the camera's LAN IP) first}"

URL="rtsp://${CAM_USER}:${CAM_PASS}@${CAM_IP}:554/${CAM_PATH}"
echo "Probing: rtsp://${CAM_USER}:***@${CAM_IP}:554/${CAM_PATH}"
echo "---------------------------------------------"
# -rtsp_transport tcp is more reliable than UDP for this.
ffprobe -hide_banner -rtsp_transport tcp \
  -show_entries stream=codec_name,width,height,avg_frame_rate,codec_type \
  -of default=noprint_wrappers=1 "$URL"
echo "---------------------------------------------"
echo "Want to see: codec_name=h264, a sane width/height/fps, and (ideally) an audio stream."

#!/usr/bin/env bash
# M2 — pull the Tapo RTSP stream and publish it into a LiveKit room as a
# participant, proving "Tapo H.264 RTSP -> LiveKit" end to end.
#
# This is a TEMPLATE — the exact incantation depends on your ffmpeg + LiveKit
# versions. Read the README "M2" section before running. It points at a LOCAL
# `livekit-server --dev`, never the production project.
#
# Prereqs:
#   - ffmpeg >= 7.1 (has the WHIP muxer):  ffmpeg -muxers | grep whip
#   - a local LiveKit dev server running:  livekit-server --dev
#   - a publish token (generate with the `lk` CLI — see README)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${CAM_IP:?Set CAM_IP in .env first}"
: "${LIVEKIT_TOKEN:?Generate a publish token and export LIVEKIT_TOKEN (see README M2)}"

URL="rtsp://${CAM_USER}:${CAM_PASS}@${CAM_IP}:554/${CAM_PATH}"
WHIP_URL="http://localhost:7880/whip"   # local dev server WHIP endpoint

echo "Publishing ${CAM_PATH} -> LiveKit room '${LIVEKIT_ROOM}' as '${LIVEKIT_IDENTITY}'"
# Try H.264 passthrough first (-c:v copy). If LiveKit rejects the profile,
# drop the next line and let it re-encode to a WebRTC-friendly baseline.
ffmpeg -hide_banner -rtsp_transport tcp -i "$URL" \
  -c:v copy \
  -f whip -authorization "$LIVEKIT_TOKEN" \
  "${WHIP_URL}?room=${LIVEKIT_ROOM}&identity=${LIVEKIT_IDENTITY}"

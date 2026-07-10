#!/usr/bin/env bash
# Process-level supervisor: keep the gateway alive across crashes.
#
# This is the lowest-tech layer of resilience. The publisher already supervises
# each CAMERA in-process (RTSP/LiveKit reconnect); THIS loop covers the whole
# PROCESS dying (unhandled crash, OOM, etc.). On the real mini-PC this is
# replaced by a systemd unit (Restart=always) or, on the Mac sim, a launchd
# job (KeepAlive=true) so it also starts on boot.
set -u
cd "$(dirname "$0")"
PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python3"
# Exponential backoff, capped at 30s, so a persistent crash doesn't hammer.
# Resets to 2s once the gateway has run healthily for a while — a fresh isolated
# crash should recover fast, not inherit a long delay from an earlier crash loop.
echo "supervisor: launching gateway ($PY publisher.py)"
backoff=2
while true; do
  start=$(date +%s)
  "$PY" publisher.py
  code=$?
  ran=$(( $(date +%s) - start ))
  [ "$ran" -ge 60 ] && backoff=2          # ran healthily -> reset
  echo "supervisor: gateway exited ($code) after ${ran}s. restarting in ${backoff}s…"
  sleep "$backoff"
  backoff=$(( backoff * 2 ))
  [ "$backoff" -gt 30 ] && backoff=30     # cap at 30s
done

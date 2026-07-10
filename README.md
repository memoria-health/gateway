# nemara-gateway

Edge agent that turns LAN IP cameras (Tapo C100 over RTSP) into LiveKit
participants — so a mini PC in a facility can bridge many cheap cameras into the
existing Nemara/Memoria pipeline, the same way the iPhone NemaraCam app does.

> **Status: SPIKE.** This repo currently exists to *validate feasibility* with
> throwaway tools. It does **not** touch `nemara-backend`, dev, or prod. Nothing
> here connects to the real system yet.

## The bet we're testing

`Tapo C100 (RTSP, H.264 on the LAN) -> mini PC -> LiveKit room (cam_<id>) -> existing backend`

If a Tapo stream can become a LiveKit `cam_*` participant, the backend can't
tell it apart from an iPhone camera, and everything downstream (detection, live
view, clips) works unchanged.

## Validation ladder (do these in order, cheapest first)

| Milestone | Question | How | Touches our system? |
|---|---|---|---|
| **M1** | Can we access the stream? | `scripts/01-probe-stream.sh`, `scripts/02-view-stream.sh` | No — pure local |
| **M2** | Can it reach LiveKit? | local `livekit-server --dev` + `scripts/03-publish-to-livekit.sh` | No — local LiveKit only |
| **M3** | Does it plug into *our* backend? | publish a `cam_<id>` into a `patient_<id>` room on a **clone** of the backend | A clone, never the live dev/prod |

## Setup

```
cp .env.example .env     # (already created with the Tapo creds)
brew install ffmpeg      # need >= 7.1 for the WHIP muxer (M2)
```

Then find the camera's LAN IP and put it in `.env` as `CAM_IP`:
- Bell router admin page → device list, or the Tapo app → camera → device info, or
- `arp -a | grep -i tp-link` / an `nmap -sn` scan of your subnet.

## M1 — access the stream (start here)

```
chmod +x scripts/*.sh
./scripts/01-probe-stream.sh   # prints codec / resolution / fps / audio
./scripts/02-view-stream.sh    # opens a live window
```
**Success = it plays, and the probe shows `h264`.** Note the resolution/fps and
whether there's an audio track. `stream1` = 1080p, `stream2` = low-res (your
built-in "high-res for viewing / low-res for the detector bot" split).

## M2 — get it into LiveKit (local only, not prod)

1. Run a **local** LiveKit server (isolated from prod):
   `livekit-server --dev`  (uses well-known keys `devkey` / `secret`)
2. Generate a publish token (LiveKit CLI):
   `lk token create --api-key devkey --api-secret secret --join --room gateway-spike --identity cam_spike01 --valid-for 24h`
   then `export LIVEKIT_TOKEN=<that token>`
3. Publish: `./scripts/03-publish-to-livekit.sh`
4. Watch it: open LiveKit's Meet sample / agents-playground pointed at
   `ws://localhost:7880` and join room `gateway-spike`. **Success = the camera
   shows up as a participant with live video.**

If `-c:v copy` (H.264 passthrough) is rejected, let ffmpeg re-encode to a
WebRTC-friendly baseline — see the comment in the script.

> Alternative to ffmpeg-WHIP: self-hosted **LiveKit Ingress** can pull RTSP
> directly. Worth evaluating once M1/M2 pass.

## M3 — link into our system (later, isolated)

When M1+M2 pass, **clone `nemara-backend` separately** and point the publish at a
`patient_<id>` room with a `cam_<id>` identity (real publish token), then confirm
the cloned backend's detector picks it up. Never run experiments against the live
dev or prod backend.

## What this becomes (not built yet)

The real gateway: per-camera RTSP→LiveKit publishers, `desired_state` reconcile
(join/leave), client-side motion → `motion_state` packets, reconnect/health,
per-camera publish-token provisioning from the backend, and systemd packaging for
the mini PC. One gateway per facility.

# nemara-gateway

Edge appliance software: turns LAN IP cameras (RTSP — Tapo is the
reference model) into LiveKit participants, so a small box in a family's
home bridges cheap cameras into the Memoria pipeline. Downstream —
detection, identity, live view, clips — cannot tell a gateway camera
from a phone camera.

**Status: deployed.** Real boxes run this in production. Changes go
through a branch + PR, never straight to `main`.

```
IP cameras (RTSP/H.264, LAN) ──> gateway box ──WebRTC──> LiveKit room patient_<id>
                                                          participants cam_<camera-id>
                                                          │
                                                          ▼
                                              backend / GPU fleet / apps
```

## The three documents

| Doc | Read it when |
|---|---|
| **This README** | first contact — what the repo is, how to run it |
| [`GATEWAY_ACCESS.md`](GATEWAY_ACCESS.md) | operating a real box — access model, ops procedures. **Source of truth**; if it disagrees with anything else, it wins. No live credentials belong in it (that rule exists because it was violated once). |
| [`FIELD_TEST_INSTRUCTIONS.md`](FIELD_TEST_INSTRUCTIONS.md) | bench-validating a NEW camera model on a Raspberry Pi — self-contained, needs no production credentials until its final phase |

## What runs on a box

- **`publisher.py`** — the gateway itself: roster-driven, multi-camera
  fan-out. Pulls each configured camera's RTSP stream and publishes it
  as a `cam_<id>` participant into the home's LiveKit room. Supervises
  each camera in-process (RTSP + LiveKit reconnects).
- **`run.sh`** — process-level supervisor for dev/Mac use (exponential
  backoff restart). On real boxes systemd replaces it
  (`deploy/memoria-gateway.service`, `Restart=always`).
- **`setup_server.py`** — the installer UX (camera-pairing portal shown
  during setup).
- **`discover.py`** — LAN camera discovery probe.
- **`viewer/` + `viewer_server.py`** — local stream viewer for bench
  debugging.
- **`deploy/`** — everything that turns a fresh machine into a gateway:
  one-shot `setup.sh` provisioner, systemd units, first-boot service,
  Wi-Fi onboarding portal, unattended-install images. See
  [`deploy/README.md`](deploy/README.md).

## Credentials model (2026-08 hardening)

The box holds **no LiveKit secret**. The backend mints short-lived
LiveKit tokens for the gateway; a missing credential fails loudly
(there are deliberately no working defaults in `setup_server.py`).
Camera RTSP credentials live in the box-local config, never in git.
`.env` files are gitignored.

## Run it locally (Mac/dev)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp cameras.example.json cameras.json   # fill in your LAN camera(s)
./run.sh                               # supervised publisher
```

Backend URL, room, and token endpoint come from the environment —
`FIELD_TEST_INSTRUCTIONS.md` §Phase 4 documents exactly which values
exist and which two are handed over out-of-band (test-account login,
LiveKit API secret). Nothing else is ever shared.

## Provision a real box

Fresh Ubuntu → full gateway:

```bash
sudo deploy/setup.sh
```

Idempotent; installs the systemd units, camera portal, and Wi-Fi
onboarding portal. `deploy/autoinstall/` builds hands-off install media
for bench batches. Recovery paths (box offline after a Wi-Fi portal
test, etc.) are in `deploy/README.md` and `GATEWAY_ACCESS.md`.

## Known constraints

- Cameras must expose a **local RTSP/ONVIF stream**. Many consumer
  Wi-Fi cameras (Tuya-cloud family, incl. some Trueview models) are
  cloud-only and can never work with the gateway — check before buying.
  The bench procedure in `FIELD_TEST_INSTRUCTIONS.md` is the definitive
  test for a candidate model.
- One gateway serves one home (one LiveKit room). Multi-camera within
  the home is the normal case.

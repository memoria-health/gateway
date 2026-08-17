# Memoria Gateway — Raspberry Pi + Trueview Camera Test

**Audience:** engineer setting up a test gateway on a Raspberry Pi and validating
a Trueview camera against our pipeline.
**Goal:** confirm the Trueview model serves a local RTSP stream our gateway can
pull, find its exact RTSP URL, and report stream details back.

Phases 1–3 (the core test) run fully standalone — no Memoria credentials
needed. Phase 4 connects the box to our **production** backend under a test
caregiver account.

---

## Credentials — what comes from where (read this first)

Two different kinds of config; don't mix them up:

1. **`.env` — you create this yourself on the Pi** (step 3b). It is gitignored,
   so it does NOT come with the repo, and nobody sends it to you. It holds only
   values about **your camera on your bench**: its IP, its RTSP user/password
   (you set those in the Trueview app), and its stream path. It's used only by
   the probe scripts in Phase 3. (The `LIVEKIT_*` lines at the bottom of
   `.env.example` are leftovers for an old spike script — ignore them.)

2. **Production service credentials — fixed values, listed in Phase 4.** These
   connect the box to the Memoria backend + LiveKit. They are passed on the
   `setup.sh` command line (Phase 4), NOT put in `.env`. The only two things
   Aryaman must send you separately are the **test-account email and password** to sign in
   with and the **LiveKit API secret**.

---

## 0. What you need

- **Raspberry Pi 4 (4 GB+) or Pi 5**, with a microSD card (≥16 GB) and power
  supply. A Pi 3 is too weak for the full pipeline — OK for Phase 3 only.
- **The Trueview camera**, powered on and joined to the same Wi-Fi/LAN as the
  Pi (join it to Wi-Fi with the Trueview phone app first).
- **⚠️ FIRST, verify the model supports local RTSP/ONVIF at all.** Many Indian
  consumer Wi-Fi cameras (Tuya-cloud-based, incl. some Trueview models) are
  cloud-only and expose **no local stream** — the gateway cannot use those,
  full stop. Check the model's spec sheet for "ONVIF" / "RTSP", and look in
  the app for a setting like *Local RTSP*, *ONVIF*, or *NVR mode* (enable it
  if present). Phase 3 is the definitive test either way.
- The camera's **RTSP/ONVIF username + password** (often set in the app when
  enabling ONVIF; some models default to `admin` + blank or the device
  verification code).
- A laptop on the same network.
- Internet on the Pi during setup (for `apt` + `pip`).

## 1. Flash the Pi

**The OS must be 64-bit** — our LiveKit Python dependency ships ARM wheels for
64-bit (`aarch64`) only; a 32-bit OS will fail at `pip install`.

1. Install **Raspberry Pi Imager** on your laptop (raspberrypi.com/software).
2. Choose OS → **Raspberry Pi OS Lite (64-bit)** (~0.5 GB download; no desktop
   needed). Ubuntu Server 24.04 (64-bit) also works if you prefer it.
3. In Imager's settings (gear icon): set hostname `memoria-testbox`, enable
   SSH, set a username/password, and pre-fill your Wi-Fi.
4. Flash the microSD, boot the Pi, then from your laptop:
   `ssh <user>@memoria-testbox.local`
5. Sanity check: `uname -m` must print `aarch64`.

## 2. Get the gateway code

Aryaman will send you a zip of the repo (~3 MB) — or, if he's added your
GitHub account, you can clone `github.com/potaton00b/nemara-gateway` (private)
directly.

First, install the base packages on the Pi:

```bash
sudo apt update && sudo apt install -y git ffmpeg python3-venv net-tools unzip rsync
```

**Zip path:** copy the zip from your laptop to the Pi, unpack it, and restore
script permissions (zipping can strip them):

```bash
# on your laptop, from the folder containing the zip:
scp nemara-gateway.zip <user>@memoria-testbox.local:~

# then on the Pi:
unzip nemara-gateway.zip -d ~ && cd ~/nemara-gateway
chmod +x scripts/*.sh deploy/*.sh run.sh
```

(If the unzipped folder has a longer name, `mv` it to `~/nemara-gateway` so the
later commands match.)

**Clone path (only if you have GitHub access):**

```bash
git clone https://github.com/potaton00b/nemara-gateway.git ~/nemara-gateway
cd ~/nemara-gateway
```

## 3. Find the camera and probe its stream  ← the core of this test

### 3a. Find the camera's IP

Read it from the Trueview app / your router's device list, or run our scanner:

```bash
python3 discover.py
# → {'ip': '192.168.1.57', 'rtsp_port': 554, 'onvif': ..., 'mac': 'xx:xx:...'}
```

Notes:
- The scanner only sweeps **port 554**. If it finds nothing but the camera is
  online, the camera may use a different RTSP port (8554 and 10554 are common
  on budget models — check the app/specs), or RTSP isn't enabled/supported
  (see the ⚠️ in section 0).
- Ignore the `onvif` true/false flag — it probes a Tapo-specific port and is
  meaningless for Trueview.

### 3b. Configure the probe

```bash
cp .env.example .env
nano .env
```

Fill in the camera section only:

```
CAM_USER=<rtsp/onvif username>
CAM_PASS=<rtsp/onvif password>
CAM_IP=<ip from step 3a>
CAM_PATH=<see below>
```

`CAM_PATH` is the RTSP URL path — **finding the right one for this Trueview
model is a key deliverable**. Trueview rebadges different OEM boards, so try
these in order (re-run the probe after each):

| Try | `CAM_PATH` value                                  | OEM family |
|-----|---------------------------------------------------|------------|
| 1   | `stream1`                                          | generic    |
| 2   | `live/ch00_0`                                      | Tuya/Ppstrong |
| 3   | `onvif1`                                           | Tuya ONVIF |
| 4   | `user=admin&password=&channel=1&stream=0.sdp?real_stream` | XM/ICSee |
| 5   | `Streaming/Channels/101`                           | Hikvision OEM |
| 6   | `cam/realmonitor?channel=1&subtype=0`              | Dahua OEM  |
| 7   | `h264Preview_01_main`                              | Reolink OEM |

Still nothing? Query the camera over ONVIF for its real stream URI — from the
Pi:

```bash
python3 -m venv /tmp/onvif && /tmp/onvif/bin/pip install onvif-zeep wsdiscovery
/tmp/onvif/bin/python - <<'EOF'
from wsdiscovery.discovery import ThreadedWSDiscovery as WSD
wsd = WSD(); wsd.start()
for s in wsd.searchServices():
    print(s.getXAddrs())   # ONVIF endpoint, e.g. http://192.168.1.57:8000/onvif/device_service
wsd.stop()
EOF
```

Then search "<Trueview model> RTSP URL" — or use the ONVIF endpoint + ONVIF
Device Manager (free Windows tool) to read the stream URI directly.

### 3c. Probe it

```bash
./scripts/01-probe-stream.sh
```

**PASS looks like:**

```
codec_name=h264          ← see below if it says hevc/h265
width=1920
height=1080
avg_frame_rate=15/1      ← anything 10–30 fps is fine
codec_type=video
codec_name=pcm_alaw      ← audio; aac or pcm_mulaw also fine — just record it
codec_type=audio
```

**Record ALL of this output plus the full working RTSP URL** — that's the main
deliverable.

Specific things to note:
- **`codec_name=hevc` (H.265):** look in the Trueview app/settings for a video
  encoding option and switch to **H.264**, then re-probe. H.264 matters on a
  Pi — it decodes far cheaper. If the model is H.265-only, that's a finding:
  report it (the Pi may still cope at low fps, but we need to know).
- **Audio codec** (`pcm_alaw` / `pcm_mulaw` / `aac`): all decode fine, just
  record which. **No audio line at all:** check the app for an audio/record-
  sound toggle; if none, report "no audio track".
- **Auth error (401)**: wrong RTSP credentials — some models want the ONVIF
  account, not the app login.
- **Connection refused / timeout on every path:** likely a cloud-only model
  (section 0 ⚠️) — report that; it disqualifies this model.

### 3d. Eyeball it

The Pi has no screen, so run the viewer from your **laptop**: install ffmpeg
(`brew install ffmpeg` / winget / apt), copy the repo + your `.env` there, and:

```bash
./scripts/02-view-stream.sh
```

Watch ~2 minutes for smooth motion, no gray smearing, no freezes.

### 3e. Stress: concurrent pulls

Budget cameras cap concurrent RTSP clients (Tapo ≈ 2–3; Trueview unknown —
that's why we test). Open the viewer 2–3 times in parallel terminals and note
at how many the camera refuses or degrades. Then power-cycle the camera and
time how long until the stream is pullable again.

**→ If Phase 3 passes, the model is gateway-compatible — send the Phase 3
results right away, then continue to Phase 4 to prove it end-to-end on prod.
If Phase 3 fails, stop and report; Phases 4–5 can't work without it.**

## 4. Install the full gateway service, connected to PROD

This registers the Pi as a real gateway on the **production** backend, under
the **test caregiver account** only.

> **⚠️ Production rules**
> - Sign in to the portal ONLY with the test-account email Aryaman gives you.
> - **Generate a fresh gateway token** as shown below. NEVER copy the
>   `gateway_token` you may see in `GATEWAY_ACCESS.md` — that is the identity
>   of the live gateway box running in a real home in the US; reusing it will
>   hijack that box's cameras.

Run (one command; get `LIVEKIT_SECRET` from Aryaman or `GATEWAY_ACCESS.md` §6):

```bash
cd ~/nemara-gateway
sudo BACKEND_URL="https://nemara-backend-production.up.railway.app" \
     LIVEKIT_URL="wss://nemara-pdx0uelu.livekit.cloud" \
     LIVEKIT_KEY="<LIVEKIT_API_KEY-REDACTED>" \
     LIVEKIT_SECRET="<from Aryaman>" \
     SUPABASE_URL="https://zgifmvjnggmgzvtnosnu.supabase.co" \
     SUPABASE_ANON="sb_publishable_OsfCMOdEIb5gvhRWDyNivg_7GriAmyP" \
     GATEWAY_TOKEN="gw-test-$(openssl rand -hex 12)" \
     ENABLE_WIFI_PORTAL=0 \
     deploy/setup.sh
```

- `ENABLE_WIFI_PORTAL=0` is **required on the Pi** — that feature downloads an
  x86-only binary.
- Record the generated token: `grep gateway_token /opt/memoria-gateway/cameras.json`
  — put it in your report so we can identify (and later clean up) this test
  gateway in the backend.
- Start it: `sudo systemctl start memoria-gateway memoria-setup`

### 4a. Register the camera via the portal

From your laptop, open `http://memoria-testbox.local:8088` (or the Pi's IP)
→ **Scan** → the Trueview camera should appear → sign in with the
**test-account email** → name the camera → **Provision**.

**Known limitation, not your bug:** the portal currently assumes the Tapo URL
shape (`:554/stream1`) for thumbnails and for the RTSP URL it saves — so the
scan will list the camera, but the thumbnail will likely be blank and the
saved URL will be wrong for a Trueview. Fix it by hand after provisioning:

```bash
sudo nano /opt/memoria-gateway/cameras.json
# in each camera entry, replace the "rtsp" value with the FULL working URL
# you found in Phase 3, e.g. "rtsp://user:pass@192.168.1.57:554/<path>"
sudo systemctl restart memoria-gateway
```

- While it runs, watch CPU: `htop` — note the ffmpeg + python CPU % in your
  report (we need to know if a Pi 4 keeps up: decode + publish of one 1080p
  stream is the heaviest thing it does).

```bash
systemctl status memoria-gateway --no-pager     # running?
journalctl -u memoria-gateway -n 40 --no-pager  # recent logs
```

Healthy logs: `source started — 1 RTSP pull, N publisher(s)` then
`stream 1920x1080@15 — pipeline up`. (The publisher only joins LiveKit when
someone actually opens the live view — Phase 5 — so "pipeline up" without a
LiveKit connect line is normal until then.)

Two cosmetic things you may notice — ignore both, they're tuned for our US
camera and we'll adjust later: the logo overlay box is positioned for a
different watermark, and audio has a 5× gain boost (if Trueview audio sounds
distorted/too loud, just note it).

## 5. End-to-end check in the app

Sign in to the Memoria app/dashboard with the same test account (Aryaman will
tell you which) and open the camera's live view: video should start within a
few seconds, with audio. Leave it open 30+ minutes and note any freezes or
reconnects (`journalctl -u memoria-gateway -f` shows them live).

## 6. What to report back

```
Pi model + RAM: ______, OS (uname -m must be aarch64): ______
Camera: Trueview <exact model>, firmware: ______
Local RTSP supported at all:                YES / NO
Full working RTSP URL: rtsp://user:***@ip:PORT/PATH  ← the key deliverable
--- ffprobe output (paste all of it) ---

--- results ---
[ ] Probe succeeded (3c)                    PASS / FAIL
    video codec: ____  resolution: ____  fps: ____
    audio codec: ____  (or NONE)
    H.264 switchable in app (if hevc):      YES / NO / n-a
[ ] Live view smooth for 2 min (3d)         PASS / FAIL
[ ] Max concurrent RTSP pulls (3e):         ____
[ ] Recovery time after camera power-cycle: ____ s
[ ] Full service ran; CPU % (4):            PASS / FAIL / skipped;  ____%
    gateway_token used (from cameras.json): gw-test-____
[ ] Live view worked in the app (5)         PASS / FAIL / skipped
    stable for 30 min:                      YES / NO
Anything odd (artifacts, lag, reconnects, log errors): ______
```

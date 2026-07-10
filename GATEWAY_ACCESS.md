# Memoria Gateway Box — Access & Operations (for an AI agent)

Everything another Claude needs to reach, edit, deploy, and debug the physical
gateway appliance. Credentials are committed on purpose (project policy).

> TL;DR: `ssh potaton00b@memoria-gateway.local` (password `qwerty123`). No SSH
> key exists — it's **password auth**, and this Mac has **no `sshpass`**, so you
> must drive SSH/rsync with **`expect`** (templates below). `sudo` needs a PTY
> (`ssh -tt`). The service is `memoria-gateway` (systemd). Deploy = edit local
> `publisher.py` → `rsync` to the box → `sudo systemctl restart memoria-gateway`.

---

## 1. What the box is

- **Hardware:** Dell OptiPlex 3060 Micro, 4 cores, ~7 GB RAM, Intel **AX210** M.2
  Wi-Fi card (interface `wlp2s0`; the OptiPlex had no built-in Wi-Fi).
- **OS:** Ubuntu **26.04 LTS**, headless (no desktop — intentional). Python **3.14.4**.
- **Role:** The Memoria gateway appliance. Pulls RTSP from Tapo cameras and
  **fans out** each physical stream to N LiveKit rooms (one physical camera →
  one camera row *per patient*). Talks only to the backend API + LiveKit Cloud +
  Supabase Auth — never the DB directly.

## 2. Network — reach it BY NAME, never by IP

- **Primary:** `memoria-gateway.local` (mDNS/avahi; IP-agnostic). **Always use
  the name** — the box is on DHCP so its IP moves.
- **Current IP:** `10.0.0.51` (fallback only). **Wi-Fi MAC:** `28:0c:50:69:69:e1`
  (last resort: MAC-scan the /24).
- **Wi-Fi:** SSID **`Suburban Living`**, password **`GoBearz420626569`**.
  NetworkManager-managed, autoconnect on. (PSK also recoverable on-box via
  `sudo grep -r psk= /etc/NetworkManager/system-connections/`.)
- **If unreachable:** (a) box may be **off** — check the power light (BIOS AC
  recovery is now **On**, so a power cut should auto-boot it); (b) it may have
  dropped Wi-Fi and raised a **`Memoria-Setup-…`** setup hotspot; (c) `.local`
  not resolving on a fresh machine → `dscacheutil -flushcache`.

## 3. SSH / credentials

- **User:** `potaton00b`  **Password:** `qwerty123`  (sudo password is the same).
- **RTSP (Tapo camera) password is also `qwerty123`** (accounts `camera1` /
  `camera2`) — coincidence, same string.
- **No SSH key is configured.** It's password auth. This Mac has **no `sshpass`**
  → use `expect`. If you want key-based access, copy a pubkey into the box's
  `~/.ssh/authorized_keys` first (one-time, via the expect password flow).

### expect templates (this is how you actually connect)

**Read-only command (no sudo):**
```tcl
cat > /tmp/gw.exp <<'EXP'
set timeout 40
spawn ssh -o StrictHostKeyChecking=accept-new potaton00b@memoria-gateway.local {REMOTE_CMD_HERE}
expect {
  -re {[Pp]assword:} { send "qwerty123\r"; exp_continue }
  -re {yes/no} { send "yes\r"; exp_continue }
  eof
}
EXP
expect -f /tmp/gw.exp
```

**Command needing sudo — MUST use `ssh -tt` (sudo needs a real TTY):**
```tcl
spawn ssh -tt -o StrictHostKeyChecking=accept-new potaton00b@memoria-gateway.local {sudo systemctl restart memoria-gateway; systemctl is-active memoria-gateway}
# answer BOTH the ssh prompt AND the "[sudo] password" prompt with qwerty123
expect {
  -re {[Pp]assword:} { send "qwerty123\r"; exp_continue }
  -re {yes/no} { send "yes\r"; exp_continue }
  eof
}
```
Tip: scrub the password out of any output you print (`sed 's/qwerty123/***/g'`).

## 4. Where the software lives

- **Dir:** `/opt/memoria-gateway/`
- **venv:** `/opt/memoria-gateway/.venv` (Python 3.14.4; has `numpy` 2.5.0,
  `livekit` rtc+api, aiohttp).
- **Files:**
  - `publisher.py` — the main **roster-driven, multi-camera, fan-out publisher**
    (RTSP pull → LiveKit publish, per-camera reconnect, motion detection, audio).
    This is the file you'll edit 99% of the time.
  - `discover.py` — subnet/RTSP discovery + snapshots (used by the setup portal).
  - `setup_server.py` — phone setup portal (see §7).
  - `viewer_server.py` — local debug viewer.
  - `cameras.json` — the config (see §6).
  - `cameras.dev.json.bak` — backup of the DEV config (restore to point at dev).

- **systemd services (all ENABLED on the OptiPlex — verified 2026-07-09):**
  - `memoria-gateway` — the publisher (`Restart=always`, `ExecStart=…/.venv/bin/python publisher.py`).
  - `memoria-setup` — camera setup portal on `:8088` (enabled on boot; §7).
  - `memoria-tunnel` — off-LAN reverse-SSH relay: `autossh -M 0 -N -R 22001:localhost:22 tunnel@144.126.211.110`, key `~/.ssh/memoria_tunnel_key`. **Port 22001 is hardcoded → clones collide** (firstboot reassigns it per-box; see §12).
  - `memoria-wifi-portal` — captive-portal hotspot when offline. `wifi-portal.sh`: `sleep 8 → nm-online -t 20 → if offline, wifi-connect --portal-ssid Memoria-Setup-<mac-suffix>`. Self-gating: silent when Wi-Fi is already up.
  - **`memoria-firstboot` — NOT installed on the OptiPlex** (it was hand-built; `setup.sh`'s firstboot step never ran here). It only exists in a *template* image, installed by `prep-golden-image.sh` (§12). This is why a plain clone of this box is a twin, not a blank unit.
- **Wi-Fi is NetworkManager-managed.** Saved connections: `Suburban Living` (active) + `jesstan`. Renderer set via `/etc/netplan/01-network-manager-all.yaml`; connections exported to `/etc/netplan/90-NM-*.yaml`. `systemd-networkd` is also active but only owns the (unused) ethernet.

## 5. Deploy / edit workflow

**Source of truth is the local git repo**, not the box:
`/Users/sunnycui/Documents/code/1.8-nemara-demo-ready/nemara-gateway/` (`publisher.py`
is committed there). Edit locally, then:

```bash
# 1) rsync the edited file to the box (drive the password with expect)
rsync -az /Users/sunnycui/Documents/code/1.8-nemara-demo-ready/nemara-gateway/publisher.py \
  potaton00b@memoria-gateway.local:/opt/memoria-gateway/publisher.py
# 2) restart the service (sudo → ssh -tt + expect)
ssh -tt potaton00b@memoria-gateway.local "sudo systemctl restart memoria-gateway"
# 3) verify
ssh potaton00b@memoria-gateway.local "systemctl is-active memoria-gateway; \
  journalctl -u memoria-gateway --no-pager -n 20"
```
Healthy startup logs look like: `source started — 1 RTSP pull, N publisher(s)`
then `stream 1920x1080@15 — pipeline up`.

## 6. cameras.json (current — PROD)

```jsonc
{
  "livekit_url":        "wss://nemara-pdx0uelu.livekit.cloud",   // shared dev+prod project
  "livekit_api_key":    "APITxi4QucycnEW",
  "livekit_api_secret": "254B2sRXlByHCPe1JpBPfE56YUcfxFXYKOxX29fn2vbA",
  "backend_url":        "https://nemara-backend-production.up.railway.app",  // PROD (Railway)
  "poll_seconds": 1,
  "gateway_token":      "gw-prod-token-2026-B1VGBaerS8wuTULm",    // maps to a prod gateways row
  "cameras": [ /* 6 entries = FAN-OUT of 2 physical Tapos */ ]
}
```
- **Fan-out:** the 6 `cameras` entries are only **2 physical Tapos**:
  - `10.0.0.145` (RTSP `camera1:qwerty123`) → `cam_53ba55c9`, `cam_619d9526`, `cam_5089d97a`
  - `10.0.0.223` (RTSP `camera2:qwerty123`) → `cam_c4218fa0`, `cam_fcaadb06`, `cam_260225e7`
  A camera runs only if it's in BOTH this local map AND the backend roster
  (`GET /v1/gateway/roster`, authed with `gateway_token`), which is the source of
  truth for which cameras run + their `desired_state`.
- **To point the box at DEV instead of PROD:** swap `backend_url`, `gateway_token`
  (and the systemd env if set), keep LiveKit unchanged (shared project). Restore
  `cameras.dev.json.bak` for the dev camera set. Then restart the service.

## 7. Setup portal (onboarding a new box / adding cameras)

- Start it: `sudo systemctl start memoria-setup` → serves
  `http://memoria-gateway.local:8088` (dual-stack IPv6+IPv4 — required, IPv6-only
  homes broke IPv4-only binds).
- Phone flow: scan subnet for Tapos → sign in by **email only** (no password) →
  name → Provision → registers the camera in the backend roster + appends
  `{livekit_identity, rtsp, mac}` to `cameras.json`; the publisher picks it up on
  the next poll (no restart). New Tapo prep: join it to the Wi-Fi via the Tapo
  app first (C100 is 2.4 GHz only), set its Camera Account (RTSP creds).

## 8. How it works (architecture, brief)

- `CameraSource` = one process per **unique RTSP stream** (pull once: probe →
  ffmpeg → reader → motion → MAC-rediscover). It fans decoded frames + audio to N
  `Publisher`s, one per patient/LiveKit room. A `Publisher` joins LiveKit only
  when that patient's live view opens (its `desired_state` goes `connected`).
  (Rationale: a Tapo caps ~2-3 concurrent RTSP clients, so pull once, fan out.)
- **Two ffmpegs per physical camera:** video (with the Memoria logo overlay
  covering the Tapo watermark) and audio (G.711 A-law 8k → 48k mono PCM). Audio
  path has low-latency flags + a small AudioSource queue.
- **Motion** is computed in `publisher.py::_reader_loop` (vectorized numpy
  frame-diff; keep it vectorized — a pure-Python pixel loop here starves the
  audio task and desyncs A/V).
- **MAC-based re-discovery:** on RTSP probe-failure a camera relocates by MAC
  (facility DHCP). `mac` may be `null` (then it can't auto-relocate — set a DHCP
  reservation).

## 9. Diagnostics cheat-sheet

```bash
systemctl status memoria-gateway --no-pager           # running? failed?
systemctl is-enabled memoria-gateway                  # starts on boot?
journalctl -u memoria-gateway --no-pager -n 40        # recent logs / errors
ps -eo pcpu,comm --sort=-pcpu | head                  # python = event loop; ffmpeg = per-camera
uptime; nproc                                         # load vs 4 cores
ping -c6 -q 8.8.8.8                                    # RTT + mdev (jitter)
nmcli -t -f NAME,DEVICE,STATE con show --active        # Wi-Fi connected?
```

## 10. Firmware / power

- **BIOS "AC Power Recovery" = On** (auto-boots after a true AC loss). Set
  remotely via the kernel `dell-wmi-sysman` sysfs (no monitor, no Dell tool):
  ```bash
  sudo bash -c 'echo -n On > /sys/class/firmware-attributes/dell-wmi-sysman/attributes/AcPwrRcvry/current_value'
  ```
  (`possible_values` = `Off;On;Last`; no BIOS admin password set → direct write.)
  This is **per-machine firmware** — it does NOT travel in a disk clone, so any
  cloned box needs it set again (bake into first-boot provisioning for a fleet).

## 11. Gotchas

- **Name, not IP.** If `memoria-gateway.local` doesn't resolve, the box is off or
  off-Wi-Fi (not "wrong IP").
- **No `sshpass`** on the Mac → always `expect`. **`sudo` → `ssh -tt`** (needs a PTY).
- The 6 `cameras` are a **fan-out of 2 physical Tapos**, not 6 devices.
- Deploying/redeploying the **backend** (Railway) — not the box — can orphan the
  detector bot from already-live LiveKit rooms until the camera reconnects; if a
  patient's analyses go silent after a backend deploy, bounce that camera
  (Disconnect→Reconnect in the app, or toggle `cameras.desired_state`).
- **Cloning a new box?** A plain clone of this OptiPlex is a *twin* (same token,
  same machine-id, same tunnel port, same Wi-Fi) — it will fight the OptiPlex.
  To make a proper self-onboarding **template**, see §12.

## 12. Golden image & fleet templates (cloning new boxes)

**Tooling:** Clonezilla, restored/saved to a USB. On a Dell OptiPlex the one-time
boot-menu key is **F12**; a Lenovo ThinkCentre is also **F12** (BIOS = F1).

**Two kinds of image — do not confuse them:**
- **Plain clone (IMAGE-A):** a `savedisk` of a live box as-is. It carries that
  box's identity (token, machine-id, SSH keys, tunnel port, saved Wi-Fi) and has
  **no** `memoria-firstboot`. Flashing it makes a **twin**. Useful only as a
  backup/restore of that *same* box.
- **Template (IMAGE-B):** a box that was **generalized** with
  `deploy/prep-golden-image.sh` before capture. Every clone self-onboards.

**The per-box things that MUST differ between units** (all handled by firstboot):
machine-id, SSH host keys, `gateway_token` (in `cameras.json`), the reverse-SSH
**tunnel port** (baked unit hardcodes 22001), and the saved **Wi-Fi** (deleted so
the hotspot raises). Firmware **AC-recovery does NOT travel in any image** — set
it per box (Dell: sysfs, §10; Lenovo: BIOS → Power → After Power Loss → On).

**Build a template — never on the live OptiPlex (prep wipes its identity + Wi-Fi).
Use a disposable workbench box (e.g. the Lenovo), at its console (no ethernet
anywhere; Wi-Fi only):**
1. Flash the workbench box from any working image (IMAGE-A) — gives it a working
   OS + code base.
2. Bring it online at the console over Wi-Fi: `ip link` → find the Wi-Fi iface →
   `nmcli device wifi connect "Suburban Living" password "GoBearz420626569"` →
   `git pull` the repo (this is why the code lives on GitHub: so the box can pull it).
3. `sudo bash deploy/prep-golden-image.sh` — installs `memoria-firstboot`, deletes
   saved Wi-Fi, blanks identity, arms the run-once flag.
4. `sudo poweroff` (**do NOT reboot** — a reboot runs firstboot and un-arms it).
5. Boot the **Clonezilla USB** → `savedisk` → that image is IMAGE-B, the template.

**Flash a field box from IMAGE-B:** boot Clonezilla → `restoredisk` → target =
its internal SSD (⚠ erases it) → poweroff → remove USB → power on. **First boot
only:** `Memoria-Setup-XXXX` hotspot appears → join from a phone, enter the site
Wi-Fi password → box comes online → firstboot mints a unique token + machine-id +
SSH keys + tunnel port, then disables itself. **Every boot after:** straight to
running. Then add cameras via the phone portal at `http://<box>.local:8088`.

**"Exactly once" guarantee (two independent guards):** the firstboot unit has
`ConditionPathExists=!/opt/memoria-gateway/.provisioned`, and the script both
`touch`es that flag and `systemctl disable`s itself on success. `prep-golden-image.sh`
is the only thing that removes the flag (i.e. re-arms). An already-set-up box (or
the OptiPlex, which has no firstboot at all) never re-provisions.

**Clonezilla `savedisk` quick path:** boot USB → *Clonezilla live (default)* →
English → "Don't touch keymap" → Start_Clonezilla → `device-image` → `local_dev`
→ (Ctrl+C after detection) → pick the USB **data** partition → `exit_directory_browsing`
→ `<Done>` → `Beginner` → `savedisk` → name it → source = the **internal SSD**
(not the USB) → compression **z9p** → check-image **Yes** → `-p choose` → confirm.
`restoredisk` is the same until the action step. mDNS: two boxes both answer to
`memoria-gateway.local` — keep them on different networks during setup.

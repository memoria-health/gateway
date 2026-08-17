# Memoria Gateway Box — Access & Operations (for an AI agent)

Everything another Claude needs to reach, edit, deploy, and debug the physical
gateway appliance.

> **Credentials are NOT in this file.** Values in `<ANGLE_BRACKETS>` are
> placeholders — substitute them at the point of use.
>
> This doc previously read "credentials are committed on purpose (project
> policy)" and carried the live gateway/sudo password, the Tapo RTSP
> password, a home Wi-Fi password, and the **production** LiveKit API key
> and secret — in a tracked file on `origin/main`, where no `.gitignore`
> rule can reach them. All of those were rotated after the leak was found
> on 2026-08-03.
>
> A doc is not a secret store. Put real values in the password manager and
> reference them by name here. If you find yourself pasting a live
> credential into this file, that is the bug.
>
> | Placeholder | What it is |
> |---|---|
> | `<GATEWAY_PASSWORD>` | box login + sudo, also the Tapo RTSP password |
> | `<WIFI_PASSWORD>` | the home Wi-Fi the box joins |
> | `<LIVEKIT_API_KEY>` / `<LIVEKIT_API_SECRET>` | LiveKit Cloud, **production** project |

> TL;DR: `ssh potaton00b@memoria-gateway.local` (password `<GATEWAY_PASSWORD>`). No SSH
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
- **Wi-Fi:** SSID **`Suburban Living`**, password **`<WIFI_PASSWORD>`**.
  NetworkManager-managed, autoconnect on. (PSK also recoverable on-box via
  `sudo grep -r psk= /etc/NetworkManager/system-connections/`.)
- **If unreachable:** (a) box may be **off** — check the power light (BIOS AC
  recovery is now **On**, so a power cut should auto-boot it); (b) it may have
  dropped Wi-Fi and raised a **`Memoria-Setup-…`** setup hotspot; (c) `.local`
  not resolving on a fresh machine → `dscacheutil -flushcache`.

## 3. SSH / credentials

- **User:** `potaton00b`  **Password:** `<GATEWAY_PASSWORD>`  (sudo password is the same).
- **RTSP (Tapo camera) password is also `<GATEWAY_PASSWORD>`** (accounts `camera1` /
  `camera2`) — coincidence, same string.

### KEY AUTH (2026-07-29) — prefer this

`~/.ssh/memoria_ops.pub` is installed in the box's `authorized_keys`, so
ordinary ssh and rsync work with no expect wrapper and no password:

```bash
ssh -i ~/.ssh/memoria_ops potaton00b@memoria-gateway.local 'systemctl is-active memoria-gateway'
rsync -az -e "ssh -i ~/.ssh/memoria_ops" publisher.py potaton00b@memoria-gateway.local:~/nemara-gateway/
```

**`sudo` still wants the password.** The one-time fix, run once on the box,
lets the watchdog restart exactly one service and nothing else:

```bash
echo 'potaton00b ALL=(root) NOPASSWD: /usr/bin/systemctl restart memoria-gateway, /usr/bin/systemctl is-active memoria-gateway, /usr/bin/systemctl status memoria-gateway' \
  | sudo tee /etc/sudoers.d/memoria-gateway-watchdog >/dev/null
sudo chmod 440 /etc/sudoers.d/memoria-gateway-watchdog
sudo visudo -c -f /etc/sudoers.d/memoria-gateway-watchdog
```

Until that exists, `tools/sentinel.py restart_gateway` reports
`GATEWAY_RESTART_FAILED: sudo wants a password` rather than hanging. The
sentinel has NO password path any more — this hardware sits in someone's
home, and a shared password usable from any machine on the LAN is not an
acceptable control for it.

### Fleet access — how a box you have never touched lets you in

`deploy/ops_authorized_keys` holds the ops PUBLIC keys, and two scripts
bake it in:

* `deploy/prep-golden-image.sh` seeds it into the template, so every
  clone ships trusting those keys from first boot;
* `deploy/memoria-firstboot.sh` re-asserts it on the clone (self-heal,
  and it fixes a box imaged from a template cut before the seeding
  step existed, since the file itself ships in the repo).

Both are additive and idempotent: a key an operator added by hand is
kept, re-running never duplicates a line. Verified on Ubuntu 2026-07-30
— fresh box, re-run, hand-added key preserved, comments never written
as keys, output parses with `ssh-keygen -l`.

Reaching a box off-LAN is then: `python3 scripts/gen-ssh-config.py`
(reads each box's backend-assigned reverse port) → `ssh memoria-<name>`
→ relay hop → that box's port 22 → your key.

**DO NOT disable password auth yet.** Order matters, and getting it
wrong locks you out of the whole fleet:

1. cut a golden image WITH the seeding step (done — the code is here);
2. boot one real clone and confirm `ssh -i ~/.ssh/memoria_ops` works on
   it, with no password anywhere;
3. confirm the relay hop works for that box (needs the ops key on the
   relay — still pending);
4. only then set `PasswordAuthentication no`, and roll it to already-
   deployed boxes only after appending the key to each by hand.

Until step 4, the account password is load-bearing: it is the ONLY way
into a box whose image predates this change, including every box
currently in the field.

### expect templates (password fallback / a box without the key yet)

**Read-only command (no sudo):**
```tcl
cat > /tmp/gw.exp <<'EXP'
set timeout 40
spawn ssh -o StrictHostKeyChecking=accept-new potaton00b@memoria-gateway.local {REMOTE_CMD_HERE}
expect {
  -re {[Pp]assword:} { send "<GATEWAY_PASSWORD>\r"; exp_continue }
  -re {yes/no} { send "yes\r"; exp_continue }
  eof
}
EXP
expect -f /tmp/gw.exp
```

**Command needing sudo — MUST use `ssh -tt` (sudo needs a real TTY):**
```tcl
spawn ssh -tt -o StrictHostKeyChecking=accept-new potaton00b@memoria-gateway.local {sudo systemctl restart memoria-gateway; systemctl is-active memoria-gateway}
# answer BOTH the ssh prompt AND the "[sudo] password" prompt with <GATEWAY_PASSWORD>
expect {
  -re {[Pp]assword:} { send "<GATEWAY_PASSWORD>\r"; exp_continue }
  -re {yes/no} { send "yes\r"; exp_continue }
  eof
}
```
Tip: scrub the password out of any output you print (`sed 's/<GATEWAY_PASSWORD>/***/g'`).

### Off-LAN access (when NOT on the box's Wi-Fi) — via the DO relay

`memoria-gateway.local` (mDNS) only resolves on the **same LAN**. From anywhere
else, reach the box through the reverse-SSH relay — DigitalOcean droplet
**`memoria-relay`**, **`144.126.211.110`** (sfo3, `status=active`). This Mac's
`~/.ssh/config` already has the hops wired:

```bash
ssh memoria-box-01   # → ProxyJump memoria-relay → localhost:22001 → OptiPlex (password <GATEWAY_PASSWORD>)
```
Verified working off-network 2026-07-09.

- **Relay login:** `root@144.126.211.110`, key **`~/.ssh/memoria_vps`** (= the DO
  account key `memoria-admin`, fp `d3:e8:c3:60:…:54:84`; installed as root at
  droplet creation). The DO API token is in memory (`reference_digitalocean_token`).
- **`GatewayPorts no`** on the relay → `22001` binds to the relay's **localhost
  only**, so you CANNOT hit `144.126.211.110:22001` directly (it times out) — you
  MUST hop through (ProxyJump). That timeout is expected, not an outage.
- **Tunnel health check** (run on the relay): `ss -tlnp | grep 22001` → a `LISTEN
  127.0.0.1:22001` line means the box's `memoria-tunnel` (autossh) is connected.
- **Fresh Mac (no config)?** Copy the `memoria_vps` private key, then add:
  ```
  Host memoria-relay
      HostName 144.126.211.110
      User root
      IdentityFile ~/.ssh/memoria_vps
  Host memoria-box-01
      HostName localhost
      Port 22001
      User potaton00b
      ProxyJump memoria-relay
  ```
- **Per-box ports:** each unit uses a UNIQUE reverse port (firstboot assigns it;
  the OptiPlex = `22001`, recorded in `/opt/memoria-gateway/.tunnel-port`). Add a
  `Host memoria-box-NN` block per box with its own port.

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
  "livekit_api_key":    "<LIVEKIT_API_KEY>",
  "livekit_api_secret": "<LIVEKIT_API_SECRET>",
  "backend_url":        "https://nemara-backend-production.up.railway.app",  // PROD (Railway)
  "poll_seconds": 1,
  "gateway_token":      "gw-prod-token-2026-B1VGBaerS8wuTULm",    // maps to a prod gateways row
  "cameras": [ /* 6 entries = FAN-OUT of 2 physical Tapos */ ]
}
```
- **Fan-out:** the 6 `cameras` entries are only **2 physical Tapos**:
  - `10.0.0.145` (RTSP `camera1:<GATEWAY_PASSWORD>`) → `cam_53ba55c9`, `cam_619d9526`, `cam_5089d97a`
  - `10.0.0.223` (RTSP `camera2:<GATEWAY_PASSWORD>`) → `cam_c4218fa0`, `cam_fcaadb06`, `cam_260225e7`
  A camera runs only if it's in BOTH this local map AND the backend roster
  (`GET /v1/gateway/roster`, authed with `gateway_token`), which is the source of
  truth for which cameras run + their `desired_state`.
- **To point the box at DEV instead of PROD:** swap `backend_url`, `gateway_token`
  (and the systemd env if set), keep LiveKit unchanged (shared project). Restore
  `cameras.dev.json.bak` for the dev camera set. Then restart the service.

## 7. Setup portal (onboarding a new box / adding cameras)

- **Enabled on boot** → serves `memoria-gateway.local:8088`. **Dual-stack
  IPv6+IPv4** (IPv6-only homes broke IPv4-only binds) **AND now serves HTTP *and*
  HTTPS on the same port** (it peeks the first byte for the TLS handshake and
  wraps if so). This fixes the "stuck loading" seen when a browser (esp. Safari)
  auto-upgrades `memoria-gateway.local:8088` to https — the box now answers with a
  **self-signed cert** (generated per-box on first start; you can't get a trusted
  cert for a `.local` name, so it's a one-time "proceed anyway" warning). Plain
  `http://memoria-gateway.local:8088` skips the warning entirely.
- Phone flow: **Scan** subnet for Tapos (`discover.py` sweeps :554, credential-
  free) → sign in by **email only** (no password) → name → **Provision**.
  Provisioning uses **this box's own `gateway_token`** (firstboot syncs the token
  into the memoria-setup unit — §12), so it creates/updates *this* box's gateways
  row, then appends `{livekit_identity, rtsp, mac}` to `cameras.json`; the
  publisher picks it up next poll (no restart).
- **Camera creds:** the portal probes/pulls RTSP with **`camera1` / `<GATEWAY_PASSWORD>`**
  by default. A camera shows in the scan regardless (discovery is credential-free)
  but the **thumbnail** needs matching creds — if a Tapo's Camera Account differs,
  edit the per-camera `user`/`pass` fields + hit **↻**, or set the Tapo's Camera
  Account to `camera1`/`<GATEWAY_PASSWORD>`. New Tapo prep: join it to Wi-Fi via the Tapo
  app first (C100 is 2.4 GHz only), then set its Camera Account.

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

**STATUS (built + validated 2026-07-11):** the working template is
**`memoria-template-final`** on the golden-image USB. Flash it → box self-onboards
(hotspot → Wi-Fi), mints a unique identity, portal answers http+https, and
provisioning registers the box's *own* gateway. Two bugs found + fixed while
building it: (1) firstboot now **syncs the minted `gateway_token` into the
memoria-setup unit** (else the portal provisioned under the source box's token →
cameras on the wrong gateway + 401); (2) the portal now **serves HTTPS** (§7).
The golden-image stick also holds `2026-07-10-03-img-Jul-9-2026` = an IMAGE-A
(OptiPlex) backup. Field boxes need an **AP-capable Wi-Fi adapter** (MediaTek
MT7921 USB e.g. EDUP/Panda, or an internal Intel AX210) for the hotspot to broadcast.

**Tooling:** Clonezilla, restored/saved to a USB. On a Dell OptiPlex the one-time
boot-menu key is **F12**; a Lenovo ThinkCentre is also **F12** (BIOS = F1). On a
Lenovo you may need BIOS → **disable Secure Boot** + move the USB out of "Excluded
from boot order" + (if the stick won't show) enable **CSM / UEFI+Legacy**.

**⚠️ macOS can't write these FAT sticks** (a known macOS Sequoia bug mounts FAT32
read-only, worse with two USBs plugged in). So you **cannot** prep a code USB on
the Mac. Get new code onto a workbench box **over the network instead** (Wi-Fi +
`git pull`, or `curl` a bootstrap; the build session used a gist + `curl … | bash`),
or push it via `ssh` (base64 → the box; `/opt/memoria-gateway` is owned by
`potaton00b`, no sudo to write there). **Delete stale images** from the golden-image
stick inside Clonezilla (Linux, writes FAT fine): drop to the shell (end-menu
`cmd`, or Ctrl+Alt+F2) → `sudo rm -rf /home/partimag/<old-image-folder>`.

**Two kinds of image — do not confuse them:**
- **Plain clone (IMAGE-A):** a `savedisk` of a live box as-is. It carries that
  box's identity (token, machine-id, SSH keys, tunnel port, saved Wi-Fi) and has
  **no** `memoria-firstboot`. Flashing it makes a **twin**. Useful only as a
  backup/restore of that *same* box.
- **Template (IMAGE-B):** a box that was **generalized** with
  `deploy/prep-golden-image.sh` before capture. Every clone self-onboards.

**The per-box things that MUST differ between units** (all handled by firstboot):
machine-id, SSH host keys, `gateway_token` (in `cameras.json` **and synced into
the memoria-setup unit** so the portal provisions under it), the reverse-SSH
**tunnel port** (baked unit hardcodes 22001; backend assigns the real one, §13),
the portal **self-signed cert** (deleted by prep → regenerated per box), and the
saved **Wi-Fi** (deleted so the hotspot raises). Firmware **AC-recovery does NOT
travel in any image** — set it per box (Dell: sysfs, §10; Lenovo: BIOS → Power →
After Power Loss → On).

**Re-generalizing an already-onboarded box (to re-capture) can be done REMOTELY:**
`ssh` in, push any fixed files (base64), then run prep detached so it survives the
Wi-Fi drop + powers off: `sudo bash -c 'setsid bash -c "bash …/prep-golden-image.sh
>/tmp/prep.log 2>&1; poweroff" </dev/null >/dev/null 2>&1 &'`. Then you only do the
Clonezilla `savedisk` at the machine. (This is how `memoria-template-final` was made.)

**Build a template — never on the live OptiPlex (prep wipes its identity + Wi-Fi).
Use a disposable workbench box (e.g. the Lenovo), at its console (no ethernet
anywhere; Wi-Fi only):**
1. Flash the workbench box from any working image (IMAGE-A) — gives it a working
   OS + code base.
2. Bring it online at the console over Wi-Fi: `ip link` → find the Wi-Fi iface →
   `nmcli device wifi connect "Suburban Living" password "<WIFI_PASSWORD>"` →
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

## 13. Fleet SSH registry (which box is which — rock solid)

With many boxes all reverse-tunneling through the ONE relay, you must never guess
which port is which home. The **backend `gateways` table is the source of truth**:

- Column **`gateways.tunnel_port`** — a UNIQUE port the DB assigns (sequence
  `gateway_tunnel_port_seq`; migration `nemara-backend/scripts/apply_tunnel_registry_delta.sql`,
  applied to prod 2026-07-09). OptiPlex = `22001`; each new box auto-gets `22002…`.
  Never hashed/guessed → no collisions.
- **`GET /v1/gateway/roster`** returns `tunnel_port`. On the box, `publisher.py`
  writes it to `/opt/memoria-gateway/.tunnel-port`; `deploy/tunnel.sh` (the tunnel
  unit's ExecStart) reads that file and (re)connects autossh on it — so a box gets
  its port automatically after it's provisioned, no root, no restart.
- **Reach any box by name:** on the fleet Mac, `python3 scripts/gen-ssh-config.py`
  (needs `SUPABASE_MGMT_TOKEN`) reads the registry (port + caregiver email) and
  writes `~/.ssh/config.d/memoria-fleet` — one `Host memoria-<caregiver-email>`
  per box. Then `ssh memoria-jane-patel-co` (verified working off-network). Re-run
  it whenever you add/remove a box.
- **Who's online now:** on the relay, `ss -tlnp | grep 220` = ports currently
  bound = boxes with a live tunnel; cross-ref `last_seen_at` in `gateways`.

## 14. Code deployment — git, not rsync (no drift)

The box's `/opt/memoria-gateway` should be a **git checkout** of
`github.com/potaton00b/nemara-gateway`, so every box runs identical, versioned
code while keeping its own gitignored identity (`cameras.json`, `.provisioned`,
`.tunnel-port`, `wifi-connect/`).

- Convert a box: `deploy/git-bootstrap.sh` (uses the read-only **deploy key** at
  `~/.ssh/memoria_deploy`, added to the repo's GitHub deploy keys). `prep-golden-image.sh`
  builds this into the template, so fresh boxes are git-connected from birth.
- Deploy new code: `cd /opt/memoria-gateway && git pull && sudo systemctl restart memoria-gateway`.
  `git rev-parse HEAD` tells you exactly what a box runs.
- **The live OptiPlex is still an rsync'd folder (NOT git) — intentionally left
  alone.** Convert it deliberately with `git-bootstrap.sh` when you choose; until
  then keep using rsync (§5) for it only.
- Backend: the roster change is committed; **deploy it (`railway up`) before
  provisioning box #2** so prod actually returns `tunnel_port`. (Deployed 2026-07-10.)

## 15. Team access — the `gw` command + multi-person auth

`scripts/gw <caregiver-email> [cmd]` — SSH into any box **by account**, from anywhere:
```bash
gw jane@patel.co
gw jane@patel.co "journalctl -u memoria-gateway -n 50"
```
It resolves the account's `tunnel_port` from the registry and connects through the
relay — no per-box config, no remembering ports. Needs `SUPABASE_MGMT_TOKEN` + the
`memoria-relay` hop in `~/.ssh/config`. Put it on PATH: `ln -s "$PWD/scripts/gw" /usr/local/bin/gw`.
Verified 2026-07-10 (`gw jane@patel.co` → port 22001 → box).

**The relay is the single access-control choke point** — everyone reaches every box
*through* it, so grant/revoke access there, in one place:
- **Person → relay:** each teammate's OWN public key in the relay's authorized_keys.
  Their own key = revocable per person; remove it → they lose the **entire** fleet
  at once (they can't hop, so they can't reach any box).
- **relay → box:** boxes trust ONE shared **fleet key** (baked into the template).
  Access is already gated at the relay, so box keys don't need to be per-person.
- **Add a teammate (e.g. cofounder):** they generate an SSH key → add their public
  key to the relay → give them the registry token + the `gw` script. Done — `gw
  <account>` works for them too, from their own machine.
- **Today** the box hop still uses the password (`<GATEWAY_PASSWORD>`). Installing the fleet
  key on boxes (automatic in the template; a one-time append on the OptiPlex) makes
  `gw` fully passwordless. The OptiPlex is intentionally left on password until you
  choose to upgrade it.

---

## Credential provisioning on the box (2026-08-03)

Nothing secret lives in this repo or in `cameras.json` any more. The box gets
its credentials from one file, written at provisioning time:

```bash
sudo install -m 600 -o potaton00b -g potaton00b /dev/null /opt/memoria-gateway/.env
sudo -u potaton00b tee /opt/memoria-gateway/.env >/dev/null <<'EOF'
GATEWAY_TOKEN=<the box's site token>
TAPO_PASSWORD=<camera RTSP password>
EOF
```

`chmod 600`, owned by the service user, and **never** committed — the repo's
`.gitignore` covers `.env` and `.env.*`.

What is deliberately NOT in that file:

| Not present | Why |
|---|---|
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | The box no longer signs its own tokens. `Publisher._mint_token` asks the backend (`POST /v1/livekit/token`, `role="gateway"`) using `GATEWAY_TOKEN`. The LiveKit secret exists only in the backend's environment, so rotating it is one change instead of a fleet update. |
| Wi-Fi password | Held by NetworkManager's own connection store on the box, not by us. |

**Reading them in code:** no fallbacks. `setup_server.py` used to do
`q.get("pass") or ["qwerty123"]`, and that default is exactly how the camera
password entered the source tree and reached `origin/main`. A missing
credential must raise, not quietly work.

## SSH: cutting over to key-only auth

`1c8421a` bakes the ops key into every box, so half of this is done. The
remaining steps need the box **online** — run them in this order, because
getting it wrong locks you out of hardware sitting in someone's home:

1. Confirm key auth works: `ssh -o PasswordAuthentication=no memoria-box-01 true`
   must succeed. **Do not proceed until it does.**
2. Confirm passwordless sudo for the service commands the runbook uses.
3. Only then: `PasswordAuthentication no` in `sshd_config`, `sudo systemctl
   reload ssh`.
4. Keep a second session open while doing step 3, so a mistake is recoverable.

Until step 3 lands, the account password stays load-bearing — which is why it
was rotated rather than simply removed from this doc.

## VERIFY WHEN THE BOX COMES BACK ONLINE

Written and unit-checked with the hardware offline, so none of this is
confirmed against a real box. Verify in this order:

- [ ] **Backend token minting.** Box publishes at all — `Publisher._mint_token`
      gets a 200 and the camera appears as `cam_*` in the room. A 401 means the
      camera's `gateway_id` does not match the box's site token.
- [ ] **Unowned cameras.** Any camera with `gateway_id IS NULL` will now FAIL to
      publish (it still shows on the roster). That is intentional, but if the
      fleet has un-backfilled rows they need a `gateway_id` set first.
- [ ] **Reconnect path.** Kill the backend briefly: the publisher should retry
      with its existing backoff, not crash and not fall back to self-signing.
- [ ] **Setup server.** Provision a camera through the web form — the password
      field is no longer pre-filled, and submitting it blank must 400 rather
      than silently using a default.
- [ ] **Trueview cameras.** Their password is legitimately empty; confirm an
      explicit empty `pass=` still works (absent vs empty are different).
- [ ] **Rotated credentials.** The box's `.env` carries the NEW `GATEWAY_TOKEN`
      and `TAPO_PASSWORD`, and Wi-Fi reconnects on the new password.
- [ ] Integration tests `test/integration/test_livekit_token_gateway.py` in
      nemara-backend — they could not run locally (the whole integration suite
      fails to reach its fixtures in this environment).

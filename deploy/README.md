# Gateway deployment

Turning a mini-PC into a Memoria gateway, from most-manual to fully-automated.

## Files
| File | What it does |
|---|---|
| `setup.sh` | One-shot provisioner: fresh Ubuntu → full gateway (services + camera portal + Wi-Fi portal). Idempotent. Run: `sudo deploy/setup.sh` |
| `memoria-gateway.service` | The always-on gateway systemd unit (also written by `setup.sh`). |
| `wifi-connect-ui/` | Vendored, pre-built web UI for the Wi-Fi onboarding portal (balena wifi-connect ships only the binary). |
| `revert-wifi.sh` | **Console recovery.** Undo the Wi-Fi-portal/NetworkManager switch and rejoin the known Wi-Fi on plain networkd. Run at the monitor if a portal test leaves the box offline. |
| `autoinstall/` | Ubuntu unattended-install (cloud-init) — build a gateway hands-off at the bench. |

## Three ways to build a box

**1. Manual (proven).** Install Ubuntu, then `sudo deploy/setup.sh`. One command.

**2. Unattended (`autoinstall/`).** Ubuntu installs itself + runs `setup.sh` on first boot. **Needs ethernet during the build** (installer + apt/pip need internet; field units never do — they use the Wi-Fi portal). This is how you build a *master* or a ready-to-ship unit. ⚠️ Not yet validated end-to-end — do one test install first.

**3. Golden image.** Build one box (via #1 or #2), generalize it (the first-boot step blanks identity + cameras), then clone its disk to every other box with Clonezilla/dd.

## Config
`setup.sh`'s top block sets `BACKEND_URL` / `LIVEKIT_URL` etc. Defaults point at the **sandbox** (`10.0.0.20`). For production, pass a stable domain:
```
sudo BACKEND_URL=https://api.memoria.care LIVEKIT_URL=wss://... deploy/setup.sh
```

## Per-site (never baked in)
- **Wi-Fi** → phone captive portal (`Memoria-Setup-XXXX` hotspot on first boot).
- **Cameras** → the setup portal at `http://<box>:8088`.

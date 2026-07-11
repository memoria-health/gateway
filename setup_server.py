#!/usr/bin/env python3
"""Gateway setup agent — the installer UX.

A tiny local web app the installer opens on the mini-PC (or from a laptop on the
same wifi). It replaces hand-editing cameras.json + SQL with:

  1. DISCOVER cameras on the network (subnet scan)
  2. LOG IN to the facility's Memoria account (Supabase Auth -> JWT)
  3. NAME + assign each camera, and PROVISION it (backend gateway-provision ->
     a registered camera row + an ingest token)
  4. WRITE cameras.json automatically, so `run.sh` picks them up

It talks ONLY to the backend API + Supabase Auth (login). It never touches the
database directly. Run:  ./.venv/bin/python setup_server.py   (open :8088)
"""
import asyncio
import json
import os
import re
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import discover

HERE = os.path.dirname(os.path.abspath(__file__))
CAMERAS_JSON = os.path.join(HERE, "cameras.json")

# Sandbox endpoints (match cameras.json). In prod these point at dev/prod.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54351")
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY", "sb_publishable_ACJWlzQHlZjBrEguHvfOxg_3BJgxAaH")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8001")
LIVEKIT = {"livekit_url": "ws://localhost:7880",
           "livekit_api_key": "devkey", "livekit_api_secret": "secret"}
# The ONE stable site credential the gateway uses to poll its roster.
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "gw-site-token-2026")
# In a finished installer this comes from a patient-picker (GET the account's
# patients); for the sim we default to the demo patient.
DEFAULT_PATIENT = os.environ.get(
    "SETUP_PATIENT_ID", "6af51654-76f3-41c5-bd7e-00119c6183cf")

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memoria Gateway Setup</title>
<style>
 body{font-family:-apple-system,Segoe UI,sans-serif;max-width:720px;margin:40px auto;
   padding:0 20px;color:#141414;background:#f3efe7}
 h1{font-weight:700} h2{margin-top:32px;font-size:18px}
 .card{background:#fff;border:1px solid #e2dccf;border-radius:12px;padding:16px;margin:12px 0}
 input{padding:8px;border:1px solid #cfc7b6;border-radius:8px;font-size:14px;margin:2px}
 button{background:#141414;color:#fff;border:0;border-radius:8px;padding:10px 18px;
   font-size:14px;cursor:pointer} button:disabled{opacity:.4}
 .cam{display:flex;gap:12px;align-items:flex-start;padding:12px 0;
   border-bottom:1px solid #eee}
 .caminfo{display:flex;flex-direction:column;gap:6px;min-width:0}
 .thumb{width:200px;height:112px;object-fit:cover;border-radius:8px;
   background:#e2dccf;border:1px solid #cfc7b6;flex:none}
 .thumb.thumberr{object-fit:contain;padding:8px;opacity:.5}
 .mini{background:#e2dccf;color:#141414;padding:4px 10px;font-size:12px}
 .badge{background:#3a7d44;color:#fff;font-size:11px;padding:2px 8px;
   border-radius:999px;margin-left:6px;vertical-align:middle}
 .muted{color:#8a8170;font-size:13px} pre{background:#141414;color:#9fe6a0;
   padding:12px;border-radius:8px;overflow:auto;font-size:12px}
 code{background:#eee;padding:1px 5px;border-radius:4px}
</style></head><body>
<h1>Memoria Gateway Setup</h1>
<p class="muted">Plug in the mini-PC, then: discover cameras → <b>look at each thumbnail to tell which room is which</b> → name them → enter the caregiver's email → provision. No password.</p>

<h2>1 &middot; Discover cameras</h2>
<div class="card">
  <button onclick="discover()" id="dbtn">Scan the network</button>
  <span class="muted" id="dstat"></span>
  <div id="cams"></div>
</div>

<h2>2 &middot; Caregiver email</h2>
<div class="card">
  <p class="muted">Which caregiver account is this home for? <b>Email only — no password.</b></p>
  <input id="email" placeholder="caregiver@email.com" value="" size="26">
</div>

<h2>3 &middot; Provision</h2>
<div class="card">
  <button onclick="provision()" id="pbtn" disabled>Provision selected cameras</button>
  <span class="muted" id="pstat"></span>
  <pre id="out" style="display:none"></pre>
</div>

<script>
let found = [];
async function discover(){
  document.getElementById('dstat').textContent = 'scanning…';
  document.getElementById('dbtn').disabled = true;
  const r = await fetch('/api/discover',{method:'POST'}); const j = await r.json();
  found = j.cameras || [];
  const box = document.getElementById('cams'); box.innerHTML='';
  if(!found.length){ box.innerHTML='<p class="muted">No cameras found on this subnet.</p>'; }
  found.forEach((c,i)=>{
    const badge = c.already ? ' <span class="badge">already added</span>' : '';
    box.insertAdjacentHTML('beforeend',
      `<div class="cam">
         <input type="checkbox" id="sel${i}" ${c.already?'':'checked'} style="margin-top:48px">
         <img id="thumb${i}" class="thumb" alt="loading preview…"
              src="/api/snapshot?ip=${encodeURIComponent(c.ip)}&user=camera1&pass=qwerty123"
              onerror="this.classList.add('thumberr');this.alt='no preview — check user/pass, then ↻'">
         <div class="caminfo">
           <div><b>${c.ip}</b> <span class="muted">RTSP${c.onvif?' + ONVIF':''}</span>${badge}</div>
           <div>name <input id="name${i}" value="Room ${i+1}" size="12"></div>
           <div>user <input id="user${i}" value="camera1" size="9">
                pass <input id="pass${i}" type="text" value="qwerty123" size="9">
                <button type="button" class="mini" onclick="repreview(${i},'${c.ip}')">↻ preview</button></div>
         </div>
       </div>`);
  });
  document.getElementById('dstat').textContent = found.length+' found';
  document.getElementById('dbtn').disabled = false;
  document.getElementById('pbtn').disabled = !found.length;
}
function repreview(i, ip){
  const u=document.getElementById('user'+i).value;
  const p=document.getElementById('pass'+i).value;
  const img=document.getElementById('thumb'+i);
  img.classList.remove('thumberr'); img.alt='loading preview…';
  img.src='/api/snapshot?ip='+encodeURIComponent(ip)+'&user='+encodeURIComponent(u)
         +'&pass='+encodeURIComponent(p)+'&t='+Date.now();
}
async function provision(){
  document.getElementById('pstat').textContent = 'provisioning…';
  document.getElementById('pbtn').disabled = true;
  const cams = [];
  found.forEach((c,i)=>{ if(document.getElementById('sel'+i).checked){
    cams.push({ip:c.ip, mac:c.mac, name:document.getElementById('name'+i).value,
      rtsp_user:document.getElementById('user'+i).value,
      rtsp_pass:document.getElementById('pass'+i).value, icon_name:'tv'}); }});
  const body = {email:document.getElementById('email').value, cameras:cams};
  const r = await fetch('/api/provision',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const j = await r.json();
  document.getElementById('out').style.display='block';
  document.getElementById('out').textContent = JSON.stringify(j,null,2);
  const sk = (j.skipped&&j.skipped.length) ? (', '+j.skipped.length+' already added (skipped)') : '';
  document.getElementById('pstat').textContent = j.ok ? ('done — '+j.provisioned+' provisioned'+sk) : ('error: '+(j.error||''));
  document.getElementById('pbtn').disabled = false;
}
</script></body></html>"""


def _post_json(url, body, headers=None):
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode())


def login(email, password):
    r = _post_json(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                   {"email": email, "password": password},
                   {"apikey": SUPABASE_ANON_KEY})
    return r["access_token"]


def provision_one(jwt, patient_id, name, icon_name):
    return _post_json(
        f"{BACKEND_URL}/v1/patients/{patient_id}/cameras/gateway-provision",
        {"room": name, "icon_name": icon_name},
        {"Authorization": f"Bearer {jwt}"})


def provision_via_email(email, name, icon_name):
    """Provision a camera with EMAIL ONLY — no password. The box's OWN unique
    token (Bearer) ties it to the caregiver identified by `email`; the backend
    creates the camera under ALL that caregiver's patients and returns one entry
    per patient (all pointing at this one physical camera)."""
    return _post_json(
        f"{BACKEND_URL}/v1/gateway/provision-camera",
        {"email": email, "room": name, "icon_name": icon_name},
        {"Authorization": f"Bearer {GATEWAY_TOKEN}"})


def grab_snapshot(ip, user, pw):
    """One small JPEG frame from a camera's RTSP so the installer can SEE which
    physical room each (identical) Tapo points at — the only reliable way to tell
    them apart, since IP/hardware don't say which room. 200px wide, ~5s cap. One
    short-lived RTSP connection; fine alongside the gateway's own single pull."""
    rtsp = f"rtsp://{user}:{pw}@{ip}:554/stream1"
    out = subprocess.run(
        # No -rw_timeout: this ffmpeg build rejects it as an input option (the
        # 12s subprocess cap is the backstop). -rtsp_transport tcp before -i.
        ["ffmpeg", "-nostdin", "-rtsp_transport", "tcp",
         "-i", rtsp, "-frames:v", "1", "-vf", "scale=320:-1", "-q:v", "6",
         "-f", "image2", "pipe:1"],
        capture_output=True, timeout=12,
    )
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError((out.stderr.decode()[-160:] or "no frame").strip())
    return out.stdout


def _rtsp_ip(rtsp):
    m = re.search(r"@([^:/@]+)", rtsp or "")
    return m.group(1) if m else None


def provisioned_keys():
    """(ips, macs) of the physical cameras ALREADY mapped in cameras.json. A
    camera counts as already-provisioned if its IP or MAC matches an existing
    entry — so we never create duplicate backend rows for the same camera."""
    ips, macs = set(), set()
    if os.path.exists(CAMERAS_JSON):
        try:
            cfg = json.load(open(CAMERAS_JSON))
            for c in cfg.get("cameras", []):
                ip = _rtsp_ip(c.get("rtsp"))
                if ip:
                    ips.add(ip)
                if c.get("mac"):
                    macs.add(c["mac"])
        except Exception:  # noqa: BLE001
            pass
    return ips, macs


def write_cameras_json(entries):
    cfg = {**LIVEKIT, "backend_url": BACKEND_URL, "poll_seconds": 3,
           "gateway_token": GATEWAY_TOKEN, "cameras": []}
    if os.path.exists(CAMERAS_JSON):
        try:
            cfg = json.load(open(CAMERAS_JSON))
        except Exception:  # noqa: BLE001
            pass
    cfg.setdefault("cameras", [])
    existing = {c["livekit_identity"] for c in cfg["cameras"]}
    for e in entries:
        if e["livekit_identity"] not in existing:
            cfg["cameras"].append(e)
    json.dump(cfg, open(CAMERAS_JSON, "w"), indent=2)
    return len(cfg["cameras"])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, bytes):
            b = body
        elif ctype == "application/json":
            b = json.dumps(body).encode()
        else:
            b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE, "text/html")
        elif parsed.path == "/api/snapshot":
            q = parse_qs(parsed.query)
            ip = (q.get("ip") or [""])[0]
            user = (q.get("user") or ["camera1"])[0]
            pw = (q.get("pass") or ["qwerty123"])[0]
            try:
                self._send(200, grab_snapshot(ip, user, pw), "image/jpeg")
            except Exception as e:  # noqa: BLE001
                self._send(502, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(ln) if ln else b"{}"
        try:
            body = json.loads(raw or b"{}")
            if self.path == "/api/discover":
                # Flag cameras already provisioned on THIS box so the UI can
                # pre-uncheck them (and the provision handler skips them too).
                ips, macs = provisioned_keys()
                cams = asyncio.run(discover.discover())
                for c in cams:
                    c["already"] = (c.get("ip") in ips) or bool(
                        c.get("mac") and c["mac"] in macs)
                self._send(200, {"cameras": cams})
            elif self.path == "/api/provision":
                email = (body.get("email") or "").strip()
                if not email:
                    self._send(400, {"error": "email is required"})
                    return
                ips, macs = provisioned_keys()
                entries, skipped = [], []
                for cam in body["cameras"]:
                    # IDEMPOTENT: never re-provision a physical camera already
                    # mapped on this box (same IP or MAC). Re-provisioning would
                    # create a whole second set of duplicate camera rows (one per
                    # patient) all pointing at the same Tapo.
                    if cam["ip"] in ips or (cam.get("mac") and cam["mac"] in macs):
                        skipped.append(cam["ip"])
                        continue
                    rtsp = (f"rtsp://{cam['rtsp_user']}:{cam['rtsp_pass']}"
                            f"@{cam['ip']}:554/stream1")
                    pr = provision_via_email(email, cam["name"],
                                             cam.get("icon_name", "tv"))
                    # One physical camera -> one identity per the caregiver's
                    # patients; ALL map to the SAME local RTSP + MAC.
                    for c in pr.get("cameras", []):
                        entries.append({
                            "livekit_identity": c["livekit_identity"],
                            "rtsp": rtsp,
                            "mac": cam.get("mac"),
                        })
                total = write_cameras_json(entries)
                self._send(200, {"ok": True, "provisioned": len(entries),
                                 "skipped": skipped, "total_cameras": total,
                                 "entries": entries})
            else:
                self._send(404, {"error": "not found"})
        except urllib.error.HTTPError as e:  # noqa: PERF203
            self._send(500, {"error": f"{e.code} {e.read().decode()[:200]}"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})


_CERT = os.path.join(HERE, "portal-cert.pem")
_KEY = os.path.join(HERE, "portal-key.pem")


def _ensure_cert():
    """Self-signed cert so the portal ALSO answers HTTPS. Browsers (esp. Safari)
    auto-upgrade http->https; with no TLS listener that just HANGS. With one, the
    box answers and the installer taps through a one-time warning (you can't get a
    trusted cert for a `.local` name). Generated once on the box via openssl."""
    if os.path.exists(_CERT) and os.path.exists(_KEY):
        return
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", _KEY, "-out", _CERT, "-days", "3650",
         "-subj", "/CN=memoria-gateway.local",
         "-addext", "subjectAltName=DNS:memoria-gateway.local,DNS:*.local,IP:127.0.0.1"],
        check=True, capture_output=True)


class DualStackServer(ThreadingHTTPServer):
    """Listen on IPv6 AND IPv4 (dual-stack). avahi/Bonjour advertises the box's
    AAAA (IPv6) records on any IPv6-enabled home network, so browsers resolve
    `memoria-gateway.local` to an IPv6 address and connect over IPv6. An IPv4-only
    bind (0.0.0.0) REFUSES those (`ERR_CONNECTION_REFUSED`) even though the name
    resolves fine — the exact failure seen in the field. Binding `::` with
    IPV6_V6ONLY off accepts whichever protocol the client (phone) picks.

    Also serves HTTP *and* HTTPS on the SAME port: we peek the first byte of each
    connection (0x16 == TLS handshake) and wrap it in TLS if so. So
    `memoria-gateway.local:8088` works whether the browser sends http or (silently
    upgraded) https — no more 'stuck loading' on Safari."""
    address_family = socket.AF_INET6
    ssl_ctx = None

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def get_request(self):
        sock, addr = self.socket.accept()
        try:
            if self.ssl_ctx and sock.recv(1, socket.MSG_PEEK) == b"\x16":
                sock = self.ssl_ctx.wrap_socket(sock, server_side=True)
        except OSError:
            try:
                sock.close()
            finally:
                raise
        return sock, addr


def main():
    port = int(os.environ.get("SETUP_PORT", "8088"))
    try:
        _ensure_cert()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(_CERT, _KEY)
        DualStackServer.ssl_ctx = ctx
    except Exception as e:  # noqa: BLE001 — TLS must never block the portal
        print(f"[setup] TLS unavailable ({e}); serving HTTP only", flush=True)
    print(f"gateway setup agent -> http(s)://memoria-gateway.local:{port}", flush=True)
    DualStackServer(("::", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

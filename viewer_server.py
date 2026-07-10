#!/usr/bin/env python3
"""Tiny local viewer for the gateway spike.

Mints a LiveKit *viewer* token, writes viewer/config.js, and serves viewer/ so
you can watch the published Tapo stream in a browser. Local only — talks to the
LIVEKIT_URL in .env (the local dev server).
"""
import http.server
import os
import re
import socketserver

from livekit import api


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = re.split(r"\s+#", v, 1)[0].strip()
        os.environ.setdefault(k.strip(), v)


load_dotenv()
URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")
ROOM = os.environ.get("LIVEKIT_ROOM", "gateway-spike")
PORT = int(os.environ.get("VIEWER_PORT", "8088"))

token = (api.AccessToken(KEY, SECRET)
         .with_identity("web-viewer")
         .with_grants(api.VideoGrants(room_join=True, room=ROOM))
         .to_jwt())

os.makedirs("viewer", exist_ok=True)
with open("viewer/config.js", "w") as f:
    f.write(f'window.LK_CONFIG = {{ url: "{URL}", token: "{token}", room: "{ROOM}" }};\n')

os.chdir("viewer")
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", PORT), http.server.SimpleHTTPRequestHandler) as httpd:
    print(f"viewer at http://localhost:{PORT}   (room={ROOM}, url={URL})", flush=True)
    httpd.serve_forever()

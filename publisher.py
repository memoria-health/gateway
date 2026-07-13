#!/usr/bin/env python3
"""nemara-gateway publisher — roster-driven, multi-camera, fan-out.

The BACKEND is the source of truth. The gateway holds ONE stable site token and
polls `GET /v1/gateway/roster`, which returns exactly the cameras it should run +
their live desired_state. A `RosterManager` reconciles the roster against the
hardware.

Physical cameras are DEDUPED by their RTSP stream. The caregiver model maps ONE
physical camera to one camera row PER patient (each row its own LiveKit
room/identity), so several roster entries can share one physical stream. The
gateway pulls each physical stream ONCE (a `CameraSource`) and fans the decoded
frames out to N `Publisher`s (one per patient/room). This is the whole point: a
single Tapo only serves ~2-3 concurrent RTSP clients, so pulling it once and
fanning out — instead of once per patient — is what lets a caregiver with several
patients share a camera without tripping the camera's client limit.

The local config (`cameras.json`) is only an `identity -> {rtsp, mac}` map — how
to reach each camera on the LAN (a facility-local detail the backend doesn't
hold). The roster decides WHICH cameras run + their desired_state. A camera runs
only if it's in BOTH the roster (authoritative) AND the local map (reachable).
Deleting a camera in the dashboard removes it from the roster, which detaches its
Publisher; when a physical stream has no publishers left, its pull stops.

Each CameraSource keeps the full resilience: RTSP probe+reconnect with backoff, a
frame watchdog (stalls that don't EOF), LiveKit reconnect, and MAC-based
re-discovery if a camera's IP drifts. The roster poll doubles as the heartbeat.
"""
import asyncio
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timezone

import numpy as np
from livekit import api, rtc

import discover

# The local LiveKit dev key ("devkey") is short; jwt warns it's below the 32-byte
# HMAC recommendation. Harmless in the sandbox — silence the per-token noise.
warnings.filterwarnings("ignore", message="The HMAC key")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def probe_stream(url):
    """Synchronous ffprobe; doubles as the connectivity check (raises if the
    camera is unreachable -> supervisor backs off / re-discovers)."""
    out = subprocess.run(
        # -rw_timeout (microseconds) makes ffprobe itself give up fast on an
        # unreachable camera instead of hanging; the 6s subprocess cap is the
        # backstop. Together they cut the "re-find an unplugged camera" delay
        # from ~15s to a few seconds. A healthy Tapo probes in ~1-2s.
        ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
         "-rw_timeout", "4000000",
         "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate",
         "-of", "csv=p=0:s=x", url],
        capture_output=True, text=True, timeout=6,
    )
    parts = out.stdout.strip().split("x")
    if len(parts) < 2:
        raise RuntimeError(f"probe failed: {out.stderr.strip() or 'no video stream'}")
    w, h = int(parts[0]), int(parts[1])
    fps = 15.0
    if len(parts) > 2 and "/" in parts[2]:
        num, den = parts[2].split("/")
        if float(num) > 0 and float(den) > 0:
            fps = float(num) / float(den)
    return w, h, fps


def build_ffmpeg_cmd(rtsp, fps, logo_path):
    out_fps = max(1, int(round(fps)))
    base = ["ffmpeg", "-use_wallclock_as_timestamps", "1",
            "-rtsp_transport", "tcp", "-i", rtsp]
    if os.path.exists(logo_path):
        cmd = base + ["-i", logo_path, "-filter_complex",
                      # Dark box sized to fully cover the Tapo watermark
                      # (bottom-left, ~245x60 on a 1080p frame), with the Memoria
                      # logo scaled up + centered inside it.
                      "[0:v]drawbox=x=0:y=ih-120:w=360:h=120:color=0x141414:t=fill[bg];"
                      "[1:v]scale=320:-1[logo];"
                      "[bg][logo]overlay=20:main_h-overlay_h-13[v]", "-map", "[v]"]
    else:
        cmd = base
    cmd += ["-vsync", "cfr", "-r", str(out_fps),
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    return cmd


# LiveKit audio wants PCM at a fixed rate. The Tapo emits G.711 A-law 8kHz mono;
# ffmpeg decodes + resamples up to 48kHz mono s16le for us.
_AUDIO_RATE = 48000
_AUDIO_CH = 1
_AUDIO_QUEUE_MS = 200   # small AudioSource buffer → tighter A/V sync (less lag)


def build_audio_ffmpeg_cmd(rtsp):
    """Second, AUDIO-ONLY RTSP pull. Decodes the Tapo's G.711 A-law 8kHz mono
    audio and resamples to 48kHz mono s16le PCM on stdout. It's a separate
    connection from the video pull, but G.711 is tiny and the camera tolerates
    the 2 concurrent clients. Low-delay flags + a causal soft-clip keep audio
    latency low so it stays closer in sync with video."""
    # The Tapo mic is quiet — boost ~5x. asoftclip (tanh) is a CAUSAL soft limiter
    # (no lookahead delay, unlike alimiter) so loud sounds round off instead of
    # hard-clipping, WITHOUT adding latency.
    return ["ffmpeg", "-nostdin", "-fflags", "nobuffer", "-flags", "low_delay",
            "-rtsp_transport", "tcp", "-i", rtsp,
            "-vn", "-map", "0:a", "-af", "volume=5,asoftclip=type=tanh",
            "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CH),
            "-f", "s16le", "-"]


def _rtsp_host(url):
    m = re.search(r"@([^:/@]+)", url)
    return m.group(1) if m else None


def _rtsp_with_host(url, host):
    return re.sub(r"(@)[^:/@]+", r"\g<1>" + host, url, count=1)


class Publisher:
    """One LiveKit destination for a physical camera. In the caregiver model a
    single physical camera has one Publisher PER patient (each its own LiveKit
    room/identity); every Publisher consumes the SAME decoded frames from the
    shared CameraSource, so the camera sees exactly ONE RTSP pull no matter how
    many patients view it. desired_state is per-publisher — each patient's
    dashboard connects/disconnects independently."""

    def __init__(self, *, identity, room_name, src):
        self.identity = identity
        self.room_name = room_name
        self.src = src              # back-ref: LiveKit creds + frame geometry
        self.desired = "disconnected"   # pushed by the manager each roster poll
        self.room = None
        self.lk_source = None       # rtc.VideoSource
        self.audio_source = None    # rtc.AudioSource
        self.publishing = False
        self._lk_dropped = False
        self.last_pub = 0.0

    async def connect(self):
        token = (api.AccessToken(self.src.api_key, self.src.api_secret)
                 .with_identity(self.identity).with_name(self.identity)
                 .with_grants(api.VideoGrants(room_join=True, room=self.room_name))
                 .to_jwt())
        room = rtc.Room()

        def _on_disconnected(*_a):
            if self.publishing:
                self._lk_dropped = True
                self.publishing = False
                log(f"[{self.identity}] LiveKit dropped — will rejoin")

        room.on("disconnected", _on_disconnected)
        await room.connect(self.src.lk_url, token)
        source = rtc.VideoSource(self.src.w, self.src.h)
        track = rtc.LocalVideoTrack.create_video_track("memoria", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA))
        # Audio track — fed by the CameraSource's shared audio pull (fanned out).
        audio_source = rtc.AudioSource(
            _AUDIO_RATE, _AUDIO_CH, queue_size_ms=_AUDIO_QUEUE_MS)
        audio_track = rtc.LocalAudioTrack.create_audio_track(
            "memoria-audio", audio_source)
        await room.local_participant.publish_track(
            audio_track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        self.room, self.lk_source, self.audio_source = room, source, audio_source
        self._lk_dropped = False
        self.publishing = True
        log(f"[{self.identity}] LiveKit CONNECTED + publishing (video+audio)")

    async def teardown(self):
        self.publishing = False
        room, self.room = self.room, None
        self.lk_source = self.audio_source = None
        if room is not None:
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001
                pass


class CameraSource:
    """One physical camera. Pulls RTSP ONCE (probe + ffmpeg + reader) and fans the
    decoded frames to every attached Publisher. Keeps the full resilience: RTSP
    probe+reconnect with backoff, a frame watchdog, LiveKit reconnect (per
    publisher), and MAC-based re-discovery if the camera's IP drifts. Publishers
    are attached/detached at runtime (roster changes) without disturbing the pull;
    the RosterManager stops the source once its last publisher is detached."""

    def __init__(self, *, key, rtsp, mac, g):
        self.key = key              # stable dedup key (original rtsp)
        self.rtsp = rtsp            # mutated in place by MAC re-discovery
        self.mac = mac
        self.lk_url = g["livekit_url"]
        self.api_key = g["livekit_api_key"]
        self.api_secret = g["livekit_api_secret"]
        self.logo = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "memoria.png")
        self.publishers = {}        # identity -> Publisher
        # runtime (geometry shared by all publishers)
        self.w = self.h = self.fps = None
        self.frame_us = 0
        self.frames = 0

    def attach(self, identity, room_name, desired):
        """Add/refresh a publisher. Cheap + synchronous — the control loop picks up
        a newly-attached publisher on its next tick and connects it to LiveKit."""
        pub = self.publishers.get(identity)
        if pub is None:
            pub = Publisher(identity=identity, room_name=room_name, src=self)
            self.publishers[identity] = pub
            log(f"[{identity}] attached to {_rtsp_host(self.rtsp)} "
                f"(desired={desired})")
        pub.desired = desired
        return pub

    async def detach(self, identity):
        pub = self.publishers.pop(identity, None)
        if pub is not None:
            await pub.teardown()
            log(f"[{identity}] detached from {_rtsp_host(self.rtsp)}")

    async def run(self):
        backoff = 1
        while True:
            start = time.monotonic()
            try:
                await self._run_pipeline()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # Reset backoff if the pipeline had been HEALTHY for a while, so
                # an isolated failure recovers fast (only rapid repeats ramp up).
                if time.monotonic() - start >= 30:
                    backoff = 1
                log(f"[{_rtsp_host(self.rtsp)}] pipeline down: {e!r} — "
                    f"retry in {backoff}s")
                # Any failure with a known MAC → try to relocate. Crucially, a
                # wrong/stale IP (camera moved to a new network) makes ffprobe
                # TIME OUT (TimeoutExpired), NOT raise "probe failed" — so we must
                # not gate on the error text or we'd skip the very case that needs
                # relocating. Re-discovery is a no-op if the IP hasn't drifted, so
                # it's safe to attempt on every failure.
                if self.mac:
                    await self._rediscover()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8)
            else:
                backoff = 1

    async def _rediscover(self):
        """Relocate this camera by MAC if its IP drifted (DHCP networks we don't
        control, e.g. facility wifi). No-op without a MAC (pinned-IP mesh). One
        source == one rediscover, even when many patients share the camera."""
        try:
            found = await discover.discover()
        except Exception as e:  # noqa: BLE001
            log(f"[{_rtsp_host(self.rtsp)}] rediscover scan failed: {e}")
            return
        for cam in found:
            if cam.get("mac") and cam["mac"] == self.mac:
                old = _rtsp_host(self.rtsp)
                if cam["ip"] != old:
                    self.rtsp = _rtsp_with_host(self.rtsp, cam["ip"])
                    log(f"[{_rtsp_host(self.key)}] RELOCATED by MAC {self.mac}: "
                        f"{old} -> {cam['ip']}")
                return
        log(f"[{_rtsp_host(self.rtsp)}] rediscover: MAC {self.mac} not on the "
            f"network yet")

    async def _run_pipeline(self):
        self.w, self.h, self.fps = await asyncio.to_thread(probe_stream, self.rtsp)
        self.frame_us = int(1_000_000 / self.fps)
        log(f"[{_rtsp_host(self.rtsp)}] stream {self.w}x{self.h}@{self.fps:.0f} — "
            f"pipeline up ({len(self.publishers)} publisher(s))")
        cmd = build_ffmpeg_cmd(self.rtsp, self.fps, self.logo)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        # Second ffmpeg: audio-only pull, fanned to publishers by _audio_loop.
        aproc = await asyncio.create_subprocess_exec(
            *build_audio_ffmpeg_cmd(self.rtsp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        reader = asyncio.create_task(self._reader_loop(proc))
        control = asyncio.create_task(self._control_loop())
        # Audio runs ALONGSIDE but does NOT gate the pipeline: an audio failure
        # logs + exits on its own, without tearing down video (video is priority).
        audio = asyncio.create_task(self._audio_loop(aproc))
        try:
            done, _ = await asyncio.wait(
                {reader, control}, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()  # re-raise so the supervisor restarts
        finally:
            reader.cancel()
            control.cancel()
            audio.cancel()
            await asyncio.gather(reader, control, audio, return_exceptions=True)
            proc.kill()
            aproc.kill()
            for pub in list(self.publishers.values()):
                await pub.teardown()

    async def _audio_loop(self, proc):
        """Read the audio-only ffmpeg's PCM (48kHz mono s16le) in 10ms frames and
        fan each to every currently-publishing patient's LiveKit audio source —
        the audio analogue of _reader_loop. On failure it logs + returns (does NOT
        raise), so a flaky audio pull never disturbs the video pipeline.

        A/V SYNC (fix 2026-07-13): audio must NEVER hold a backlog. capture_frame
        paces playout at real time, so any bytes that pile up in the pipe
        (startup skew between the two RTSP sessions, event-loop stalls) become a
        PERMANENT constant audio delay — the observed 2-3s lip-sync lag on the
        live feed. Track consumed audio-time against wall-clock; when it falls
        behind, DROP chunks (consume without forwarding) until caught up — a
        one-time audible skip instead of a forever lag."""
        samples = _AUDIO_RATE // 100          # 480 samples = 10ms
        chunk = samples * 2 * _AUDIO_CH       # 960 bytes (s16le mono)
        START_DROP_S = 0.35  # begin dropping when audio trails wall-clock this far
        STOP_DROP_S = 0.05   # drain down to here (hysteresis avoids flapping)
        t0 = None            # wall-clock at first chunk
        fwd_s = 0.0          # seconds of audio consumed (forwarded OR dropped)
        dropping = False
        drop_start_s = 0.0
        try:
            while True:
                buf = await asyncio.wait_for(
                    proc.stdout.readexactly(chunk), timeout=8.0)
                now = time.monotonic()
                if t0 is None:
                    t0 = now
                behind = (now - t0) - fwd_s
                fwd_s += 0.010
                if not dropping and behind > START_DROP_S:
                    dropping = True
                    drop_start_s = fwd_s
                if dropping:
                    if behind > STOP_DROP_S:
                        continue          # drop this chunk — drains the backlog
                    dropping = False
                    log(f"[{_rtsp_host(self.rtsp)}] audio: dropped "
                        f"{fwd_s - drop_start_s:.1f}s backlog to restore A/V sync")
                frame = rtc.AudioFrame(buf, _AUDIO_RATE, _AUDIO_CH, samples)
                for pub in list(self.publishers.values()):
                    if pub.publishing and pub.audio_source is not None:
                        try:
                            await pub.audio_source.capture_frame(frame)
                        except Exception:  # noqa: BLE001
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"[{_rtsp_host(self.rtsp)}] audio stopped ({e!r}) — video "
                f"unaffected; recovers on next pipeline restart")

    async def _control_loop(self):
        """Match EACH publisher's LiveKit connection to its desired_state. ffmpeg
        keeps running either way, so a reconnect has frames READY (no green flash);
        only the LiveKit publish follows desired_state. One loop covers all
        publishers — a publisher attached/detached mid-run is handled on the next
        tick. Connects only once the stream geometry (w/h) is known."""
        while True:
            if self.w is not None:
                for ident, pub in list(self.publishers.items()):
                    if pub.desired == "connected":
                        if pub.room is None or pub._lk_dropped:
                            if pub.room is not None:
                                await pub.teardown()
                            try:
                                await pub.connect()
                            except Exception as e:  # noqa: BLE001
                                log(f"[{ident}] LiveKit connect failed ({e}) — retry")
                    else:
                        if pub.room is not None:
                            await pub.teardown()
                            log(f"[{ident}] disconnected")
            # Check desired_state often so a dashboard connect/disconnect (pushed
            # by the manager's roster poll) is acted on in ~0.5s, not a full cycle.
            await asyncio.sleep(0.5)

    async def _reader_loop(self, proc):
        fsize = self.w * self.h * 3 // 2
        prev = None
        motion_until = 0.0
        # Motion = fraction of sampled pixels that changed a LOT. Robust to the
        # Tapo's heavy sensor noise (a small diff spread over the whole frame,
        # which mean-SAD conflated with a walking person). PIXEL_DELTA rejects
        # noise (per-pixel ~1-5); MOTION_FRAC is how much of the frame must move.
        PIXEL_DELTA = 30
        MOTION_FRAC = 0.0025  # sensitive: idle >30 floor ~0.2%; false wakes are
                              # cheap (worker re-checks for people, sleeps if none)
        MOTION_HOLD_S = 4.0
        STALL_TIMEOUT = 4.0   # no frame this long = dead stream (unplug/wifi)
        while True:
            try:
                buf = await asyncio.wait_for(
                    proc.stdout.readexactly(fsize), timeout=STALL_TIMEOUT)
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    f"stream stalled (no frame in {STALL_TIMEOUT:.0f}s) — "
                    f"camera/network down") from e
            self.frames += 1
            now = time.monotonic()
            # Motion is computed ONCE per decoded frame and shared by every
            # publisher — it's a property of the physical camera, not the viewer.
            # Motion = fraction of sampled pixels that changed a LOT (>PIXEL_DELTA).
            # Robust to the Tapo's compression noise (a small change spread over the
            # whole frame) — real motion is a big change in a cluster. Any motion
            # holds `in_motion` for MOTION_HOLD_S so the detector wakes + re-checks.
            # Sample every 397th pixel of the Y (luma) plane and compare to the
            # previous frame with numpy (vectorized C, releases the GIL). The old
            # pure-Python `sum(1 for a,b in zip(...))` ran ~1-3ms/frame INLINE on
            # the shared event loop (×cameras×fps), blocking the audio task and
            # making audio lag video. `.copy()` keeps only the ~5KB sample, not a
            # view into the 3MB frame buffer.
            sample = np.frombuffer(
                buf, dtype=np.uint8, count=self.w * self.h)[::397].copy()
            if prev is not None and sample.shape == prev.shape:
                changed = int(np.count_nonzero(
                    np.abs(sample.astype(np.int16) - prev.astype(np.int16))
                    > PIXEL_DELTA))
                if changed / sample.size > MOTION_FRAC:
                    motion_until = now + MOTION_HOLD_S
            prev = sample
            in_motion = now < motion_until
            # Fan the SAME frame out to every currently-publishing patient room.
            frame = rtc.VideoFrame(self.w, self.h, rtc.VideoBufferType.I420, buf)
            ts = self.frames * self.frame_us
            for pub in list(self.publishers.values()):
                if pub.publishing and pub.lk_source is not None:
                    pub.lk_source.capture_frame(frame, timestamp_us=ts)
                    if pub.room is not None and now - pub.last_pub >= 1.0:
                        try:
                            await pub.room.local_participant.publish_data(
                                json.dumps({"type": "motion_state",
                                            "in_motion": in_motion}),
                                topic="motion_state")
                        except Exception:  # noqa: BLE001
                            pass
                        pub.last_pub = now


class RosterManager:
    """Polls the backend roster (source of truth) and reconciles CameraSources +
    their Publishers. Roster entries are DEDUPED by physical stream (rtsp): N
    patients sharing one physical camera => ONE CameraSource pulling RTSP once,
    fanned out to N Publishers. Spawn a source on first appearance, detach a
    publisher on delete, stop a source when its last publisher goes, push
    desired_state each poll. The poll itself is the gateway's heartbeat."""

    def __init__(self, g):
        self.g = g
        self.token = g["gateway_token"]
        self.roster_url = f"{g['backend_url']}/v1/gateway/roster"
        self.wait_url = f"{g['backend_url']}/v1/gateway/wait"
        self.poll_s = g["poll_seconds"]
        self.local = {c["livekit_identity"]: c for c in g.get("cameras", [])}
        self.sources = {}   # source_key (rtsp) -> (CameraSource, task)
        self._warned = set()
        self._fingerprint = ""
        self._lock = asyncio.Lock()

    @staticmethod
    def _fp(cameras):
        return "|".join(f"{c['livekit_identity']}:{c['desired_state']}"
                        for c in sorted(cameras, key=lambda c: c["livekit_identity"]))

    def _fetch_roster(self):
        req = urllib.request.Request(
            self.roster_url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        self._apply_tunnel_port(data.get("tunnel_port"))
        return data.get("cameras", [])

    def _apply_tunnel_port(self, port):
        """Persist the DB-assigned reverse-SSH port to .tunnel-port when it
        changes. `deploy/tunnel.sh` watches that file and (re)connects autossh on
        the new port — so a freshly-provisioned box gets its own unique off-LAN
        port with NO root and NO restart. Best-effort: must never disturb the
        publisher (the backend may be old and omit the field, hence the guard)."""
        if not port:
            return
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            pf = os.path.join(here, ".tunnel-port")
            cur = open(pf).read().strip() if os.path.exists(pf) else None
            if cur != str(port):
                with open(pf, "w") as f:
                    f.write(str(port))
                log(f"tunnel port assigned by backend: {port} (was {cur or 'unset'})")
        except Exception:  # noqa: BLE001
            pass

    def _fetch_wait(self, since):
        url = f"{self.wait_url}?since={urllib.parse.quote(since)}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=35) as r:
            return json.loads(r.read().decode())

    def _reload_local(self):
        """Re-read the local identity->{rtsp,mac} map each poll so a camera the
        setup agent just provisioned (it rewrites cameras.json) is picked up with
        NO gateway restart. Keep the current map on a parse error (mid-write)."""
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            cfg = json.load(open(os.path.join(here, "cameras.json")))
            self.local = {c["livekit_identity"]: c for c in cfg.get("cameras", [])}
        except Exception:  # noqa: BLE001
            pass

    async def _apply(self, cameras):
        async with self._lock:
            self._reload_local()
            await self._reconcile(cameras)
            self._fingerprint = self._fp(cameras)

    async def _poll_loop(self):
        """Steady fallback: catches adds/deletes, stamps liveness, and covers any
        gap if the long-poll connection drops."""
        while True:
            try:
                roster = await asyncio.to_thread(self._fetch_roster)
            except Exception as e:  # noqa: BLE001
                log(f"roster poll failed ({e}) — keeping current set")
                await asyncio.sleep(self.poll_s)
                continue
            await self._apply(roster)
            await asyncio.sleep(self.poll_s)

    async def _wait_loop(self):
        """Fast path: hold ONE long-poll open; the backend answers the instant a
        connect/disconnect (or add/delete) happens (~0.4s) instead of waiting for
        the poll. Re-issues on every response/error; the poll_loop is the net."""
        while True:
            try:
                resp = await asyncio.to_thread(self._fetch_wait, self._fingerprint)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(1)   # re-open shortly; poll_loop covers the gap
                continue
            if resp.get("changed"):
                await self._apply(resp.get("cameras", []))

    async def run(self):
        log(f"gateway up: roster (poll + long-poll) -> {self.roster_url} "
            f"({len(self.local)} camera(s) mapped locally)")
        await asyncio.gather(self._poll_loop(), self._wait_loop())

    async def _reconcile(self, roster):
        # Join roster (authoritative: which cameras + desired_state) with local
        # (reachable: rtsp/mac), then GROUP by physical stream so one camera is
        # pulled once and fanned out to every patient sharing it.
        groups = {}   # key(rtsp) -> {"mac": .., "pubs": {identity: (room, desired)}}
        for cam in roster:
            ident = cam["livekit_identity"]
            lm = self.local.get(ident)
            if lm is None:
                if ident not in self._warned:
                    log(f"[{ident}] in roster but no local RTSP mapping — "
                        f"skipping (provision it on THIS gateway)")
                    self._warned.add(ident)
                continue
            key = lm["rtsp"]
            grp = groups.setdefault(key, {"mac": lm.get("mac"), "pubs": {}})
            grp["pubs"][ident] = (cam["livekit_room"], cam["desired_state"])

        # Stop sources whose physical camera has no roster cameras left at all
        # (every sharing patient deleted in the dashboard).
        for key in list(self.sources):
            if key not in groups:
                src, task = self.sources.pop(key)
                task.cancel()
                for ident in list(src.publishers):
                    self._warned.discard(ident)
                log(f"[{_rtsp_host(key)}] no cameras left — stopping RTSP pull")

        # Start / update the source for each physical camera.
        for key, grp in groups.items():
            if key not in self.sources:
                src = CameraSource(key=key, rtsp=key, mac=grp["mac"], g=self.g)
                for ident, (room, desired) in grp["pubs"].items():
                    src.attach(ident, room, desired)
                task = asyncio.create_task(src.run())
                self.sources[key] = (src, task)
                log(f"[{_rtsp_host(key)}] source started — 1 RTSP pull, "
                    f"{len(grp['pubs'])} publisher(s)")
            else:
                src, _ = self.sources[key]
                # attach new / refresh desired_state for wanted publishers
                for ident, (room, desired) in grp["pubs"].items():
                    src.attach(ident, room, desired)
                # detach publishers this physical camera no longer serves
                for ident in list(src.publishers):
                    if ident not in grp["pubs"]:
                        await src.detach(ident)


def load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(here, "cameras.json")))
    cfg.setdefault("poll_seconds", 3)
    return cfg


async def main():
    cfg = load_config()
    await RosterManager(cfg).run()


if __name__ == "__main__":
    asyncio.run(main())

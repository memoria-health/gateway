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
    # The Tapo mic is quiet — boost ~5x by default. asoftclip (tanh) is a CAUSAL
    # soft limiter (no lookahead delay, unlike alimiter) so loud sounds round
    # off instead of hard-clipping, WITHOUT adding latency. Hotter mics
    # (Trueview) overdrive the 5x into the clipper and sound garbled — set a
    # per-box override: `echo 1 > /opt/memoria-gateway/.audio-gain` (float;
    # missing/invalid file keeps the Tapo default).
    gain = 5.0
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        gain = float(open(os.path.join(here, ".audio-gain")).read().strip())
    except Exception:  # noqa: BLE001
        pass
    return ["ffmpeg", "-nostdin", "-fflags", "nobuffer", "-flags", "low_delay",
            "-rtsp_transport", "tcp", "-i", rtsp,
            "-vn", "-map", "0:a", "-af", f"volume={gain},asoftclip=type=tanh",
            "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CH),
            "-f", "s16le", "-"]


def _rtsp_host(url):
    m = re.search(r"@([^:/@]+)", url)
    return m.group(1) if m else None


def _kill_quiet(proc):
    """kill() raises ProcessLookupError if the process already exited — the
    normal case when we're tearing down BECAUSE ffmpeg died."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _rtsp_with_host(url, host):
    return re.sub(r"(@)[^:/@]+", r"\g<1>" + host, url, count=1)


# ── Privacy enforcement (2026-07-27, pinned contract) ────────────────────────
# The roster gains `privacy_enabled: bool` per camera. When true, NO caregiver
# (identity prefix "caregiver_") may receive this publisher's live video/audio;
# only analysis workers (identity prefix "detector_worker_") may subscribe.
# Enforced at the SFU via publisher-set track subscription permissions
# (LocalParticipant.set_track_subscription_permissions). The SDK's
# ParticipantTrackPermission only takes EXACT identities (no prefix match), so
# when private we maintain an allow-list of the detector_worker_* participants
# currently in the room and re-apply it whenever a new one joins.
#
# 2026-07-27: the identity service ("identity-service") is on the AI side of
# the privacy line — it must see private rooms to keep the patient seat
# working there (patient-elsewhere gating, night coverage). Caregiver
# identities stay denied at the SFU regardless. It gets its own prefix
# instead of a detector_worker_ name because the backend's startup zombie
# reaper kicks detector_worker_* participants it didn't spawn.
_DETECTOR_WORKER_PREFIX = "detector_worker_"
_IDENTITY_SERVICE_PREFIX = "identity-service"
_AI_SUBSCRIBER_PREFIXES = (_DETECTOR_WORKER_PREFIX, _IDENTITY_SERVICE_PREFIX)


class Publisher:
    """One LiveKit destination for a physical camera. In the caregiver model a
    single physical camera has one Publisher PER patient (each its own LiveKit
    room/identity); every Publisher consumes the SAME decoded frames from the
    shared CameraSource, so the camera sees exactly ONE RTSP pull no matter how
    many patients view it. desired_state is per-publisher — each patient's
    dashboard connects/disconnects independently."""

    def __init__(self, *, identity, room_name, src, privacy=False, audio=True):
        self.identity = identity
        self.room_name = room_name
        self.src = src              # back-ref: LiveKit creds + frame geometry
        self.desired = "disconnected"   # pushed by the manager each roster poll
        self.privacy = privacy      # roster privacy_enabled — pushed each poll
        self.audio = audio          # patient audio_capture_enabled — False =
                                    # facilities audio kill, no track published
        self.room = None
        self.lk_source = None       # rtc.VideoSource
        self.audio_source = None    # rtc.AudioSource
        self.publishing = False
        self._lk_dropped = False
        # Watchdog bookkeeping (2026-07-27): first moment this publisher
        # was observed NOT (connected + publishing); None while healthy.
        self.unhealthy_since = None
        # First moment the BACKEND reported this camera offline while we
        # believed we were publishing to it; None while frames land.
        # Read by RosterManager._check_publish_freshness.
        self.dark_since = None
        self.last_pub = 0.0

    def _apply_permissions_to(self, room, quiet=False):
        """Push this publisher's subscription permissions to the SFU (sync FFI
        call; raises on failure so callers can fail CLOSED). Private → deny all,
        allow-list = exactly the detector_worker_* participants currently in the
        room (allow_all=True per entry covers BOTH the video and audio tracks).
        Not private → allow everyone (also restores after a privacy→off flip).
        `quiet` suppresses the log line for the every-poll re-assert."""
        if self.privacy:
            allowed = [
                rtc.ParticipantTrackPermission(
                    participant_identity=p.identity, allow_all=True)
                for p in room.remote_participants.values()
                if p.identity.startswith(_AI_SUBSCRIBER_PREFIXES)
            ]
            room.local_participant.set_track_subscription_permissions(
                allow_all_participants=False,
                participant_permissions=allowed)
            if not quiet:
                log(f"[{self.identity}] PRIVACY ON — caregivers blocked at SFU; "
                    f"{len(allowed)} AI subscriber(s) allow-listed")
        else:
            room.local_participant.set_track_subscription_permissions(
                allow_all_participants=True)

    def _mint_token(self):
        """POST /v1/livekit/token with the gateway site token.

        The backend checks that this box owns the camera named by
        `identity` and that `room` is that camera's own room, then returns a
        JWT scoped to exactly that. A 401 here means the box is asking for a
        camera it does not own (or its site token is stale) — both are real
        errors and must surface, not be worked around.
        """
        body = json.dumps({
            "room": self.room_name,
            "identity": self.identity,
            "role": "gateway",
        }).encode()
        req = urllib.request.Request(
            f"{self.src.backend_url}/v1/livekit/token",
            data=body,
            headers={
                "Authorization": f"Bearer {self.src.gateway_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())["token"]

    def apply_permissions(self, quiet=False):
        """Re-apply permissions to the live room (privacy flag flipped by the
        roster, or a detector worker joined). No-op while disconnected — the
        next connect() applies the current flag before publishing."""
        if self.room is not None:
            self._apply_permissions_to(self.room, quiet=quiet)

    async def connect(self):
        # Ask the backend to mint the token (2026-08-03). Blocking urllib in a
        # thread, matching the roster poller's style — one short call per
        # (re)connect, not a hot path. Any failure propagates so the existing
        # reconnect-with-backoff treats it like any other connect failure; the
        # box must NOT fall back to signing its own, because it no longer has
        # a secret to sign with.
        token = await asyncio.to_thread(self._mint_token)
        room = rtc.Room()

        def _on_disconnected(*_a):
            if self.publishing:
                self._lk_dropped = True
                self.publishing = False
                log(f"[{self.identity}] LiveKit dropped — will rejoin")

        room.on("disconnected", _on_disconnected)

        def _on_participant_connected(p):
            # Exact-identity allow-list means a detector worker that joins
            # AFTER we applied permissions isn't on it yet — re-apply so it can
            # subscribe. Caregivers joining need nothing: they're already
            # denied by allow_all_participants=False. Failure here fails
            # CLOSED (the room stays restricted; only the new worker waits).
            if self.privacy and p.identity.startswith(_AI_SUBSCRIBER_PREFIXES):
                try:
                    self._apply_permissions_to(room)
                except Exception as e:  # noqa: BLE001
                    log(f"[{self.identity}] privacy: failed to allow-list "
                        f"{p.identity} ({e}) — room stays restricted")

        room.on("participant_connected", _on_participant_connected)

        def _on_reconnected(*_a):
            # SDK-internal reconnects (livekit-rtc's Rust FFI resume/restart)
            # bypass connect() entirely, and after a FULL restart there is no
            # guarantee the SDK re-sends our subscription permissions — a
            # private camera could come back allow-all. Re-apply the current
            # state (idempotent). On failure while private, fail CLOSED:
            # force a rejoin, and connect() re-applies permissions BEFORE
            # any track is published.
            try:
                self._apply_permissions_to(room)
            except Exception as e:  # noqa: BLE001
                log(f"[{self.identity}] privacy re-apply after SDK reconnect "
                    f"failed ({e})"
                    + (" — forcing rejoin (fail closed)" if self.privacy else ""))
                if self.privacy:
                    self._lk_dropped = True
                    self.publishing = False

        room.on("reconnected", _on_reconnected)
        # PUBLISH-ONLY connection: the gateway consumes nothing from the room.
        # The SDK's auto-subscribe default made every publisher decode the
        # caregiver's mic track during a Call (N patients x cameras of pure
        # waste) — enough extra load on a weak host (Pi) to stall the event
        # loop past the audio loop's read timeout and kill audio. If camera-
        # speaker talk ever lands, subscribe SELECTIVELY at call time instead.
        await room.connect(self.src.lk_url, token,
                           rtc.RoomOptions(auto_subscribe=False))
        # PRIVACY ORDERING (fail-safe): subscription permissions are applied
        # immediately after connect and BEFORE any publish_track below, so on a
        # private camera there is never a window — however brief — where a
        # caregiver could subscribe: the SFU holds the restriction before a
        # track even exists. If this raises, connect() aborts with NOTHING
        # published (fail closed) and the control loop retries. This also
        # covers reconnects: every path back to publishing runs through here.
        # Belt-and-suspenders: frames/audio only fan out once `publishing` is
        # set at the very end of this method, i.e. always after permissions.
        try:
            self._apply_permissions_to(room)
            source = rtc.VideoSource(self.src.w, self.src.h)
            track = rtc.LocalVideoTrack.create_video_track("memoria", source)
            # SINGLE-ENCODING switch (2026-07-19, flag-file gated, default
            # OFF = today's behavior). By default the SDK publishes a
            # simulcast LADDER (tiny/medium/full) and the SFU silently
            # downshifts subscribers between rungs whenever its congestion
            # estimate twitches — measured backend-side as the sharp↔blurry
            # flapping that blinds YOLO/face matching (soak #2: blur↔identity
            # r=0.52). `touch /opt/memoria-gateway/.simulcast-off` +
            # service restart publishes ONE encoding instead: constant
            # quality; congestion then shows as frame drops (sharp-but-
            # fewer beats many-mush for detection). Trade-off to decide
            # before flipping: every subscriber (caregiver app on cellular
            # included) then receives the full stream — no small rung.
            #
            # DECIDED 2026-08-24: the ladder is back ON (box-01's flag
            # renamed to `.simulcast-off.disabled-20260824`; one `mv`
            # restores it). The flapping this switch existed to stop is
            # the SFU CHOOSING a rung, and both AI subscribers now pin
            # their own with `set_video_quality` — the backend in
            # `room_worker._request_layer`, the identity service in
            # `identity/service.py:request_layer`. A pinned subscriber
            # cannot be downshifted, so the ladder costs detection
            # nothing and buys two things: an idle camera drops to
            # 640x360 (LiveKit bills the subscription whether or not
            # anything looks at the frames, and an empty room was
            # sending 4 Mbps to two subscribers all night), and the
            # caregiver app finally has a small rung on cellular.
            #
            # ORDER MATTERS. Do not remove the flag on a box whose
            # subscribers do not pin — that is the original bug. Before
            # 05:51 today both pinning calls failed with "requires a
            # simulcasted publication", which is what this flag causes.
            # Cost on the box: the encoder now makes three rungs
            # instead of one. Watch CPU on a multi-camera box.
            # Explicit top-layer encoding (Sunny's benchmark finding,
            # 2026-07-27): the bare defaults cap the top simulcast layer at
            # 720p even from a 1080p Tapo source — prod analysis was
            # silently sub-native. Pin the encoding so the top layer stays
            # the source resolution; 4 Mbps so the encoder isn't forced to
            # crush the extra pixels.
            publish_opts = rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                video_encoding=rtc.VideoEncoding(
                    max_framerate=int(round(self.src.fps or 15)),
                    max_bitrate=4_000_000,
                ),
            )
            if os.path.exists(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), ".simulcast-off")):
                publish_opts.simulcast = False
                log(f"[{self.identity}] simulcast OFF — single encoding "
                    f"({self.src.w}x{self.src.h})")
            await room.local_participant.publish_track(track, publish_opts)
            # Audio track — fed by the CameraSource's shared audio pull
            # (fanned out). SKIPPED entirely when the patient's
            # audio_capture_enabled is false (facilities audio kill,
            # 2026-07-28): the track is never published, so clips,
            # transcripts, and caregiver live audio are silent at the
            # source — nothing downstream can leak what was never sent.
            if self.audio:
                audio_source = rtc.AudioSource(
                    _AUDIO_RATE, _AUDIO_CH, queue_size_ms=_AUDIO_QUEUE_MS)
                audio_track = rtc.LocalAudioTrack.create_audio_track(
                    "memoria-audio", audio_source)
                await room.local_participant.publish_track(
                    audio_track,
                    rtc.TrackPublishOptions(
                        source=rtc.TrackSource.SOURCE_MICROPHONE))
            else:
                audio_source = None
                log(f"[{self.identity}] AUDIO OFF — no audio track published "
                    f"(patient audio_capture_enabled=false)")
        except BaseException:  # noqa: BLE001 — re-raised below
            # Anything failing after a successful room.connect (permission
            # apply, publish_track, cancellation) would otherwise leak the
            # connected room as a GHOST participant: nothing holds a
            # reference to it yet, so teardown() could never reach it.
            # Best-effort leave, then let the failure propagate (the control
            # loop retries / the caller marks us dropped).
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise
        self.room, self.lk_source, self.audio_source = room, source, audio_source
        self._lk_dropped = False
        self.publishing = True
        log(f"[{self.identity}] LiveKit CONNECTED + publishing "
            f"({'video+audio' if self.audio else 'video ONLY, audio killed'})")

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
        # LiveKit tokens are MINTED BY THE BACKEND (2026-08-03), so the box
        # never holds the LiveKit API secret. It previously read
        # `livekit_api_key`/`livekit_api_secret` from cameras.json and signed
        # its own tokens — that is how the PRODUCTION LiveKit credential came
        # to sit in three places at once (this box, a committed ops doc, and a
        # leaked .env), which in turn made rotating it a fleet-wide operation
        # nobody wanted to run. The site token below is the same one the
        # roster poll already uses.
        self.backend_url = g["backend_url"].rstrip("/")
        self.gateway_token = g["gateway_token"]
        self.logo = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "memoria.png")
        self.publishers = {}        # identity -> Publisher
        # runtime (geometry shared by all publishers)
        self.w = self.h = self.fps = None
        self.frame_us = 0
        self.frames = 0

    def attach(self, identity, room_name, desired, privacy=False, audio=True):
        """Add/refresh a publisher. Cheap + synchronous — the control loop picks up
        a newly-attached publisher on its next tick and connects it to LiveKit."""
        pub = self.publishers.get(identity)
        if pub is None:
            pub = Publisher(identity=identity, room_name=room_name, src=self,
                            privacy=privacy, audio=audio)
            self.publishers[identity] = pub
            log(f"[{identity}] attached to {_rtsp_host(self.rtsp)} "
                f"(desired={desired}, privacy={privacy}, audio={audio})")
            return
        if audio != pub.audio:
            # Facilities audio kill (2026-07-28): the audio track can only
            # be added/removed by republishing — force a reconnect either
            # direction; connect() consults pub.audio BEFORE publishing,
            # so an audio-off flip is fail-closed (no track, ever, until
            # someone flips it back and we republish with it).
            log(f"[{identity}] audio_capture_enabled -> {audio} — forcing "
                f"republish")
            pub.audio = audio
            pub._lk_dropped = True
            pub.publishing = False
        if privacy != pub.privacy:
            # Roster flipped privacy_enabled — re-apply on the LIVE room
            # (set_track_subscription_permissions is a sync FFI call). If we
            # can't apply while turning privacy ON, fail CLOSED: force a
            # reconnect, and connect() re-applies permissions BEFORE any
            # track is published.
            pub.privacy = privacy
            log(f"[{identity}] privacy_enabled -> {privacy} — updating "
                f"subscription permissions")
            try:
                pub.apply_permissions()
            except Exception as e:  # noqa: BLE001
                log(f"[{identity}] privacy apply failed ({e})"
                    + (" — forcing reconnect (fail closed)" if privacy else ""))
                if privacy:
                    pub._lk_dropped = True
                    pub.publishing = False
        elif privacy:
            # Self-asserting enforcement: set_track_subscription_permissions
            # is effectively fire-and-forget at the signal layer, so a flip
            # that "succeeded" on a half-dead connection may never have
            # reached the SFU. Re-apply (idempotent, cheap sync FFI) on
            # EVERY roster poll while private — a missed flip converges
            # within poll_seconds. Failure fails CLOSED like the flip above.
            try:
                pub.apply_permissions(quiet=True)
            except Exception as e:  # noqa: BLE001
                log(f"[{identity}] privacy re-assert failed ({e}) — forcing "
                    f"reconnect (fail closed)")
                pub._lk_dropped = True
                pub.publishing = False
        pub.desired = desired
        return pub

    async def detach(self, identity):
        pub = self.publishers.pop(identity, None)
        if pub is not None:
            await pub.teardown()
            log(f"[{identity}] detached from {_rtsp_host(self.rtsp)}")

    # (The 2026-07-27 `_watchdog` that lived here was DEAD — never
    # scheduled, and it read `self.sources`, a RosterManager attribute
    # CameraSource does not have, so it would have crashed if it ever
    # had been. Both of its judgments now run for real in
    # RosterManager._check_publish_freshness, which is on a loop that
    # actually executes.)

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
        reader = asyncio.create_task(self._reader_loop(proc))
        control = asyncio.create_task(self._control_loop())
        # Audio runs ALONGSIDE but does NOT gate the pipeline: an audio failure
        # is retried by its own supervisor without disturbing video.
        audio = asyncio.create_task(self._audio_supervisor())
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
            _kill_quiet(proc)
            for pub in list(self.publishers.values()):
                await pub.teardown()

    async def _audio_supervisor(self):
        """Keep the audio pull alive for the life of the video pipeline. The
        old design ran _audio_loop ONCE and gave up on any failure ("recovers
        on next pipeline restart") — but the pipeline only restarts when VIDEO
        fails, so on a healthy stream a single 8s audio stall meant PERMANENT
        silence (seen in the field: a Call press loaded a Pi past the read
        timeout and audio never returned). Respawn the audio ffmpeg with
        backoff instead — a failure is now a blip, not an outage. Backoff
        resets after a healthy minute; a camera with no audio track settles
        into a quiet 30s retry cadence."""
        backoff = 2
        while True:
            aproc = await asyncio.create_subprocess_exec(
                *build_audio_ffmpeg_cmd(self.rtsp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            started = time.monotonic()
            try:
                await self._audio_loop(aproc)  # returns only on failure
            finally:
                _kill_quiet(aproc)
            if time.monotonic() - started >= 60:
                backoff = 2
            log(f"[{_rtsp_host(self.rtsp)}] audio: restarting pull in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

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
                t_read = time.monotonic()
                buf = await asyncio.wait_for(
                    proc.stdout.readexactly(chunk), timeout=8.0)
                now = time.monotonic()
                starved = (now - t_read) > 0.004
                if t0 is None:
                    t0 = now
                behind = (now - t0) - fwd_s
                fwd_s += 0.010
                # SOURCE-GAP REBASE (fix 2026-07-21): `behind` is only real,
                # drainable backlog while the pipe has data QUEUED. If this
                # read had to WAIT (pipe ran dry), we are at the live edge —
                # accumulated `behind` is a SOURCE gap (a camera stall: audio
                # that never existed), not backlog, and can never be drained.
                # Without this, one stall engaged the dropper permanently and
                # it silently discarded 100% of live audio while ffmpeg + this
                # loop looked healthy (Nagpur Trueview, flaky-wifi stalls).
                # Rebase the clock to the live edge instead of dropping.
                if starved and behind > STOP_DROP_S:
                    t0 = now - fwd_s
                    behind = 0.0
                    if dropping:
                        dropping = False
                        log(f"[{_rtsp_host(self.rtsp)}] audio: clock rebased "
                            f"after source gap — resuming live audio")
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
                f"unaffected; supervisor will respawn the audio pull")

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
        # noise; MOTION_FRAC is how much of the frame must move.
        #
        # PIXEL_DELTA 30→45 (2026-07-17): the old value was calibrated on
        # DAYTIME noise (idle >30 floor ~0.2% vs 0.25% trip — 0.05pp of
        # headroom). IR night mode's gain fattens the noise tail past the
        # line: measured on the Nagpur cam over a full night, 97.4% of
        # ticks tripped >30 (p50 0.32%, max 0.57%) — the backend paid for
        # Gemini calls on an empty office all night (~$31/day). At >45 the
        # same night reads p99 0.09% / max 0.13% — 2x under the line —
        # while a real scene change measured 40%+ and a person is far
        # above that. Day ambient (PC LED flicker, dawn) reads 0.03-0.04%
        # at >45. This hint is additionally VERIFIED backend-side by
        # block-motion (nemara-backend services/detection/motion.py), so
        # this constant is hardening, not the last line of defense.
        PIXEL_DELTA = 45
        MOTION_FRAC = 0.0025  # sensitive: false wakes are cheap (worker
                              # re-checks for people, sleeps if none)
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


# ── Process watchdog (2026-07-27, rewired 2026-07-29) ────────────────
# The per-camera supervisor + rejoin loop handle every failure that
# RAISES. What they cannot see is the zombie transport: the SDK's
# "disconnected" event never fires, publishing stays True, and
# capture_frame swallows frames into a dead WebRTC connection — the
# gateway ran 4d10h in exactly that state, blind and "healthy", and
# then did it again on the 2026-07-29 wifi blip.
# The watchdog judges OUTCOME and kills the whole process; run.sh /
# systemd restart us (Restart=always + StartLimitIntervalSec=0 — it
# must NEVER declare us hopeless) and a from-scratch boot rebuilds
# every connection with no human involved.
# It lives in RosterManager._check_publish_freshness and runs on the
# roster poll (poll_seconds, ~3s). The 2026-07-27 version was written
# as a standalone loop that nothing ever started — which is precisely
# why the second zombie survived. If you move this logic again, make
# sure something still calls it.
WD_LK_UNHEALTHY_S = 45.0
# How long the backend may report a camera offline while we believe we
# are publishing to it, before we restart. Well clear of the reconciler's
# own debounce (10s poll + 30s liveness grace + absent-streak) so an
# ordinary blip that both sides recover from never trips it.
WD_PUBLISH_DARK_S = 120.0


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
        # source_key -> CameraSource.frames at the previous roster poll.
        # The watchdog compares against it to tell "we're decoding but
        # can't get frames out" (our bug) from "we have no frames to
        # send" (the camera's).
        self._wd_frames = {}
        self._warned = set()
        self._fingerprint = ""
        self._lock = asyncio.Lock()

    @staticmethod
    def _fp(cameras):
        # Deliberately EXCLUDES privacy_enabled: this string is sent as `since`
        # to /gateway/wait, and the backend compares it against ITS fingerprint
        # (identity:desired_state today) — adding a field here that the backend
        # doesn't fingerprint would make every long-poll return "changed"
        # instantly (a request storm). Privacy flips still propagate within
        # poll_seconds via _poll_loop, which applies the roster every cycle;
        # if the backend answers with its own fingerprint we adopt it (see
        # _wait_loop), so a backend-side format change self-corrects too.
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

    async def _apply(self, cameras, fp=None):
        async with self._lock:
            self._reload_local()
            await self._reconcile(cameras)
            # Prefer the backend-computed fingerprint when the response carries
            # one — format-agnostic, so the long-poll stays quiet even if the
            # backend fingerprints more fields than we do.
            self._fingerprint = fp if fp is not None else self._fp(cameras)

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
            self._check_publish_freshness(roster)
            await asyncio.sleep(self.poll_s)

    def _check_publish_freshness(self, roster):
        """The process watchdog. Two judgments, one loop.

        (A) LiveKit visibly unhealthy — we want to be connected but
        room / publishing / connection_state say we are not. This was
        written 2026-07-27 but lived on a method nothing ever scheduled,
        so it has never once run; it does now.

        (B) The zombie — everything here looks perfect and no media is
        actually leaving.

        The 2026-07-29 wifi blip left a publisher that had a room, had
        publications, and reported CONN_CONNECTED — while zero media
        actually reached the SFU. Nothing observable on this box could
        tell the difference: `capture_frame` swallows frames into a dead
        WebRTC transport without complaint. Only the subscriber knows,
        so the roster now carries `online` (the backend's own view of
        frame arrival, stamped on real frames and independent of AI
        pause) and we act on it.

        BOTH judgments are gated on this box actually DECODING frames
        for that camera right now, and this gate is load-bearing. When a
        camera is unplugged or off the wifi, `_run_pipeline` raises and
        its `finally` tears every publisher down — so `room is None` and
        `publishing is False`, which reads as "unhealthy" to (A) and
        would kill the process every 45s for as long as the camera stays
        unplugged, taking every OTHER camera on this box down with it on
        each restart. A dead camera is the per-camera supervisor's job
        (it retries with backoff); the process watchdog only has
        standing when we have frames in hand and cannot get them out.

        Restart, not repair: a from-scratch boot rebuilds every RTSP
        pull and every LiveKit connection, which is the only thing known
        to clear this state. run.sh / systemd bring us straight back.

        Called ONLY from the roster poll — `/gateway/wait` answers with
        a different, `online`-free payload, and treating a missing field
        as "healthy" there would keep resetting the timer.
        """
        online = {c.get("livekit_identity"): c.get("online")
                  for c in roster if c.get("livekit_identity")}
        now = time.monotonic()
        for gone in set(self._wd_frames) - set(self.sources):
            self._wd_frames.pop(gone, None)   # camera deleted from the roster
        for key, (src, _task) in list(self.sources.items()):
            # Did our own decode advance since the previous poll? The
            # counter is monotonic per pipeline and resets to 0 on a
            # pipeline restart, so compare with > and treat any reset as
            # "not pulling" for one poll — a conservative direction.
            prev = self._wd_frames.get(key)
            self._wd_frames[key] = src.frames
            pulling = prev is not None and src.frames > prev

            for ident, pub in list(src.publishers.items()):
                if not pulling:
                    pub.unhealthy_since = None
                    pub.dark_since = None
                    continue

                # (A) visibly unhealthy
                if pub.desired != "connected":
                    pub.unhealthy_since = None
                else:
                    healthy = (
                        pub.room is not None
                        and pub.publishing
                        and pub.room.connection_state
                        == rtc.ConnectionState.CONN_CONNECTED
                    )
                    if healthy:
                        pub.unhealthy_since = None
                    elif pub.unhealthy_since is None:
                        pub.unhealthy_since = now
                    elif now - pub.unhealthy_since > WD_LK_UNHEALTHY_S:
                        log(f"[{ident}] WATCHDOG: LiveKit unhealthy "
                            f"{now - pub.unhealthy_since:.0f}s "
                            f"(room={'yes' if pub.room else 'no'} "
                            f"publishing={pub.publishing}) — exiting for "
                            f"a clean restart")
                        os._exit(3)

                # (B) the zombie. `is not False` covers a backend too
                # old to send the field: with no signal we cannot judge,
                # so we clear the timer rather than restart on missing
                # data.
                if not pub.publishing or online.get(ident) is not False:
                    pub.dark_since = None
                    continue
                if pub.dark_since is None:
                    pub.dark_since = now
                    log(f"[{ident}] backend reports NO frames arriving while "
                        f"we believe we're publishing — watching")
                elif now - pub.dark_since > WD_PUBLISH_DARK_S:
                    log(f"[{ident}] WATCHDOG: publishing into the void for "
                        f"{now - pub.dark_since:.0f}s (backend says offline) "
                        f"— exiting for a clean restart")
                    os._exit(4)

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
                await self._apply(resp.get("cameras", []),
                                  fp=resp.get("fingerprint"))

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
            # privacy_enabled is a NEW roster field (pinned contract) — parse
            # tolerantly so an older backend that omits it means "not private".
            grp["pubs"][ident] = (cam["livekit_room"], cam["desired_state"],
                                  bool(cam.get("privacy_enabled", False)),
                                  bool(cam.get("audio_capture_enabled", True)))

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
                for ident, (room, desired, privacy, audio) in grp["pubs"].items():
                    src.attach(ident, room, desired, privacy, audio)
                task = asyncio.create_task(src.run())
                self.sources[key] = (src, task)
                log(f"[{_rtsp_host(key)}] source started — 1 RTSP pull, "
                    f"{len(grp['pubs'])} publisher(s)")
            else:
                src, _ = self.sources[key]
                # attach new / refresh desired_state + privacy + audio for
                # wanted publishers (a privacy flip re-applies SFU
                # permissions; an audio flip forces a republish)
                for ident, (room, desired, privacy, audio) in grp["pubs"].items():
                    src.attach(ident, room, desired, privacy, audio)
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

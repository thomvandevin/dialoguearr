#!/usr/bin/env python3
"""
audio-normalize: add a dialogue-forward stereo track to surround-only media.

Films are mastered far below streaming loudness (frequently under -27 LUFS)
while peaking near full scale. A television folding 5.1 down to two speakers
compounds it, mixing LFE and surrounds into the same output as the centre
channel, so dialogue ends up buried under effects.

For each file holding a 6-channel track but no stereo track, this adds an AAC
2.0 track built from a centre-forward matrix with LFE discarded, normalised to
TARGET_LUFS with a constrained loudness range, and marks it default. The
original surround track is always preserved.

Work arrives two ways. Sonarr and Radarr POST to the webhook on import, and
those are handled straight away so a file is ready the same evening. The
periodic scan is a backfill safety net for anything the webhook missed, and is
confined to WINDOW_START..WINDOW_END so bulk work happens overnight.

Environment variables:
    MEDIA_PATH        Root directory to scan (default: /data/media)
    SCAN_INTERVAL     Seconds between backfill scans (default: 3600)
    WEBHOOK_PORT      Port for the Sonarr/Radarr webhook (default: 8080)
    IMPORT_DELAY      Seconds to let an imported file settle (default: 60)
    WINDOW_START      Hour processing may begin, 0-23 (default: 3)
    WINDOW_END        Hour processing must stop, 0-23 (default: 8)
    TARGET_LUFS       Integrated loudness target (default: -16)
    TARGET_LRA        Loudness range target (default: 8)
    TARGET_TP         True peak ceiling in dBTP (default: -1.5)
    PAN_CENTRE        Centre channel weight (default: 0.9)
    PAN_FRONT         Front left/right weight (default: 0.55)
    PAN_SURROUND      Surround left/right weight (default: 0.25)
    AUDIO_BITRATE     Bitrate of the new track (default: 256k)
    TRACK_TITLE       Title of the new track, also the already-done marker
    PREFERRED_LANGUAGES  Comma-separated codes, e.g. "jpn,eng". Empty means
                      follow whichever track the file marks default.
    PLEX_URL          Plex base URL, empty disables refreshes
    PLEX_TOKEN        Plex authentication token
    DRY_RUN           "true" to report candidates without writing (default: false)
    LOG_LEVEL         Logging level (default: INFO)
"""

import json
import logging
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEDIA_PATH = Path(os.environ.get("MEDIA_PATH", "/data/media"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "3600"))
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8080"))
IMPORT_DELAY = int(os.environ.get("IMPORT_DELAY", "60"))

WINDOW_START = int(os.environ.get("WINDOW_START", "3"))
WINDOW_END = int(os.environ.get("WINDOW_END", "8"))

TARGET_LUFS = float(os.environ.get("TARGET_LUFS", "-16"))
TARGET_LRA = float(os.environ.get("TARGET_LRA", "8"))
TARGET_TP = float(os.environ.get("TARGET_TP", "-1.5"))

PAN_CENTRE = float(os.environ.get("PAN_CENTRE", "0.9"))
PAN_FRONT = float(os.environ.get("PAN_FRONT", "0.55"))
PAN_SURROUND = float(os.environ.get("PAN_SURROUND", "0.25"))

AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "256k")
TRACK_TITLE = os.environ.get("TRACK_TITLE", "Stereo (dialogue boost)")
PREFERRED_LANGUAGES = [x.strip().lower()
                       for x in os.environ.get("PREFERRED_LANGUAGES", "").split(",")
                       if x.strip()]

# Files tag languages inconsistently as 2- or 3-letter codes.
LANG_ALIASES = {"ja": "jpn", "jp": "jpn", "en": "eng", "es": "spa", "fr": "fra",
                "de": "deu", "ger": "deu", "it": "ita", "pt": "por", "nl": "nld"}

PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

CONTAINERS = {".mkv", ".mp4", ".m4v"}
DURATION_TOLERANCE = 2.0
SILENCE_FLOOR = -60.0  # ebur128 reports -70 for digital silence

# Channel indices rather than names: ffmpeg labels the surround pair BL/BR for
# 5.1 but SL/SR for 5.1(side), while the channel order is identical in both.
PAN = (
    "pan=stereo|"
    f"c0={PAN_CENTRE}*c2+{PAN_FRONT}*c0+{PAN_SURROUND}*c4|"
    f"c1={PAN_CENTRE}*c2+{PAN_FRONT}*c1+{PAN_SURROUND}*c5"
)

SINGLE_PASS = object()

LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)
EBUR128_I = re.compile(r"I:\s+(-?[\d.]+|-inf)\s+LUFS")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("audio-normalize")


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path):
    r = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)])
    if r.returncode != 0:
        log.warning("ffprobe failed for %s: %s", path.name, r.stderr.strip()[:200])
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def language_of(stream):
    code = ((stream.get("tags") or {}).get("language") or "").lower()
    return LANG_ALIASES.get(code, code)


def surround_track(info):
    """Return (audio-relative index, stream) of the 5.1 track to convert."""
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        return None
    if any((s.get("tags") or {}).get("title") == TRACK_TITLE for s in audio):
        return None
    if any(int(s.get("channels") or 0) <= 2 for s in audio):
        return None
    surround = [(i, s) for i, s in enumerate(audio)
                if int(s.get("channels") or 0) == 6]
    if not surround:
        return None

    # Most of these files carry several dubs in arbitrary track order, so an
    # explicit preference wins, then whichever track the release marked
    # default, so the new track matches the language that played before.
    for want in PREFERRED_LANGUAGES:
        for i, s in surround:
            if language_of(s) == LANG_ALIASES.get(want, want):
                return i, s
    for i, s in surround:
        if (s.get("disposition") or {}).get("default"):
            return i, s
    return surround[0]


def analyse(path, aidx):
    """First loudnorm pass, returning the measured values for the second."""
    flt = (f"{PAN},loudnorm=I={TARGET_LUFS}:LRA={TARGET_LRA}"
           f":TP={TARGET_TP}:print_format=json")
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-map", f"0:a:{aidx}", "-af", flt, "-f", "null", "-"])
    m = LOUDNORM_JSON.search(r.stderr)
    if not m:
        log.error("  no loudnorm measurement returned")
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

    # A stream whose format changes mid-file makes ffmpeg rebuild the filter
    # graph and report only the final segment, which is often a second of
    # silence. Those measurements are unusable, so normalise in one pass.
    try:
        if all(math.isfinite(float(data[k]))
               for k in ("input_i", "input_lra", "input_tp", "input_thresh")):
            return data
    except (KeyError, ValueError):
        pass
    log.warning("  measurement unusable (input_i=%s), using single-pass",
                data.get("input_i"))
    return SINGLE_PASS


def encode(src, dst, aidx, measured, n_audio):
    flt = f"{PAN},loudnorm=I={TARGET_LUFS}:LRA={TARGET_LRA}:TP={TARGET_TP}"
    if measured is not SINGLE_PASS:
        flt += (f":measured_I={measured['input_i']}"
                f":measured_LRA={measured['input_lra']}"
                f":measured_TP={measured['input_tp']}"
                f":measured_thresh={measured['input_thresh']}"
                f":offset={measured['target_offset']}")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(src),
           "-map", "0:v", "-map", f"0:a:{aidx}", "-map", "0:a",
           "-map", "0:s?", "-map", "0:t?", "-map_chapters", "0",
           "-c", "copy", "-max_muxing_queue_size", "4096",
           # loudnorm resamples to 192kHz internally, so pin the rate back.
           "-c:a:0", "aac", "-b:a:0", AUDIO_BITRATE, "-ar:a:0", "48000",
           "-filter:a:0", flt,
           "-metadata:s:a:0", f"title={TRACK_TITLE}",
           "-disposition:a:0", "default"]
    for i in range(1, n_audio + 1):
        cmd += [f"-disposition:a:{i}", "0"]
    cmd.append(str(dst))

    r = run(cmd)
    if r.returncode != 0:
        tail = r.stderr.strip().splitlines()
        log.error("  encode failed: %s", tail[-1] if tail else "unknown error")
        return False
    return True


def verify(dst, expected_duration):
    """Confirm the rewritten file is complete before it replaces the original."""
    info = probe(dst)
    if not info:
        return False, "unreadable"

    duration = float(info.get("format", {}).get("duration") or 0)
    if abs(duration - expected_duration) > DURATION_TOLERANCE:
        return False, f"duration {duration:.1f}s, expected {expected_duration:.1f}s"

    audio = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if not audio or int(audio[0].get("channels") or 0) != 2:
        return False, "new track missing or not stereo"
    if (audio[0].get("tags") or {}).get("title") != TRACK_TITLE:
        return False, "new track is not labelled"

    # Duration already rules out truncation, so sample for content instead.
    # A film may legitimately end in silence, but a broken encode is silent
    # throughout, so probe inside the body and take the loudest reading.
    loudest = None
    for fraction in (0.5, 0.25, 0.75):
        r = run(["ffmpeg", "-hide_banner", "-nostats", "-ss", str(duration * fraction),
                 "-t", "30", "-i", str(dst), "-map", "0:a:0", "-af", "ebur128",
                 "-f", "null", "-"])
        found = EBUR128_I.findall(r.stderr)
        if not found or found[-1] == "-inf":
            continue
        try:
            level = float(found[-1])
        except ValueError:
            continue
        if loudest is None or level > loudest:
            loudest = level
        if loudest > SILENCE_FLOOR:
            break

    if loudest is None or loudest <= SILENCE_FLOOR:
        return False, "new track is silent"

    return True, f"{loudest:.1f} LUFS"


# ---------------------------------------------------------------------------
# Plex
# ---------------------------------------------------------------------------


_sections = []


def plex_sections():
    """Library sections, fetched lazily and cached on first success.

    Plex is usually still starting when this container comes up after a host
    reboot, so fetching once at startup would leave refreshes silently
    disabled for the lifetime of the process.
    """
    global _sections
    if _sections:
        return _sections
    if not (PLEX_URL and PLEX_TOKEN):
        return []
    try:
        r = requests.get(f"{PLEX_URL}/library/sections",
                         headers={"X-Plex-Token": PLEX_TOKEN}, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError) as exc:
        log.warning("could not list Plex sections: %s", exc)
        return []
    _sections = [(d.get("key"), [loc.get("path") for loc in d.findall("Location")])
                 for d in root.findall("Directory")]
    return _sections


def plex_refresh(path):
    folder = str(path.parent)
    for key, paths in plex_sections():
        if not any(folder == p or folder.startswith(p.rstrip("/") + "/")
                   for p in paths if p):
            continue
        try:
            requests.put(f"{PLEX_URL}/library/sections/{key}/refresh",
                         params={"path": folder},
                         headers={"X-Plex-Token": PLEX_TOKEN}, timeout=15)
        except requests.RequestException as exc:
            log.warning("  Plex refresh failed: %s", exc)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def in_window():
    if WINDOW_START == WINDOW_END:
        return True
    hour = datetime.now().hour
    if WINDOW_START < WINDOW_END:
        return WINDOW_START <= hour < WINDOW_END
    return hour >= WINDOW_START or hour < WINDOW_END


def scan():
    for path in sorted(MEDIA_PATH.rglob("*")):
        if path.name.startswith(".") or path.suffix.lower() not in CONTAINERS:
            continue
        if path.is_file():
            yield path


def cleanup_temp():
    """Drop temp files orphaned by an encode that was killed part way through."""
    for stale in MEDIA_PATH.rglob(".*.audionorm.*"):
        try:
            size = stale.stat().st_size
            stale.unlink()
            log.warning("removed orphaned temp file %s (%.1f GB)",
                        stale.name, size / 1024 ** 3)
        except OSError as exc:
            log.error("could not remove %s: %s", stale.name, exc)


def process(path):
    info = probe(path)
    if not info:
        return False

    picked = surround_track(info)
    if not picked:
        # Only webhook-queued files reach here without being candidates, since
        # the backfill scan pre-filters. Saying why keeps a silent skip from
        # looking like a failure.
        audio = [x for x in info.get("streams", []) if x.get("codec_type") == "audio"]
        if any((x.get("tags") or {}).get("title") == TRACK_TITLE for x in audio):
            reason = "already has a dialogue-boost track"
        elif any(int(x.get("channels") or 0) <= 2 for x in audio):
            reason = "already has a stereo track"
        else:
            reason = "no 5.1 track"
        log.info("skipping %s: %s", path.name, reason)
        return False
    aidx, stream = picked

    duration = float(info.get("format", {}).get("duration") or 0)
    if duration <= 0:
        log.warning("skipping %s: unknown duration", path.name)
        return False

    n_audio = len([s for s in info["streams"] if s.get("codec_type") == "audio"])
    log.info("candidate: %s (%s %dch, %.0f min)", path.name,
             stream.get("codec_name"), int(stream.get("channels") or 0), duration / 60)

    if DRY_RUN:
        log.info("  DRY_RUN, not writing")
        return False

    measured = analyse(path, aidx)
    if not measured:
        return False
    if measured is not SINGLE_PASS:
        log.info("  measured %s LUFS, LRA %s, peak %s dBTP",
                 measured["input_i"], measured["input_lra"], measured["input_tp"])

    mtime = path.stat().st_mtime
    tmp = path.with_name(f".{path.stem}.audionorm{path.suffix}")
    try:
        if not encode(path, tmp, aidx, measured, n_audio):
            return False

        ok, detail = verify(tmp, duration)
        if not ok:
            log.error("  verification failed (%s), original left untouched", detail)
            return False

        if path.stat().st_mtime != mtime:
            log.warning("  source changed while encoding, discarding result")
            return False

        os.replace(tmp, path)
        os.chmod(path, 0o664)
        log.info("  replaced (%s)", detail)
        plex_refresh(path)
        return True
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# Work queue
# ---------------------------------------------------------------------------

work = queue.Queue()
pending = set()
pending_lock = threading.Lock()


def enqueue(path, urgent):
    with pending_lock:
        if path in pending:
            return False
        pending.add(path)
    work.put((path, urgent))
    return True


def worker():
    while True:
        path, urgent = work.get()
        try:
            # Backfill items are window-bound; the next scan re-queues whatever
            # is dropped here. Imports are never deferred.
            if not urgent and not in_window():
                continue
            if urgent and IMPORT_DELAY:
                time.sleep(IMPORT_DELAY)
            if not path.is_file():
                continue
            process(path)
        except OSError as exc:
            log.error("error handling %s: %s", path.name, exc)
        finally:
            with pending_lock:
                pending.discard(path)
            work.task_done()


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def media_paths(obj, found=None):
    """Pull existing media file paths out of an arbitrary webhook payload.

    Sonarr and Radarr disagree on payload shape and have changed it between
    versions, so this walks the whole document rather than reading fixed keys.
    """
    if found is None:
        found = []
    if isinstance(obj, dict):
        for value in obj.values():
            media_paths(value, found)
    elif isinstance(obj, list):
        for value in obj:
            media_paths(value, found)
    elif isinstance(obj, str) and obj.startswith("/"):
        candidate = Path(obj)
        if candidate.suffix.lower() in CONTAINERS and candidate.is_file():
            found.append(candidate)
    return found


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        event = payload.get("eventType", "?") if isinstance(payload, dict) else "?"
        if event == "Test":
            log.info("webhook test received")
            return

        for path in dict.fromkeys(media_paths(payload)):
            if enqueue(path, urgent=True):
                log.info("webhook (%s) queued %s", event, path.name)

    def log_message(self, *args):
        pass


def serve():
    server = ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    server.serve_forever()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    log.info("audio-normalize starting")
    log.info("  media=%s window=%02d:00-%02d:00 target=%.1f LUFS lra=%.1f dry_run=%s",
             MEDIA_PATH, WINDOW_START, WINDOW_END, TARGET_LUFS, TARGET_LRA, DRY_RUN)

    cleanup_temp()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    log.info("  webhook listening on port %d", WEBHOOK_PORT)

    while True:
        if in_window():
            queued = 0
            for path in scan():
                info = probe(path)
                if info and surround_track(info) and enqueue(path, urgent=False):
                    queued += 1
            if queued:
                log.info("backfill scan queued %d file(s)", queued)
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("shutting down")

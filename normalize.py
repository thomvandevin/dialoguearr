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
    PORT              HTTP port (default: 7373)
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
from pathlib import Path

import requests

import db
import settings

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEDIA_PATH = Path(os.environ.get("MEDIA_PATH", "/data/media"))





TRACK_TITLE = os.environ.get("TRACK_TITLE", "Stereo (dialogue boost)")

# Files tag languages inconsistently as 2- or 3-letter codes.
LANG_ALIASES = {"ja": "jpn", "jp": "jpn", "en": "eng", "es": "spa", "fr": "fra",
                "de": "deu", "ger": "deu", "it": "ita", "pt": "por", "nl": "nld"}

PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

CONTAINERS = {".mkv", ".mp4", ".m4v"}
DURATION_TOLERANCE = 2.0
SILENCE_FLOOR = -60.0  # ebur128 reports -70 for digital silence


SINGLE_PASS = object()

def preferred_languages():
    return [x.strip().lower()
            for x in (settings.get("preferred_languages") or "").split(",") if x.strip()]


def pan_filter():
    """Centre-forward downmix with LFE discarded.

    Channel indices rather than names: ffmpeg labels the surround pair BL/BR
    for 5.1 but SL/SR for 5.1(side), while the channel order is identical.
    """
    c = settings.get("pan_centre")
    f = settings.get("pan_front")
    r = settings.get("pan_surround")
    return (f"pan=stereo|c0={c}*c2+{f}*c0+{r}*c4|c1={c}*c2+{f}*c1+{r}*c5")


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


def surround_track(info, allow_existing_stereo=False, override=None):
    """Return (audio-relative index, stream) of the 5.1 track to convert."""
    audio = audio_streams(info)
    if not audio:
        return None
    if any((s.get("tags") or {}).get("title") == TRACK_TITLE for s in audio):
        return None
    if not allow_existing_stereo and any(int(s.get("channels") or 0) <= 2 for s in audio):
        return None

    surround = [(i, s) for i, s in enumerate(audio)
                if int(s.get("channels") or 0) == 6]
    if not surround:
        return None

    override = override or {}

    # An explicit per-file choice wins over everything, which is the escape
    # hatch for releases whose tracks carry no language tags at all.
    forced = override.get("force_track")
    if forced is not None:
        for i, s in surround:
            if i == int(forced):
                return i, s

    wanted = override.get("force_language")
    preferred = [wanted.strip().lower()] if wanted else preferred_languages()

    # Most of these files carry several dubs in arbitrary track order, so an
    # explicit preference wins, then whichever track the release marked
    # default, so the new track matches the language that played before.
    for want in preferred:
        for i, s in surround:
            if language_of(s) == LANG_ALIASES.get(want, want):
                return i, s
    for i, s in surround:
        if (s.get("disposition") or {}).get("default"):
            return i, s
    return surround[0]


def existing_stereo_level(path, info, duration):
    """Level of a file's existing stereo track, measured once and remembered.

    Only consulted when restereo_below is set, since it costs a decode.
    """
    audio = audio_streams(info)
    idx = next((i for i, s in enumerate(audio)
                if int(s.get("channels") or 0) <= 2), None)
    if idx is None:
        return None
    cached = db.get_file(path) or {}
    if cached.get("existing_stereo_i") is not None:
        return cached["existing_stereo_i"]
    level = sample_loudness(path, idx, duration)
    if level is not None:
        db.upsert_file(path, existing_stereo_i=level)
    return level


def wants_reprocessing(path, info, duration):
    """True when an existing stereo track is too quiet to count as a fallback."""
    threshold = settings.get("restereo_below")
    if threshold is None:
        return False
    level = existing_stereo_level(path, info, duration)
    return level is not None and level < threshold


def analyse(path, aidx):
    """First loudnorm pass, returning the measured values for the second."""
    flt = (f"{pan_filter()},loudnorm=I={settings.get('target_lufs')}"
           f":LRA={settings.get('target_lra')}:TP={settings.get('target_tp')}:print_format=json")
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
    flt = (f"{pan_filter()},loudnorm=I={settings.get('target_lufs')}"
           f":LRA={settings.get('target_lra')}:TP={settings.get('target_tp')}")
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
           "-c:a:0", "aac", "-b:a:0", settings.get("audio_bitrate"), "-ar:a:0", "48000",
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
    start, end = settings.get("window_start"), settings.get("window_end")
    if start == end:
        return True
    hour = datetime.now().hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


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


def library_of(path):
    try:
        return path.relative_to(MEDIA_PATH).parts[0]
    except (ValueError, IndexError):
        return "unknown"


def skip_reason(info):
    audio = [x for x in info.get("streams", []) if x.get("codec_type") == "audio"]
    if any((x.get("tags") or {}).get("title") == TRACK_TITLE for x in audio):
        return "already has a dialogue-boost track"
    if any(int(x.get("channels") or 0) <= 2 for x in audio):
        return "already has a stereo track"
    return "no 5.1 track"


def candidate(path, info=None):
    """The track to convert, honouring per-file overrides and the threshold."""
    info = info if info is not None else probe(path)
    if not info:
        return None
    row = db.get_file(path) or {}
    if row.get("excluded"):
        return None
    duration = float(info.get("format", {}).get("duration") or 0)
    allow = wants_reprocessing(path, info, duration) if duration > 0 else False
    return surround_track(info, allow_existing_stereo=allow, override=row)


def process(path, trigger="scan"):
    info = probe(path)
    if not info:
        return False

    if (db.get_file(path) or {}).get("excluded"):
        log.info("skipping %s: excluded", path.name)
        return False

    picked = candidate(path, info)
    if not picked:
        # Only webhook-queued files reach here without being candidates, since
        # the backfill scan pre-filters. Saying why keeps a silent skip from
        # looking like a failure.
        reason = skip_reason(info)
        log.info("skipping %s: %s", path.name, reason)
        db.upsert_file(path, library=library_of(path), name=path.name,
                       state="skipped", skip_reason=reason)
        return False
    aidx, stream = picked

    duration = float(info.get("format", {}).get("duration") or 0)
    if duration <= 0:
        log.warning("skipping %s: unknown duration", path.name)
        return False

    n_audio = len([s for s in info["streams"] if s.get("codec_type") == "audio"])
    log.info("candidate: %s (%s %dch, %.0f min)", path.name,
             stream.get("codec_name"), int(stream.get("channels") or 0), duration / 60)

    db.upsert_file(path, library=library_of(path), name=path.name,
                   duration=duration, size=path.stat().st_size, state="eligible",
                   source_lang=language_of(stream) or None,
                   source_codec=stream.get("codec_name"),
                   source_channels=int(stream.get("channels") or 0))

    if settings.get("dry_run"):
        log.info("  DRY_RUN, not writing")
        return False

    run_id = db.start_run(path, path.name, trigger)
    _current.update(path=str(path), name=path.name, stage="analysing",
                    started=db.now())
    mode = None
    try:
        measured = analyse(path, aidx)
        if not measured:
            db.finish_run(run_id, "failed", "analysis produced no measurement")
            db.upsert_file(path, state="failed")
            return False

        mode = "single-pass" if measured is SINGLE_PASS else "two-pass"
        if measured is not SINGLE_PASS:
            log.info("  measured %s LUFS, LRA %s, peak %s dBTP",
                     measured["input_i"], measured["input_lra"], measured["input_tp"])
            db.upsert_file(path, input_i=float(measured["input_i"]),
                           input_lra=float(measured["input_lra"]),
                           input_tp=float(measured["input_tp"]),
                           measurement="full")

        _current["stage"] = "encoding"
        mtime = path.stat().st_mtime
        tmp = path.with_name(f".{path.stem}.audionorm{path.suffix}")
        try:
            if not encode(path, tmp, aidx, measured, n_audio):
                db.finish_run(run_id, "failed", "encode failed", mode)
                db.upsert_file(path, state="failed")
                return False

            _current["stage"] = "verifying"
            ok, detail = verify(tmp, duration)
            if not ok:
                log.error("  verification failed (%s), original left untouched", detail)
                db.finish_run(run_id, "failed", f"verification failed: {detail}", mode)
                db.upsert_file(path, state="failed")
                return False

            if path.stat().st_mtime != mtime:
                log.warning("  source changed while encoding, discarding result")
                db.finish_run(run_id, "aborted", "source changed while encoding", mode)
                return False

            os.replace(tmp, path)
            os.chmod(path, 0o664)
            log.info("  replaced (%s)", detail)
            db.finish_run(run_id, "replaced", detail, mode)
            db.upsert_file(path, state="done", mode=mode, processed_at=db.now(),
                           size=path.stat().st_size,
                           output_i=measured_output(detail))
            plex_refresh(path)
            return True
        finally:
            if tmp.exists():
                tmp.unlink()
    finally:
        _current.update(path=None, name=None, stage=None, started=None)


def measured_output(detail):
    """verify() reports the loudest sampled level, e.g. "-16.4 LUFS"."""
    try:
        return float(detail.split()[0])
    except (AttributeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Work queue
# ---------------------------------------------------------------------------

work = queue.Queue()
pending = set()
pending_lock = threading.Lock()

_current = {"path": None, "name": None, "stage": None, "started": None}


def status():
    with pending_lock:
        depth = len(pending)
    return {
        "current": dict(_current),
        "queued": depth,
        "in_window": in_window(),
        "window": f"{settings.get('window_start'):02d}:00-{settings.get('window_end'):02d}:00",
        "dry_run": settings.get("dry_run"),
        "paused": settings.get("paused"),
        "version": os.environ.get("DIALOGUEARR_VERSION", "dev"),
        "target_lufs": settings.get("target_lufs"),
        "preferred_languages": preferred_languages(),
    }


def enqueue(path, trigger):
    with pending_lock:
        if path in pending:
            return False
        pending.add(path)
    work.put((path, trigger))
    return True


def worker():
    while True:
        path, trigger = work.get()
        urgent = trigger != "scan"
        while settings.get("paused") and trigger != "manual":
            time.sleep(10)
        try:
            # Backfill items are window-bound; the next scan re-queues whatever
            # is dropped here. Imports are never deferred.
            if not urgent and not in_window():
                continue
            delay = settings.get("import_delay")
            if urgent and delay:
                time.sleep(delay)
            if not path.is_file():
                continue
            process(path, trigger)
        except OSError as exc:
            log.error("error handling %s: %s", path.name, exc)
        finally:
            with pending_lock:
                pending.discard(path)
            work.task_done()


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




def sample_loudness(path, aidx, duration, with_pan=False, seconds=60):
    """Loudness of a 60s sample, used to backfill files processed before the
    database existed. Far cheaper than a full analysis pass and good enough for
    an indicative before/after, so it is recorded as "sampled" not "full"."""
    if duration <= seconds * 2:
        return None
    flt = f"{pan_filter()},ebur128" if with_pan else "ebur128"
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-ss", str(duration * 0.4),
             "-t", str(seconds), "-i", str(path), "-map", f"0:a:{aidx}",
             "-af", flt, "-f", "null", "-"])
    found = EBUR128_I.findall(r.stderr)
    if not found or found[-1] == "-inf":
        return None
    try:
        return float(found[-1])
    except ValueError:
        return None


def audio_streams(info):
    return [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]


def classify(info):
    """Current state of a file for the coverage table."""
    audio = audio_streams(info)
    if not audio:
        return "skipped", "no audio"
    if any((s.get("tags") or {}).get("title") == TRACK_TITLE for s in audio):
        return "done", None
    if surround_track(info):
        return "eligible", None
    return "skipped", skip_reason(info)


def classify_with_overrides(path, info):
    row = db.get_file(path) or {}
    if row.get("excluded"):
        return "excluded", "excluded by hand"
    state, reason = classify(info)
    if state == "skipped" and reason == "already has a stereo track":
        duration = float(info.get("format", {}).get("duration") or 0)
        if duration > 0 and wants_reprocessing(path, info, duration):
            return "eligible", None
    return state, reason

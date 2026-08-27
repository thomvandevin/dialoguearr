"""Runtime settings: a database override layered over the environment.

Environment variables remain the declared configuration and stay visible in
compose. Anything changed through the UI is stored in the database and wins,
so `describe()` always reports both values and the UI marks the difference.
"""

import logging
import os
import threading

import db

log = logging.getLogger("dialoguearr.settings")

# key: (env var, default, caster, group, help)
SPEC = {
    "target_lufs":      ("TARGET_LUFS", -16.0, float, "loudness",
                         "Integrated loudness target"),
    "target_lra":       ("TARGET_LRA", 8.0, float, "loudness",
                         "Loudness range target"),
    "target_tp":        ("TARGET_TP", -1.5, float, "loudness",
                         "True peak ceiling in dBTP"),
    "audio_bitrate":    ("AUDIO_BITRATE", "256k", str, "loudness",
                         "Bitrate of the new stereo track"),
    "pan_centre":       ("PAN_CENTRE", 0.9, float, "downmix",
                         "Centre channel weight, carries the dialogue"),
    "pan_front":        ("PAN_FRONT", 0.55, float, "downmix",
                         "Front left/right weight"),
    "pan_surround":     ("PAN_SURROUND", 0.25, float, "downmix",
                         "Surround left/right weight. LFE is always discarded"),
    "preferred_languages": ("PREFERRED_LANGUAGES", "", str, "selection",
                            "Comma separated, e.g. jpn,eng. Empty follows the "
                            "track each file marks default"),
    "restereo_below":   ("RESTEREO_BELOW", None, float, "selection",
                         "Also process files that already have a stereo track when it "
                         "measures below this LUFS. Empty disables"),
    "window_start":     ("WINDOW_START", 3, int, "schedule",
                         "Hour the backfill scan may start"),
    "window_end":       ("WINDOW_END", 8, int, "schedule",
                         "Hour the backfill scan must stop"),
    "scan_interval":    ("SCAN_INTERVAL", 3600, int, "schedule",
                         "Seconds between backfill scans"),
    "import_delay":     ("IMPORT_DELAY", 60, int, "schedule",
                         "Seconds to let an imported file settle"),
    "paused":           (None, False, bool, "schedule",
                         "Stop picking up new work without stopping the container"),
    "dry_run":          ("DRY_RUN", False, bool, "schedule",
                         "Report candidates without writing"),
}

_cache = {}
_lock = threading.Lock()


def _cast(caster, raw):
    if raw is None or raw == "":
        return None
    if caster is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return caster(raw)


def env_value(key):
    envvar, default, caster, _, _ = SPEC[key]
    if envvar and os.environ.get(envvar) not in (None, ""):
        try:
            return _cast(caster, os.environ[envvar])
        except (TypeError, ValueError):
            log.warning("ignoring unparsable %s=%r", envvar, os.environ[envvar])
    return default


def refresh():
    with _lock:
        _cache.clear()
        _cache.update(db.get_settings())


def get(key):
    with _lock:
        if not _cache:
            _cache.update(db.get_settings() or {"__loaded__": ""})
        raw = _cache.get(key)
    if raw is not None:
        try:
            return _cast(SPEC[key][2], raw)
        except (TypeError, ValueError):
            log.warning("ignoring unparsable override %s=%r", key, raw)
    return env_value(key)


def set(key, value):
    if key not in SPEC:
        raise KeyError(key)
    db.set_setting(key, None if value in (None, "") else value)
    refresh()


def describe():
    """Every setting with its effective value and where it came from."""
    overrides = db.get_settings()
    out = []
    for key, (envvar, _default, caster, group, helptext) in SPEC.items():
        out.append({
            "key": key, "group": group, "help": helptext,
            "env_var": envvar, "env_value": env_value(key),
            "override": overrides.get(key),
            "value": get(key),
            "type": caster.__name__,
            "overridden": key in overrides,
        })
    return out


def log_effective():
    for row in describe():
        if row["overridden"]:
            log.info("  %s = %s (override, env says %s)",
                     row["key"], row["value"], row["env_value"])
    log.info("  effective: target %s LUFS, lra %s, languages %r, window %02d:00-%02d:00%s",
             get("target_lufs"), get("target_lra"), get("preferred_languages"),
             get("window_start"), get("window_end"),
             ", PAUSED" if get("paused") else "")

#!/usr/bin/env python3
"""dialoguearr web service.

Serves the UI and the JSON the Glance widget reads, and owns the background
threads that do the actual work. GET / is the dashboard and POST / is the
Sonarr/Radarr webhook, deliberately the same path so existing webhook
configuration keeps working unchanged.
"""

import logging
import threading
import time

from flask import Flask, jsonify, render_template, request

import db
import normalize as n

log = logging.getLogger("dialoguearr.web")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------


def seed():
    """Populate the coverage table by walking the library once."""
    seen = 0
    for path in n.scan():
        info = n.probe(path)
        if not info:
            continue
        state, reason = n.classify(info)
        fields = {"library": n.library_of(path), "name": path.name,
                  "state": state, "skip_reason": reason}
        audio = n.audio_streams(info)
        source = next((s for s in audio if int(s.get("channels") or 0) >= 6), None)
        if source:
            fields.update(source_lang=n.language_of(source) or None,
                          source_codec=source.get("codec_name"),
                          source_channels=int(source.get("channels") or 0))
        try:
            fields["duration"] = float(info.get("format", {}).get("duration") or 0)
            fields["size"] = path.stat().st_size
        except (OSError, ValueError):
            pass
        db.upsert_file(path, **fields)
        seen += 1
    log.info("seed complete, %d file(s) catalogued", seen)


def backfill():
    """Sample-measure files processed before the database existed."""
    todo = [f for f in db.list_files(state="done", limit=10000)
            if f.get("input_i") is None]
    if not todo:
        return
    log.info("backfilling measurements for %d file(s)", len(todo))
    done = 0
    for row in todo:
        path = n.Path(row["path"])
        if not path.is_file():
            continue
        info = n.probe(path)
        if not info:
            continue
        audio = n.audio_streams(info)
        out_idx = next((i for i, s in enumerate(audio)
                        if (s.get("tags") or {}).get("title") == n.TRACK_TITLE), None)
        src_idx = next((i for i, s in enumerate(audio)
                        if int(s.get("channels") or 0) >= 6), None)
        if out_idx is None or src_idx is None:
            continue
        duration = float(info.get("format", {}).get("duration") or 0)
        out = n.sample_loudness(path, out_idx, duration)
        src = n.sample_loudness(path, src_idx, duration, with_pan=True)
        if out is None and src is None:
            continue
        db.upsert_file(path, input_i=src, output_i=out, measurement="sampled")
        done += 1
    log.info("backfill complete, %d file(s) measured", done)


def catalogue():
    """One-off startup pass, then keep the coverage table fresh."""
    while True:
        try:
            seed()
            backfill()
        except Exception:
            log.exception("catalogue pass failed")
        time.sleep(max(n.SCAN_INTERVAL, 3600) * 6)


def backfill_scan():
    """The nightly window scan, unchanged in behaviour."""
    while True:
        if n.in_window():
            queued = 0
            for path in n.scan():
                info = n.probe(path)
                if info and n.surround_track(info) and n.enqueue(path, "scan"):
                    queued += 1
            if queued:
                log.info("backfill scan queued %d file(s)", queued)
        time.sleep(n.SCAN_INTERVAL)


def start_background():
    db.init()
    n.cleanup_temp()
    for target in (n.worker, backfill_scan, catalogue):
        threading.Thread(target=target, daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/")
def webhook():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return "ok", 200
    event = payload.get("eventType", "?")
    if event == "Test":
        log.info("webhook test received")
        return "ok", 200
    for path in dict.fromkeys(n.media_paths(payload)):
        if n.enqueue(path, "webhook"):
            log.info("webhook (%s) queued %s", event, path.name)
    return "ok", 200


@app.get("/api/summary")
def api_summary():
    return jsonify(db.summary())


@app.get("/api/status")
def api_status():
    return jsonify(n.status())


@app.get("/api/files")
def api_files():
    return jsonify(db.list_files(
        state=request.args.get("state") or None,
        query=request.args.get("q") or None,
        limit=min(int(request.args.get("limit", 500)), 5000)))


@app.get("/api/runs")
def api_runs():
    return jsonify(db.list_runs(limit=min(int(request.args.get("limit", 100)), 1000)))


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

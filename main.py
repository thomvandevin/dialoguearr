#!/usr/bin/env python3
"""Entry point: start the background workers, then serve the app."""

import os

from waitress import serve

import app as web

PORT = int(os.environ.get("PORT", "8080"))

if __name__ == "__main__":
    web.start_background()
    web.log.info("dialoguearr listening on port %d", PORT)
    serve(web.app, host="0.0.0.0", port=PORT, threads=8, _quiet=True)

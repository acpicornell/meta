#!/usr/bin/env python3
"""Tiny no-cache static server for local dev.

`python -m http.server` doesn't set Cache-Control, so every modern
browser is free to use its memory cache for app.js / style.css across
reloads. The result: you edit a file, reload, and still see the old
build until you do a hard reload (cmd-shift-R) or open a private tab.

This wrapper sets `Cache-Control: no-store` on every response so a
normal reload always picks up the latest bytes. It's the local
counterpart to the Cloudflare `_headers` rules — same semantics, just
applied by Python instead of the CDN.

Run from the `web/` directory:

    python3 dev_server.py            # listens on 0.0.0.0:8766
    python3 dev_server.py 8000       # custom port
"""

from __future__ import annotations
import http.server
import socketserver
import sys


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    with socketserver.TCPServer(("", port), NoCacheHandler) as srv:
        srv.allow_reuse_address = True
        print(f"Serving with Cache-Control: no-store on http://localhost:{port}/")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nbye")


if __name__ == "__main__":
    main()

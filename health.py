from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _HealthHandler(BaseHTTPRequestHandler):
    def _health_paths(self) -> bool:
        return self.path.split("?", 1)[0] in ("/", "/health")

    def do_GET(self) -> None:
        if not self._health_paths():
            self.send_response(404)
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        if not self._health_paths():
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        pass


def start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="health-server",
    )
    thread.start()

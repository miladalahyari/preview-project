#!/usr/bin/env python3
"""A dependency-free HTTP application for disposable preview environments."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def response(path):
    if path in {"/health", "/healthz"}:
        return 200, {"status": "ok"}
    if path == "/":
        return 200, {"application": "example-app", "company": "Company",
                     "message": "Your preview is running.",
                     "version": os.environ.get("APP_VERSION", "development"),
                     "source": os.environ.get("SOURCE_URL", "https://github.com/OWNER/preview-project")}
    return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, payload = response(self.path.split("?", 1)[0])
        body = (json.dumps(payload) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Avoid logging user-supplied paths/query strings.
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"example-app listening on {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

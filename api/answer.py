"""Vercel serverless function: POST /api/answer {"question": "..."}."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("VECTOR_BACKEND", "numpy")  # Chroma is not bundled here

from rag.api import handle  # noqa: E402

MAX_BODY = 4096


class handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(400, {"error": "invalid_input"})
        self._send(*handle(self.rfile.read(length)))

    def do_GET(self):
        self._send(405, {"error": "use POST"})

    def log_message(self, *args):  # no request logging from the server itself
        pass

"""Stub Anthropic API server for e2e resilience testing.

Injects a configurable sequence of HTTP errors before forwarding to the real
Anthropic API. Reads ANTHROPIC_API_KEY to proxy real requests.

Usage:
    # Return 429 twice, then 529 once, then pass through to real API:
    STUB_ERRORS=429,529 python tests/stub_anthropic.py &
    STUB_PID=$!
    ANTHROPIC_BASE_URL=http://localhost:4999 beidou run "..."
    kill $STUB_PID

ENV:
    STUB_ERRORS   Comma-separated HTTP status codes to return before proxying.
                  Special: "exhaust" repeats STUB_MAX_FAIL times to trigger
                  llm_exhausted. Default: "429,529"
    STUB_PORT     Port to listen on. Default: 4999
    STUB_MAX_FAIL How many errors to inject for "exhaust" mode. Default: 5
    STUB_VERBOSE  Set to "1" to print each request/response.
"""
from __future__ import annotations

import http.server
import io
import json
import os
import sys
import threading
import time
from http import HTTPStatus

import httpx

# STUB_REAL_BASE: where to proxy passing-through requests.
# Falls back to ANTHROPIC_BASE_URL (the profile-configured proxy) then real Anthropic.
REAL_BASE = (os.environ.get("STUB_REAL_BASE")
             or os.environ.get("ANTHROPIC_BASE_URL")
             or "https://api.anthropic.com")
PORT = int(os.environ.get("STUB_PORT", 4999))
VERBOSE = os.environ.get("STUB_VERBOSE", "0") == "1"
MAX_FAIL = int(os.environ.get("STUB_MAX_FAIL", 5))

_raw_errors = os.environ.get("STUB_ERRORS", "429,529")
if _raw_errors == "exhaust":
    ERROR_SEQUENCE: list[int] = [429] * MAX_FAIL
else:
    ERROR_SEQUENCE = [int(x.strip()) for x in _raw_errors.split(",") if x.strip()]

# Thread-safe counter
_lock = threading.Lock()
_request_count = 0

# Stats for test assertions
stats: dict = {"injected": [], "proxied": 0, "errors": []}


_ERROR_BODIES = {
    429: {"type": "error", "error": {"type": "rate_limit_error",
          "message": "Rate limit exceeded — stub injected 429"}},
    529: {"type": "error", "error": {"type": "overloaded_error",
          "message": "API overloaded — stub injected 529"}},
    500: {"type": "error", "error": {"type": "api_error",
          "message": "Internal server error — stub injected 500"}},
    503: {"type": "error", "error": {"type": "api_error",
          "message": "Service unavailable — stub injected 503"}},
}


class StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default access log
        if VERBOSE:
            sys.stderr.write(f"[stub] {fmt % args}\n")

    def do_POST(self):
        global _request_count
        with _lock:
            idx = _request_count
            _request_count += 1

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        # Inject error?
        if idx < len(ERROR_SEQUENCE):
            code = ERROR_SEQUENCE[idx]
            err_body = json.dumps(_ERROR_BODIES.get(code, {"error": str(code)})).encode()
            with _lock:
                stats["injected"].append(code)
            if VERBOSE:
                sys.stderr.write(f"[stub] req#{idx} → injecting {code}\n")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            # Anthropic 529 uses this header
            if code == 529:
                self.send_header("X-Error-Type", "overloaded")
            self.end_headers()
            self.wfile.write(err_body)
            return

        # Proxy to real API — forward auth and anthropic headers verbatim from
        # the client request so we don't have to know how the key is stored.
        headers = {
            "x-api-key": self.headers.get("x-api-key", os.environ.get("ANTHROPIC_API_KEY", "")),
            "anthropic-version": self.headers.get("anthropic-version", "2023-06-01"),
            "content-type": "application/json",
        }
        for k, v in self.headers.items():
            k_lower = k.lower()
            if k_lower.startswith("anthropic-") and k_lower != "anthropic-version":
                headers[k_lower] = v

        key_len = len(headers.get("x-api-key", ""))
        if VERBOSE:
            sys.stderr.write(f"[stub] req#{idx} → proxying (x-api-key len={key_len})\n")

        with _lock:
            stats["proxied"] += 1

        url = f"{REAL_BASE}{self.path}"
        try:
            resp = httpx.post(url, content=body, headers=headers, timeout=120)
            resp_body = resp.content
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() in ("content-type", "content-length", "anthropic-request-id"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as exc:
            with _lock:
                stats["errors"].append(str(exc))
            err = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def do_GET(self):
        # /stats endpoint — return stub state as JSON (used by test scripts to assert)
        if self.path == "/stub/stats":
            body = json.dumps(stats).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    print(f"[stub] Starting on port {PORT}", file=sys.stderr)
    print(f"[stub] Error sequence: {ERROR_SEQUENCE}", file=sys.stderr)
    print(f"[stub] Then proxying to {REAL_BASE}", file=sys.stderr)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), StubHandler)
    # Signal readiness on stdout so the test runner can synchronise
    print(f"STUB_READY port={PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n[stub] Stats: {json.dumps(stats)}", file=sys.stderr)


if __name__ == "__main__":
    main()

"""Entrypoint: start the forge, register with the env server, serve.

Contract with the kit's env server (program/env/server.mjs):
  - Listen on a port (``PORT`` env, else an ephemeral one).
  - POST {"port": N} to $MAKEFUN_ENV_URL/register
    (200 ok; 409 = already registered, keep waiting; 400/405 = contract
    error, stop).
  - Serve until killed. The env server drives the checks and exports
    state.json / perf.json itself; the forge needs no state endpoint.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge import httpd  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.bind(("::", 0))
        return sock.getsockname()[1]


def _post_json(url: str, payload: dict) -> tuple[int, bytes]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    env_url = os.environ.get("MAKEFUN_ENV_URL", "")
    port = int(os.environ.get("PORT", "0") or 0)
    if port == 0:
        port = _free_port()
    server = httpd.serve(port)
    print(f"forge listening on [::]:{port}", flush=True)
    if env_url:
        deadline = time.time() + 60
        delay = 0.5
        while time.time() < deadline:
            status, _ = _post_json(env_url.rstrip("/") + "/register",
                                   {"port": port})
            if status == 200:
                print("registered with env server", flush=True)
                break
            if status == 409:
                print("already registered; waiting", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 5)
                continue
            print(f"register rejected with HTTP {status}", file=sys.stderr,
                  flush=True)
            return 1
        else:
            print("register timed out", file=sys.stderr, flush=True)
            return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

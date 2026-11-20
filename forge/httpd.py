"""HTTP front door: dual-stack server, request routing, auth gate.

Routes:
  GET  /healthz                    -> 200 "ok" (no auth)
  /api/*                           -> REST v3
  POST /graphql                    -> GraphQL
  POST /mcp                        -> MCP JSON-RPC
  /{owner}/{repo}.git/*            -> git smart HTTP + dumb HTTP
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import gitapi, gql, mcpapi, model
from .restapi import Request, RestApi

NOT_FOUND = {"message": "Not Found",
             "documentation_url": "https://docs.github.com/rest"}
NEEDS_AUTH = {"message": "Requires authentication",
              "documentation_url": "https://docs.github.com/rest"}


class ForgeServer:
    """Owns the forge state and the three API surfaces."""

    def __init__(self):
        self.forge = model.ForgeState()
        self.rest = RestApi(self.forge)
        self.git = gitapi.GitService(self.forge)

    # -- REST ---------------------------------------------------------------

    def rest_call(self, method: str, raw_path: str, headers: dict,
                  body: bytes) -> tuple[int, bytes, str, dict]:
        # The kit mounts REST under /api/v3/; accept /api/ as well.
        path = raw_path
        for prefix in ("/api/v3", "/api"):
            if path == prefix:
                path = "/"
                break
            if path.startswith(prefix + "/"):
                path = path[len(prefix):]
                break
        if not path.startswith("/"):
            path = "/" + path
        req = Request(method, path, headers, body)
        status, payload, ctype = self.rest.dispatch(req)
        extra = {}
        if self.rest.link_header:
            extra["Link"] = self.rest.link_header
        return status, payload, ctype, extra

    # -- GraphQL ------------------------------------------------------------

    def graphql_call(self, headers: dict, body: bytes) -> tuple[int, bytes]:
        if not gitapi.authorised(headers):
            return 401, json.dumps(NEEDS_AUTH).encode()
        try:
            payload = json.loads(body.decode())
        except (ValueError, UnicodeDecodeError):
            return 400, json.dumps({"errors": [{"message": "invalid JSON"}]}).encode()
        if isinstance(payload, list):
            # Batched operations: one result object per operation, in order.
            results = []
            for item in payload:
                _status, data = self._graphql_one(item)
                results.append(json.loads(data.decode()))
            return 200, json.dumps(results).encode()
        return self._graphql_one(payload)

    def _graphql_one(self, payload) -> tuple[int, bytes]:
        if not isinstance(payload, dict):
            return 400, json.dumps({"errors": [{"message": "invalid JSON"}]}).encode()
        query = payload.get("query", "")
        variables = payload.get("variables") or {}
        try:
            tokens = gql.Lexer(query).tokens()
            doc = gql.Parser(tokens).document()
        except ValueError as exc:
            return 200, json.dumps({"errors": [{"message": str(exc)}]}).encode()
        engine = gql.Engine(self.forge, variables)
        if "__schema" in query or "__type" in query:
            return 200, json.dumps(gql.introspection(engine, doc)).encode()
        operation = next((d for d in doc["definitions"]
                          if d["kind"] == "operation"), None)
        if operation is None:
            return 200, json.dumps({"data": {}}).encode()
        if operation["operation"] == "mutation":
            result = gql.execute_mutation(engine, doc)
        else:
            result = engine.run(doc)
        return 200, json.dumps(result).encode()

    # -- MCP ----------------------------------------------------------------

    def mcp_call(self, headers: dict, body: bytes) -> tuple[int, bytes]:
        if not gitapi.authorised(headers):
            return 401, json.dumps(NEEDS_AUTH).encode()
        return mcpapi.handle(self.forge, body)

    # -- git ----------------------------------------------------------------

    def git_call(self, method: str, raw_path: str, headers: dict,
                 body: bytes) -> tuple[int, bytes, str]:
        parsed = urlparse(raw_path)
        path = unquote(parsed.path)
        query = parsed.query
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or not parts[1].endswith(".git"):
            return 404, json.dumps(NOT_FOUND).encode(), "application/json"
        owner, repo_name = parts[0], parts[1][:-4]
        repo = self.forge.find_repo(owner, repo_name)
        if repo is None:
            return 404, json.dumps(NOT_FOUND).encode(), "application/json"
        if not gitapi.authorised(headers):
            return 401, json.dumps(NEEDS_AUTH).encode(), "application/json"
        rest = parts[2:]
        if method == "GET" and rest == ["info", "refs"]:
            from urllib.parse import parse_qs
            service = parse_qs(query).get("service", [""])[0]
            if service:
                protocol = 2 if headers.get("git-protocol", "") \
                    .strip() == "version=2" else 0
                ctype = ("application/x-git-receive-pack-advertisement"
                         if service == "git-receive-pack"
                         else "application/x-git-upload-pack-advertisement")
                return 200, self.git.info_refs(repo, service, protocol), \
                    ctype
            return 200, self.git.dumb_refs(repo), "text/plain"
        if method == "GET" and rest == ["HEAD"]:
            branch = repo.default_branch
            return 200, f"ref: refs/heads/{branch}\n".encode(), "text/plain"
        if method == "GET" and rest == ["objects", "info", "packs"]:
            return 200, b"", "text/plain"
        if method == "GET" and len(rest) == 3 and rest[0] == "objects" \
                and len(rest[1]) == 2 and len(rest[2]) == 38:
            oid = rest[1] + rest[2]
            blob = repo.fetch_object(oid)
            if blob is None:
                return 404, b"not found", "text/plain"
            return 200, blob, "application/octet-stream"
        if method == "POST" and rest == ["git-upload-pack"]:
            service = self._detect_upload_version(body)
            if service == 2:
                return 200, self.git.upload_v2(repo, body), \
                    "application/x-git-upload-pack-result"
            return 200, self.git.upload_v0(repo, body), \
                "application/x-git-upload-pack-result"
        if method == "POST" and rest == ["git-receive-pack"]:
            return 200, self.git.receive(repo, body), \
                "application/x-git-receive-pack-result"
        return 404, json.dumps(NOT_FOUND).encode(), "application/json"

    @staticmethod
    def _detect_upload_version(body: bytes) -> int:
        """Protocol v2 starts with a ``command=`` pkt-line."""
        if len(body) >= 4:
            try:
                size = int(body[:4], 16)
                if size > 4 and body[4:4 + size - 4].startswith(b"command="):
                    return 2
            except ValueError:
                pass
        return 0


def make_handler(server: ForgeServer):
    _counter = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence access logs
            pass

        def _headers(self) -> dict:
            return {k.lower(): v for k, v in self.headers.items()}

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _send(self, status: int, body: bytes, ctype: str,
                  extra: dict | None = None) -> None:
            _counter["n"] += 1
            request_id = "req-%08d" % _counter["n"]
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-github-request-id", request_id)
            self.send_header("x-oauth-scopes", "repo, user")
            self.send_header("x-accepted-oauth-scopes", "repo, user")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _route(self, method: str) -> None:
            path = urlparse(self.path).path
            headers = self._headers()
            body = self._read_body() if method in ("POST", "PUT", "PATCH",
                                                   "DELETE") else b""
            if path == "/healthz":
                self._send(200, b"ok", "text/plain")
                return
            # GraphQL and MCP first: /api/graphql also matches the
            # /api/ REST prefix, and must not fall through to REST.
            if path in ("/graphql", "/api/graphql") and method == "POST":
                status, payload = server.graphql_call(headers, body)
                self._send(status, payload, "application/json")
                return
            if path == "/mcp" and method == "POST":
                status, payload = server.mcp_call(headers, body)
                self._send(status, payload, "application/json")
                return
            if path.startswith("/api/v3/") or path.startswith("/api/") \
                    or path in ("/api", "/api/v3"):
                status, payload, ctype, extra = server.rest_call(
                    method, self.path, headers, body)
                self._send(status, payload, ctype, extra)
                return
            if ".git" in path:
                status, payload, ctype = server.git_call(
                    method, self.path, headers, body)
                self._send(status, payload, ctype)
                return
            self._send(404, json.dumps(NOT_FOUND).encode(),
                       "application/json")

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

        def do_HEAD(self):
            self._route("GET")

    return Handler


class DualStackServer(ThreadingHTTPServer):
    """Threaded HTTP server on IPv6 any (``::``), IPv4-mapped included."""
    address_family = socket.AF_INET6
    daemon_threads = True
    request_queue_size = 128

    def server_bind(self):
        # IPV6_V6ONLY must be cleared before bind, or the setsockopt
        # fails with EINVAL. The socket exists here but is not bound yet.
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def serve(port: int) -> DualStackServer:
    """Dual-stack server on ``::`` (IPv6 any, IPv4-mapped included)."""
    server = ForgeServer()
    httpd = DualStackServer(("::", port), make_handler(server))
    httpd.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return httpd

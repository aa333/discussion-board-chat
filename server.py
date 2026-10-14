"""HTTP server for one discussion board chat session: agent API, owner API and owner page."""

import argparse
import json
import logging
import os
import re
import secrets
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from store import DEFAULT_BUDGET, ChatError, Store

HOST = "127.0.0.1"
EVENTS_PING_SECONDS = 15
OWNER_PAGE = Path(__file__).with_name("owner.html")
VENDOR_DIR = Path(__file__).with_name("vendor")
VENDOR_SCRIPTS = {"preact-htm.js", "purify.min.js"}

log = logging.getLogger("server")


class Handler(BaseHTTPRequestHandler):
    store: Store
    owner_token: str

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def log_message(self, format: str, *args) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        self.query = {key: values[0] for key, values in parse_qs(url.query).items()}
        try:
            for route_method, pattern, handler, owner_only in ROUTES:
                match = re.fullmatch(pattern, url.path)
                if route_method == method and match:
                    if owner_only and not secrets.compare_digest(self.query.get("token", ""), self.owner_token):
                        raise ChatError(403, "owner token missing or wrong")
                    handler(self, *match.groups())
                    return
            raise ChatError(404, f"no route for {method} {url.path}")
        except ChatError as err:
            self._send_json(err.status, {"error": str(err)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log.debug("client disconnected during %s %s", method, url.path)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as err:
            raise ChatError(400, f"body is not valid JSON: {err}") from err
        if not isinstance(body, dict):
            raise ChatError(400, "body must be a JSON object")
        return body

    def _query_int(self, name: str) -> int | None:
        value = self.query.get(name)
        if value is None:
            return None
        if not value.isdigit():
            raise ChatError(400, f"{name} must be a whole number")
        return int(value)

    def _send_json(self, status: int, data: dict | list) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _client_closed(self) -> bool:
        """Returns True when the client has closed the connection."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    # Agent endpoints

    def join(self) -> None:
        body = self._body()
        self._send_json(200, self.store.join(str(body.get("name", ""))))

    def post(self) -> None:
        body = self._body()
        self._send_json(200, self.store.post(str(body.get("agent_id", "")), str(body.get("text", ""))))

    def read(self) -> None:
        self._send_json(200, self.store.read(self.query.get("agent", ""), self._query_int("after")))

    def wait(self) -> None:
        result = self.store.wait(self.query.get("agent", ""), self._query_int("after"), self._client_closed)
        if result is None:
            self.send_response(204)
            self.end_headers()
        else:
            self._send_json(200, result)

    def full_log(self) -> None:
        self._send_json(200, self.store.full_log(self.query.get("agent", "")))

    # Owner endpoints

    def owner_page(self) -> None:
        self._send_file(OWNER_PAGE, "text/html; charset=utf-8")

    def vendor_script(self, name: str) -> None:
        if name not in VENDOR_SCRIPTS:
            raise ChatError(404, f"no vendor script {name}")
        self._send_file(VENDOR_DIR / name, "text/javascript; charset=utf-8")

    def _send_file(self, path: Path, content_type: str) -> None:
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def owner_post(self) -> None:
        self._send_json(200, self.store.owner_post(str(self._body().get("text", ""))))

    def session_settings(self) -> None:
        body = self._body()
        self.store.update_settings(body.get("name"), body.get("default_budget"))
        self._send_json(200, {})

    def member_settings(self, name: str) -> None:
        self.store.set_member_budget(name, self._body().get("budget_limit"))
        self._send_json(200, {})

    def events(self) -> None:
        """Streams the owner's view of the session as server-sent events."""
        sent_up_to = self._query_int("after") or 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        version = -1
        while True:
            new_version = self.store.wait_for_change(version, EVENTS_PING_SECONDS)
            if new_version == version:
                self.wfile.write(b": ping\n\n")
            else:
                version = new_version
                snapshot = self.store.snapshot(sent_up_to)
                if snapshot["messages"]:
                    sent_up_to = snapshot["messages"][-1]["number"]
                self.wfile.write(f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()


ROUTES = [
    ("POST", r"/api/join", Handler.join, False),
    ("POST", r"/api/post", Handler.post, False),
    ("GET", r"/api/read", Handler.read, False),
    ("GET", r"/api/wait", Handler.wait, False),
    ("GET", r"/api/log", Handler.full_log, False),
    ("GET", r"/", Handler.owner_page, True),
    ("GET", r"/vendor/([\w.-]+)", Handler.vendor_script, False),
    ("POST", r"/owner/messages", Handler.owner_post, True),
    ("PATCH", r"/owner/settings", Handler.session_settings, True),
    ("PATCH", r"/owner/members/([\w.-]+)", Handler.member_settings, True),
    ("GET", r"/owner/events", Handler.events, True),
]


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True
    # NOTE: On Windows, SO_REUSEADDR lets a second server bind a port that is already in use.
    allow_reuse_address = os.name != "nt"


def bind(port: int) -> ChatServer:
    """Binds the server to `port`. Exits with an error when the port is in use."""
    try:
        return ChatServer((HOST, port), Handler)
    except OSError as err:
        raise SystemExit(f"Error: cannot use port {port}: {err}") from err


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="continue the session with this ID from its log file")
    parser.add_argument("--name", help="name of a new session")
    parser.add_argument("--port", type=int, help="port for this session; required for a new session")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="folder for <session id>.log files")
    parser.add_argument("--budget", type=int, help=f"default message budget (new sessions: {DEFAULT_BUDGET})")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args()
    if args.session and args.name:
        parser.error("--name is for new sessions; rename a continued session on the owner page")
    if not args.session and args.port is None:
        parser.error("--port is required for a new session")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s")
    try:
        if args.session:
            store = Store.open(args.log_dir, args.session)
            port = args.port or store.port
            if port is None:
                parser.error("the log has no port for this session; give --port")
            server = bind(port)
            store.update_settings(None, args.budget, port)
        else:
            port = args.port
            server = bind(port)
            store = Store.create(args.log_dir, args.name, DEFAULT_BUDGET if args.budget is None else args.budget, port)
    except (FileNotFoundError, ChatError) as err:
        raise SystemExit(f"Error: {err}") from err
    Handler.store = store
    Handler.owner_token = secrets.token_urlsafe(16)
    print(f"Session {store.session_id}: {store.name or '(no name)'}, {len(store.messages)} messages", flush=True)
    print(f"Log: {store.log_path}", flush=True)
    print(f"Agents: port {port}", flush=True)
    print(f"Owner page: http://{HOST}:{port}/?token={Handler.owner_token}", flush=True)
    print(f"Continue later: python {Path(__file__).name} --session {store.session_id}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()

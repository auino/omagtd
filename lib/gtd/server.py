"""The HTTP front door: the same CLI, one request at a time.

Why this is small
-----------------
Every mutating command already funnels through :func:`gtd.cli.main`, which
opens the database, journals undo, runs one handler and sweeps. A request here
does exactly the same thing against the same file, with the output captured in
a buffer instead of a terminal. There is no service layer, no second copy of
the schema and no sync protocol: the server is the *only* writer, so the
desktop, the phone and a terminal are all just reading the same rows.

That is the whole trick. Changes do not travel between devices; each device
re-reads. :class:`Hub` only exists to make that re-read happen promptly — it
watches ``PRAGMA data_version`` on a spare connection and pushes a revision
number to Server-Sent Event subscribers when *any* connection commits.

Authentication is one shared password, because this is a personal system: the
password lives in a file, a client trades it once for an opaque session cookie,
and after that request no password crosses the wire. The network boundary is
expected to be Tailscale or a TLS reverse proxy. Bind to loopback unless you
know what is on the other side of the interface.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import queue
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import theme
from .db import Store, data_home, db_path

# A capture is a line of text; a body larger than this is a mistake or an
# attack, not a task.
MAX_BODY = 1 << 20

# How often the watcher asks sqlite whether another connection committed.
WATCH_PERIOD = 0.75

# An idle EventSource connection gets a comment this often, which keeps both
# the socket and any intermediary awake.
SSE_KEEPALIVE = 20.0

# Commands run one at a time. They share the process-wide date configuration
# and they all write to one sqlite file; serialising is both the simplest
# correctness argument and the shape of the system (one writer).
COMMAND_LOCK = threading.Lock()

# Commands that only read. They are safe to expose and cheap to repeat.
READ_COMMANDS = {
    "list", "search", "show", "state", "focus", "today", "bar", "tree",
    "subtree", "stats", "log", "calendar", "cal",
    "agenda", "quick", "repeat-preview", "complete", "projects", "areas",
    "tags", "context", "triage",
}

# Commands that change the database. They run one at a time server-side, so
# "last command wins" is the only merge rule the system needs. `settings`
# is here because `gtd settings set` writes, even though the plain form reads.
WRITE_COMMANDS = {
    "capture", "clarify", "edit", "engage", "done", "drop", "start", "stop",
    "resume", "flag", "unflag", "archive", "unarchive", "defer", "postpone",
    "roll", "wait", "someday", "undo", "project", "area", "tag", "review",
    "trash", "restore", "purge", "settings",
}

ALLOWED_COMMANDS = READ_COMMANDS | WRITE_COMMANDS

# Deliberately absent: `open` (xdg-open would run on the server), `attach`,
# `unattach`, `import*` (they read local files), `export*` (they write local
# files), `notify` (it raises a desktop notification on the host), `db` (it
# discloses the path) and `serve` (recursion).
HOST_BOUND_COMMANDS = {
    "open", "attach", "unattach", "import", "import-lines", "import-txt",
    "export", "export-day", "day-export", "export-context", "context-export",
    "notify", "db", "serve", "init",
}

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


@dataclass
class Config:
    db: Path
    password: str
    web_root: Path
    verbose: bool = False
    notify_attempts: bool = True


# ------------------------------------------------------------------ sessions

SESSION_COOKIE = "omagtd_session"
# A session is a standing invitation: it outlives the page that earned it, but
# it lives in server memory only, so a restart — which is how the password
# changes — closes every door it opened.
SESSION_TTL = 7 * 24 * 3600


class Sessions:
    """Opaque, single-server tickets that replace the password on the wire.

    The shared password is a secret this machine holds; a session is not. A
    log reader or a rogue same-origin listener may at most see one — and it
    buys nothing more than *this server's* current state, until it expires,
    the server restarts, or the client logs out.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._tokens[token] = time.time() + SESSION_TTL
        return token

    def is_valid(self, token: str) -> bool:
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None:
                return False
            if expiry < time.time():
                del self._tokens[token]
                return False
            self._tokens[token] = time.time() + SESSION_TTL  # sliding
            return True

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, expiry in self._tokens.items() if expiry < now]:
            del self._tokens[token]


# ------------------------------------------------------------------ changes


class Hub:
    """Fan out "something committed" to every listening browser."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[queue.Queue] = set()
        self._rev = 0
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    @property
    def revision(self) -> int:
        with self._lock:
            return self._rev

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=16)
        with self._lock:
            self._clients.add(channel)
            revision = self._rev
        # Tell a fresh subscriber where it stands; it refreshes immediately and
        # then only on later revisions.
        try:
            channel.put_nowait(revision)
        except queue.Full:
            pass
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(channel)

    def publish(self) -> int:
        with self._lock:
            self._rev += 1
            revision = self._rev
            clients = list(self._clients)
        for channel in clients:
            try:
                channel.put_nowait(revision)
            except queue.Full:
                # A client that cannot keep up will refresh on its next poll
                # anyway; dropping the edge is the right trade.
                pass
        return revision

    def watch(self, database: Path) -> None:
        """Start the spare-connection watcher, if it is not running."""
        if self._watcher is not None:
            return
        self._watcher = threading.Thread(
            target=self._watch, args=(database,), name="gtd-watch", daemon=True)
        self._watcher.start()

    def _watch(self, database: Path) -> None:
        try:
            conn = sqlite3.connect(str(database), timeout=5.0,
                                   isolation_level=None)
        except sqlite3.Error:
            return
        try:
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            while not self._stop.is_set():
                time.sleep(WATCH_PERIOD)
                try:
                    current = conn.execute("PRAGMA data_version").fetchone()[0]
                except sqlite3.Error:
                    continue
                if current != version:
                    version = current
                    self.publish()
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------ command


def validate_argv(argv) -> str | None:
    """Reject anything the HTTP surface must not run. None means "fine"."""
    if not isinstance(argv, list) or not argv:
        return "argv must be a non-empty list"
    if len(argv) > 64:
        return "argv is too long"
    if not all(isinstance(part, str) for part in argv):
        return "argv must contain only strings"

    command = argv[0]
    if command in HOST_BOUND_COMMANDS:
        return f"'{command}' runs on the host, not over HTTP"
    if command not in ALLOWED_COMMANDS:
        return f"unknown command: {command}"

    for part in argv:
        if part in ("--db", "--profile") or part.startswith(("--db=", "--profile=")):
            return "the database is chosen by the server"
        if part in ("-h", "--help", "--version"):
            return "help is not available over HTTP"
        if "\x00" in part or len(part) > 8192:
            return "invalid argument"
    return None


def run_command(argv: list[str], database: Path) -> tuple[int, dict]:
    """Run one CLI command with its output captured, and return (exit, json)."""
    from . import cli

    buffer = io.StringIO()
    full = ["--json", "--db", str(database), *argv]
    try:
        with COMMAND_LOCK:
            code = cli.main(full, stream=buffer, color=False)
    except SystemExit as exc:  # out.fail() and argparse both land here
        code = exc.code if isinstance(exc.code, int) else 2
    except Exception as exc:  # a broken command must not kill the server
        return 1, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    text = buffer.getvalue()
    payload: dict | None = None
    if text.strip():
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        payload = {"ok": code == 0}
        if text.strip():
            payload["text"] = text.strip()
    return code, payload


def status_for(code: int, payload: dict) -> int:
    if code == 0:
        return 200
    if code == 2:
        return 422 if payload.get("error") else 400
    return 500


# A page that keeps retrying a rejected password must not turn the desktop
# into an alarm, so one client cannot repeat the same outcome inside this
# window. Different outcomes (accepted then refused) still announce.
ATTEMPT_QUIET = 10.0
_attempt_gate = threading.Lock()
_attempt_seen: dict[tuple[str, bool], float] = {}


def claim_attempt(client: str, accepted: bool) -> bool:
    """True when this outcome is worth announcing, once per quiet window."""
    now = time.monotonic()
    key = (client, accepted)
    with _attempt_gate:
        if len(_attempt_seen) > 64:
            cutoff = now - ATTEMPT_QUIET
            for stale in [k for k, at in _attempt_seen.items() if at < cutoff]:
                del _attempt_seen[stale]
        previous = _attempt_seen.get(key)
        if previous is not None and now - previous < ATTEMPT_QUIET:
            return False
        _attempt_seen[key] = now
        return True


def attempt_message(accepted: bool, client: str, agent: str = "") -> tuple[str, str]:
    """What the desktop shows for one attempt; the secret is not an input."""
    title = "OmaGTD unlocked" if accepted else "OmaGTD unlock refused"
    detail = f"from {client}"
    agent = " ".join(agent.split())
    if agent:
        detail += f" · {agent[:80]}"
    return title, detail


def notify_attempt(accepted: bool, client: str, agent: str = "") -> None:
    """Announce an unlock attempt on the desktop that runs the server.

    This is how the person at the machine learns that the phone just
    connected, or that something on the network tried the door. Delivery is
    best effort and happens off the request thread: a missing notification
    daemon must not slow a login.
    """
    if not claim_attempt(client, accepted):
        return

    title, detail = attempt_message(accepted, client, agent)

    def deliver() -> None:
        try:
            from .cli import _notify_send
            _notify_send(title, detail)
        except Exception:
            pass

    threading.Thread(target=deliver, daemon=True).start()


# ------------------------------------------------------------------ handler


class Handler(BaseHTTPRequestHandler):
    """One CLI invocation per request, plus the app shell and its change feed."""

    server_version = "omagtd"
    protocol_version = "HTTP/1.1"

    config: Config = Config(Path("."), "", Path("."))
    hub: Hub = Hub()
    sessions: Sessions = Sessions()

    # ---- plumbing

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.config.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes,
              content_type: str = "application/json; charset=utf-8",
              *, cache: str = "no-store", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, *, cache: str = "no-store",
              extra: dict | None = None) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True,
                          default=str).encode("utf-8")
        self._send(status, body, cache=cache, extra=extra)

    # ---- auth

    def _supplied_password(self) -> str:
        """The password presented with this request, when it came in by header.

        Deliberately *not* the query string: anything in a URL is something
        every access log, proxy and browser history gets to keep, and this is
        the one password that opens everything. Browsers that cannot set a
        header get a session cookie instead (see :meth:`_session_cookie`).
        """
        header = self.headers.get("Authorization") or ""
        lowered = header.lower()
        if lowered.startswith("bearer "):
            return header[7:].strip()
        if lowered.startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
            except (ValueError, binascii.Error):
                return ""
            return decoded.split(":", 1)[1] if ":" in decoded else decoded
        direct = self.headers.get("X-GTD-Password")
        if direct:
            return direct
        return ""

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == SESSION_COOKIE:
                return value
        return ""

    def _authorized(self) -> bool:
        supplied = self._supplied_password()
        if supplied and secrets.compare_digest(supplied, self.config.password):
            return True
        token = self._cookie_token()
        # No expected password means no one may authenticate at all.
        if token and self.config.password and self.sessions.is_valid(token):
            return True
        return False

    def _csrf_ok(self) -> bool:
        """A cookie rides with a request automatically, so a page on another
        origin could ride it into a write. Same-origin clients pass; a
        cross-site fetch is refused. Clients without a browser (curl, the
        desktop) send no Origin at all and authenticate by header instead."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site == "cross-site":
            return False
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if origin:
            proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
            expected = ("https" if proto == "https" else "http") + "://" \
                       + (self.headers.get("Host") or "").strip().lower()
            if origin.lower() != expected:
                return False
        return True

    def _is_https(self) -> bool:
        # The proxy (Tailscale, nginx) says the external scheme. Over a plain
        # local bind there is no proxy, so the cookie must stay usable over
        # http:// without the flag that would only help it over https://.
        return (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https"

    def _session_cookie(self, token: str, *, expired: bool = False) -> str:
        base = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        if expired:
            return base + "; Max-Age=0"
        if self._is_https():
            base += "; Secure"
        return base + f"; Max-Age={SESSION_TTL}"

    # ---- routing

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)

        if path == "/healthz":
            self._json(200, {"ok": True, "app": "omagtd"})
            return
        if path == "/theme.css":
            self._theme()
            return
        if path == "/events":
            self._events()
            return
        if path == "/v1/state":
            self._state()
            return
        if path.startswith("/v1/item/"):
            self._item(path[len("/v1/item/"):])
            return
        if path == "/v1/session":
            self._session()
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)

        # A valid cookie arrives with any page request, so a write must prove
        # it comes from this origin before it is trusted with one.
        if not self._csrf_ok():
            self._json(403, {"ok": False, "error": "cross-site request"})
            return

        if path == "/v1/unlock":
            self._unlock()
            return
        if path == "/v1/logout":
            self._logout()
            return
        if path != "/rpc":
            self._json(404, {"ok": False, "error": "no such endpoint"})
            return
        self._rpc()

    # ---- the API

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _rpc(self) -> None:
        body = self._read_body()
        if body is None:
            self._json(400, {"ok": False, "error": "expected a JSON object"})
            return

        # The password may ride in the body for clients that cannot set a
        # header (both are accepted). The change feed (/events) never comes
        # here: it authenticates with the session cookie instead.
        if not self._authorized():
            if not (isinstance(body.get("password"), str)
                    and secrets.compare_digest(body["password"], self.config.password)):
                self._json(401, {"ok": False, "error": "password required"})
                return

        argv = body.get("argv")
        problem = validate_argv(argv)
        if problem:
            self._json(400, {"ok": False, "error": problem})
            return

        code, payload = run_command(argv, self.config.db)
        if code == 0 and argv[0] in WRITE_COMMANDS:
            self.hub.publish()
        self._json(status_for(code, payload), payload)

    def _unlock(self) -> None:
        """One explicit password attempt, announced on the host.

        The web app calls this when the button (or Enter) is pressed, and only
        then, so a password manager or an automated retry cannot use the
        endpoint as a cracker. On success it mints the session cookie; every
        later request — including the change feed on reload — rides the
        cookie, not the password.
        """
        body = self._read_body()
        if body is None:
            self._json(400, {"ok": False, "error": "expected a JSON object"})
            return

        supplied = self._supplied_password()
        if not supplied and isinstance(body.get("password"), str):
            supplied = body["password"]

        accepted = bool(self.config.password) and secrets.compare_digest(
            supplied, self.config.password)
        client = self.client_address[0]
        self.log_message("unlock from %s: %s", client,
                         "accepted" if accepted else "refused")
        if self.config.notify_attempts and body.get("announce", True) is not False:
            notify_attempt(accepted, client, self.headers.get("User-Agent", ""))

        if not accepted:
            self._json(401, {"ok": False, "error": "password required"})
            return
        # The exchange stops here: from this request on the browser proves
        # itself with the session cookie, so the password is spent exactly
        # once and no longer travels with any URL, log or reload.
        cookie = self._session_cookie(self.sessions.create())
        self._json(200, {"ok": True, "revision": self.hub.revision},
                   extra={"Set-Cookie": cookie})

    def _session(self) -> None:
        """Does this device still have a valid session? The re-open path: the
        page asks, and only then starts painting — no secret is sent on load,
        so a listener on the port sees at most one useless opaque token."""
        if not self._authorized():
            self._json(401, {"ok": False, "error": "no session"})
            return
        self._json(200, {"ok": True, "revision": self.hub.revision})

    def _logout(self) -> None:
        self.sessions.revoke(self._cookie_token())
        self._json(200, {"ok": True}, extra={
            "Set-Cookie": self._session_cookie("", expired=True)})

    def _state(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "password required"})
            return
        query = parse_qs(urlparse(self.path).query)
        argv = ["state"]
        view = (query.get("view") or [""])[0]
        if view:
            argv += ["--view", view]
        for key, flag in (("limit", "--limit"), ("offset", "--offset"),
                          ("project", "--project"), ("area", "--area"),
                          ("tag", "--tag")):
            value = (query.get(key) or [""])[0]
            if value:
                argv += [flag, value]
        code, payload = run_command(argv, self.config.db)
        self._json(status_for(code, payload), payload)

    def _item(self, item_id: str) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "password required"})
            return
        item_id = item_id.strip()
        if not item_id:
            self._json(400, {"ok": False, "error": "an id is required"})
            return
        code, payload = run_command(["show", item_id], self.config.db)
        self._json(status_for(code, payload), payload)

    # ---- the change feed

    def _events(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "password required"})
            return

        channel = self.hub.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    revision = channel.get(timeout=SSE_KEEPALIVE)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                else:
                    self.wfile.write(f"data: {revision}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(channel)

    # ---- the shell

    def _theme(self) -> None:
        # The palette is not secret — the login screen needs it too.
        name = "omarchy"
        try:
            store = Store(path=str(self.config.db), readonly=True)
            try:
                name = str(store.meta("theme", "omarchy") or "omarchy")
            finally:
                store.close()
        except (sqlite3.Error, KeyError, TypeError):
            pass
        body = theme.stylesheet(name).encode("utf-8")
        self._send(200, body, "text/css; charset=utf-8", cache="no-cache")

    def _static(self, path: str) -> None:
        root = self.config.web_root.resolve()
        relative = path.lstrip("/") or "index.html"
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or target.is_dir():
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not target.exists():
            # A client-side route (the PWA has no router, but a stale bookmark
            # may still arrive): hand back the shell and let it decide.
            if "." not in Path(relative).name:
                target = root / "index.html"
            else:
                self._json(404, {"ok": False, "error": "not found"})
                return
        try:
            body = target.read_bytes()
        except OSError:
            self._json(404, {"ok": False, "error": "not found"})
            return
        content_type = MIME_TYPES.get(target.suffix, "application/octet-stream")
        # The shell is small and changes with the plugin; only the icons are
        # worth a browser cache. A long-lived style.css would keep a fixed
        # layout bug visible for an hour after the server was updated.
        cache = ("no-cache" if target.suffix in {".html", ".js", ".css", ".webmanifest"}
                 else "public, max-age=3600")
        extra = {}
        if target.suffix == ".html":
            extra["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
                "base-uri 'none'; form-action 'none'")
        self._send(200, body, content_type, cache=cache, extra=extra)


# ------------------------------------------------------------------ startup


def default_web_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "web"


def resolve_password(args) -> tuple[str, str]:
    """The shared secret and a human name for where it came from.

    Precedence: --password-file, $GTD_PASSWORD, ~/.local/share/omagtd/
    server.password, --password. Naming a file is a promise: a missing one
    fails closed rather than falling through to a secret the user did not
    mean to use.
    """
    path = getattr(args, "password_file", None)
    if path:
        target = Path(path).expanduser()
        return _read_password(target), str(target)
    env = os.environ.get("GTD_PASSWORD")
    if env and env.strip():
        return env.strip(), "$GTD_PASSWORD"
    default = data_home() / "server.password"
    if default.exists():
        return _read_password(default), str(default)
    flag = str(getattr(args, "password", "") or "").strip()
    if flag:
        return flag, "--password"
    return "", "nowhere"


def _read_password(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_password(args) -> str:
    return resolve_password(args)[0]


def check_password(args, out, database: Path) -> int:
    """Report where the password comes from, and optionally compare one.

    This exists because "not accepted" has several boring causes: an editor
    that saved a quote or an extra line, a server started under sudo or with
    a different $GTD_HOME, a stale process still holding the port, or simply
    typing the phrase from memory. None of them require guessing.
    """
    secret, source = resolve_password(args)

    supplied = getattr(args, "check_password", None)
    if supplied is None and secret and sys.stdin.isatty():
        import getpass
        supplied = getpass.getpass("password to test (blank to skip): ")

    if out.json_mode:
        # The settings pane runs this without a terminal, so it gets the facts
        # and, when a candidate was given, whether it matches. Never the secret.
        report = {
            "ok": bool(secret),
            "source": source,
            "database": str(database),
            "length": len(secret),
        }
        if supplied:
            report["match"] = bool(secret) and secrets.compare_digest(
                supplied.strip(), secret)
        out.data(report)
        return 0 if secret else 2

    if not secret:
        out.line(out.red("no password configured"))
        out.line(f"  looked at {data_home() / 'server.password'}")
        out.line("  and $GTD_PASSWORD, --password-file, --password")
        return 2

    out.line(out.bold("password source: ") + source)
    out.line(out.dim(f"database:        {database}"))
    out.line(out.dim(f"password:        {len(secret)} characters "
                     "(leading and trailing whitespace is ignored)"))

    if not supplied:
        return 0

    entered = supplied.strip()
    matches = secrets.compare_digest(entered, secret)
    if matches:
        out.line(out.green("match: yes"))
        return 0
    out.line(out.red("match: no"))
    if entered != supplied:
        out.line(out.dim("  (surrounding whitespace was ignored)"))
    out.line(out.dim("  restart `gtd serve` after changing the password"))
    return 2


def serve(args, out) -> int:
    database = db_path(profile=getattr(args, "profile", None),
                       explicit=getattr(args, "db", None))
    password, source = resolve_password(args)

    if getattr(args, "check", False) or getattr(args, "check_password", None) is not None:
        return check_password(args, out, database)

    if not password:
        out.fail("serve needs a shared password: use --password-file, "
                 "$GTD_PASSWORD, or ~/.local/share/omagtd/server.password")
        return 2

    web_root = Path(getattr(args, "web_root", None) or default_web_root()).expanduser()
    if not (web_root / "index.html").exists():
        out.fail(f"web client not found under {web_root}")
        return 2

    # Make sure the schema exists before anyone connects; this also proves the
    # path is writable while there is still a terminal to complain to.
    try:
        Store(path=str(database)).close()
    except Exception as exc:
        out.fail(f"cannot open the database: {exc}")
        return 2

    hub = Hub()
    config = Config(db=database, password=password, web_root=web_root,
                    verbose=bool(getattr(args, "verbose", False)),
                    notify_attempts=not bool(getattr(args, "no_notify_attempts", False)))
    handler = type("GtdHandler", (Handler,), {"config": config, "hub": hub})

    try:
        httpd = ThreadingHTTPServer((args.bind, args.port), handler)
    except OSError as exc:
        out.fail(f"cannot listen on {args.bind}:{args.port}: {exc}")
        return 2

    hub.watch(database)
    out.line(f"{out.green('serving')}  {out.bold(str(database))}")
    out.line(f"  http://{args.bind}:{args.port}/  "
             + out.dim("(bind is the listen address; use a hostname on the phone)"))
    out.line(out.dim("  shared password from " + source))
    try:
        # A server's log is often a file or a pipe, where stdout is block
        # buffered; the startup lines should be readable immediately.
        out.stream.flush()
    except (AttributeError, OSError):
        pass

    try:
        httpd.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        out.line("")
        out.line("stopping")
    finally:
        httpd.server_close()
        hub.stop()
    return 0

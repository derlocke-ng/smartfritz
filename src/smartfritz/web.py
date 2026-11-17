"""The web interface: a JSON API and three static files, standard library only."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .reconciler import Reconciler
from .schedule import Schedule, next_lights_on
from .store import KINDS, Store

SESSION_SECONDS = 30 * 86400
MAX_BODY = 64 * 1024
MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 60.0
HOLD_WITHOUT_CHANGE = 3600.0
STALE_AFTER = 180.0

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class App:
    def __init__(self, store: Store, reconciler: Reconciler, password: str, cookie_secure: bool = True, clock: Callable[[], float] = time.time) -> None:
        if not password:
            raise ValueError("an app password is required")
        self.store = store
        self.reconciler = reconciler
        self.password = password
        self.cookie_secure = cookie_secure
        self.clock = clock
        self.key = hashlib.sha256(b"smartfritz-session\0" + password.encode()).digest()
        self.login_lock = threading.Lock()
        self.login_failures = 0
        self.login_locked_until = 0.0

    # sessions: the token carries its expiry and survives a restart

    def _sign(self, expiry: int) -> str:
        return hmac.new(self.key, str(expiry).encode(), hashlib.sha256).hexdigest()

    def make_token(self) -> str:
        expiry = int(self.clock()) + SESSION_SECONDS
        return f"{expiry}.{self._sign(expiry)}"

    def valid_token(self, token: str) -> bool:
        expiry, _, signature = token.partition(".")
        if not expiry.isdigit() or int(expiry) < self.clock():
            return False
        return hmac.compare_digest(signature, self._sign(int(expiry)))

    def check_login(self, password: str) -> bool:
        with self.login_lock:
            if self.clock() < self.login_locked_until:
                raise ApiError(429, "too many attempts, wait a minute")
            if hmac.compare_digest(password.encode(), self.password.encode()):
                self.login_failures = 0
                return True
            self.login_failures += 1
            if self.login_failures >= MAX_LOGIN_FAILURES:
                self.login_failures = 0
                self.login_locked_until = self.clock() + LOGIN_LOCK_SECONDS
            return False

    # views

    def zone_view(self, zone: dict, now: float) -> dict:
        desired, reason, change = self.reconciler.desired(zone, now)
        runtime = self.reconciler.runtime.get(zone["id"])
        unit = self.reconciler.units.get(zone["unit_uid"])
        run = self.store.open_run(zone["id"])
        phase = self.store.active_phase(run["id"], now) if run else None
        pending = self.store.pending_phase(run["id"], now) if run else None
        cycle_position = None
        if phase and not Schedule(phase["on_minutes"], phase["off_minutes"]).constant:
            cycle_position = ((now - phase["started_at"]) % ((phase["on_minutes"] + phase["off_minutes"]) * 60)) / 60
        return {
            "id": zone["id"], "name": zone["name"], "unit_uid": zone["unit_uid"], "lamp_watts": zone["lamp_watts"],
            "enabled": bool(zone["enabled"]),
            "desired": desired, "reason": reason, "next_change": change,
            "hold_until": zone["hold_until"] if reason == "hold" else None,
            "problem": runtime.problem if runtime else "",
            "unit": unit.to_dict() if unit else None,
            "run": self.store.run_summary(run["id"], now) if run else None,
            "phase": phase, "pending": pending, "cycle_position": cycle_position,
        }

    def state(self) -> dict:
        now = self.clock()
        reconciler = self.reconciler
        return {
            "now": now,
            "box": {"ok": not reconciler.box_error and reconciler.last_round > 0, "error": reconciler.box_error, "last_round": reconciler.last_round},
            "zones": [self.zone_view(zone, now) for zone in self.store.zones()],
        }

    def healthy(self) -> bool:
        return self.clock() - self.reconciler.last_round < STALE_AFTER

    # requests

    @staticmethod
    def _schedule(body: dict) -> tuple[str, int, int]:
        kind = body.get("kind")
        if kind not in KINDS:
            raise ApiError(400, "kind must be veg, bloom or other")
        try:
            schedule = Schedule(body.get("on_minutes"), body.get("off_minutes"))  # type: ignore[arg-type]
        except ValueError as error:
            raise ApiError(400, str(error)) from error
        return kind, schedule.on_minutes, schedule.off_minutes

    @staticmethod
    def _text(body: dict, key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 80:
            raise ApiError(400, f"{key} must be 1 to 80 characters")
        return value.strip()

    @staticmethod
    def _watts(body: dict) -> float | None:
        value = body.get("lamp_watts")
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 10000:
            raise ApiError(400, "lamp_watts must be between 0 and 10000")
        return float(value)

    def _zone(self, zone_id: int) -> dict:
        zone = self.store.zone(zone_id)
        if zone is None:
            raise ApiError(404, "no such plug")
        return zone

    def _open_run(self, run_id: int) -> dict:
        run = self.store.run(run_id)
        if run is None or run["ended_at"] is not None:
            raise ApiError(404, "no such running grow")
        return run

    def api(self, method: str, path: str, body: dict) -> object:
        now = self.clock()
        if (method, path) == ("GET", "/api/state"):
            return self.state()
        if (method, path) == ("GET", "/api/units"):
            taken = {zone["unit_uid"] for zone in self.store.zones()}
            return [{**unit.to_dict(), "taken": unit.uid in taken} for unit in self.reconciler.units.values()]
        if (method, path) == ("GET", "/api/runs"):
            return [self.store.run_summary(run["id"], now) for run in self.store.runs()]
        if (method, path) == ("GET", "/api/events"):
            return self.store.events(limit=100)
        if (method, path) == ("POST", "/api/zones"):
            uid = self._text(body, "unit_uid")
            if uid not in self.reconciler.units:
                raise ApiError(400, "the box does not list that plug")
            try:
                return {"id": self.store.add_zone(self._text(body, "name"), uid, self._watts(body))}
            except sqlite3.IntegrityError as error:
                raise ApiError(409, "that plug is already added") from error

        if match := re.fullmatch(r"/api/zones/(\d+)", path):
            zone_id = int(match[1])
            self._zone(zone_id)
            if method == "PATCH":
                fields: dict[str, object] = {}
                if "name" in body:
                    fields["name"] = self._text(body, "name")
                if "lamp_watts" in body:
                    fields["lamp_watts"] = self._watts(body)
                if "enabled" in body:
                    fields["enabled"] = int(bool(body["enabled"]))
                self.store.update_zone(zone_id, **fields)
                return {}
        if match := re.fullmatch(r"/api/zones/(\d+)/hold", path):
            return self._hold(int(match[1]), body, now) if method == "POST" else self._not_found()
        if match := re.fullmatch(r"/api/zones/(\d+)/runs", path):
            return self._start_run(int(match[1]), body, now) if method == "POST" else self._not_found()
        if (match := re.fullmatch(r"/api/runs/(\d+)/end", path)) and method == "POST":
            self._open_run(int(match[1]))
            self.store.end_run(int(match[1]), now)
            return {}
        if (match := re.fullmatch(r"/api/runs/(\d+)/phases", path)) and method == "POST":
            return self._add_phase(int(match[1]), body, now)
        return self._not_found()

    @staticmethod
    def _not_found() -> object:
        raise ApiError(404, "not found")

    def _hold(self, zone_id: int, body: dict, now: float) -> dict:
        zone = self._zone(zone_id)
        state = body.get("state")
        if state is None:
            self.store.set_hold(zone_id, None, None)
            self.store.add_event(zone_id, "hold_ended", "released", ts=now)
            return {}
        if not isinstance(state, bool):
            raise ApiError(400, "state must be true, false or null")
        self.store.set_hold(zone_id, None, None)
        _, _, change = self.reconciler.desired({**zone, "hold_state": None}, now)
        until = change if change is not None else now + HOLD_WITHOUT_CHANGE
        self.store.set_hold(zone_id, state, until)
        self.store.add_event(zone_id, "hold", "until the next change", active=state, ts=now)
        return {"until": until}

    def _start_run(self, zone_id: int, body: dict, now: float) -> dict:
        self._zone(zone_id)
        kind, on_minutes, off_minutes = self._schedule(body)
        start = body.get("first_light_on", now)
        if start is None:
            start = now
        if isinstance(start, bool) or not isinstance(start, int | float):
            raise ApiError(400, "first_light_on must be a time")
        try:
            run_id = self.store.start_run(zone_id, self._text(body, "name"), float(start), kind, on_minutes, off_minutes)
        except sqlite3.IntegrityError as error:
            raise ApiError(409, "this plug already has a grow running") from error
        self.store.add_event(zone_id, "grow_started", self._text(body, "name"), ts=now)
        return {"id": run_id}

    def _add_phase(self, run_id: int, body: dict, now: float) -> dict:
        run = self._open_run(run_id)
        kind, on_minutes, off_minutes = self._schedule(body)
        current = self.store.active_phase(run_id, now)
        start = body.get("start", "next_on")
        if start == "now":
            at = now
        elif start == "next_on":
            if current is None:
                raise ApiError(400, "the grow has not started yet")
            at = next_lights_on(Schedule(current["on_minutes"], current["off_minutes"]), current["started_at"], now)
        elif isinstance(start, int | float) and not isinstance(start, bool) and start >= now:
            at = float(start)
        else:
            raise ApiError(400, "start must be now, next_on or a future time")
        phase_id = self.store.add_phase(run_id, kind, on_minutes, off_minutes, at, now)
        self.store.add_event(run["zone_id"], "schedule_changed", f"{kind} {on_minutes / 60:g}/{off_minutes / 60:g}", ts=now)
        return {"id": phase_id, "starts": at}


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "smartfritz"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _send(self, status: int, payload: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, document: object, extra: dict | None = None) -> None:
        self._send(status, json.dumps(document).encode(), "application/json", extra)

    def _session(self) -> bool:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        morsel = cookies.get("session")
        return morsel is not None and self.app.valid_token(morsel.value)

    def _cookie(self, value: str, max_age: int) -> dict:
        secure = "; Secure" if self.app.cookie_secure else ""
        return {"Set-Cookie": f"session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"}

    def _body(self) -> dict:
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ApiError(415, "send JSON")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ApiError(400, "bad length") from error
        if length < 0 or length > MAX_BODY:
            raise ApiError(413, "too large")
        try:
            document = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as error:
            raise ApiError(400, "bad JSON") from error
        if not isinstance(document, dict):
            raise ApiError(400, "send an object")
        return document

    def _handle(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if method == "GET" and path in STATIC:
                name, content_type = STATIC[path]
                data = resources.files("smartfritz").joinpath("static", name).read_bytes()
                self._send(200, data, content_type)
                return
            if method == "GET" and path == "/healthz":
                healthy = self.app.healthy()
                self._json(200 if healthy else 503, {"ok": healthy})
                return
            body = self._body() if method != "GET" else {}
            if (method, path) == ("POST", "/api/login"):
                password = body.get("password")
                if isinstance(password, str) and self.app.check_login(password):
                    self._json(200, {}, self._cookie(self.app.make_token(), SESSION_SECONDS))
                    return
                raise ApiError(403, "wrong password")
            if not self._session():
                raise ApiError(401, "log in first")
            if (method, path) == ("POST", "/api/logout"):
                self._json(200, {}, self._cookie("", 0))
                return
            self._json(200, self.app.api(method, path, body))
        except ApiError as error:
            self._json(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._json(500, {"error": f"internal error: {type(error).__name__}"})

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")


def make_server(app: App, bind: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    return server

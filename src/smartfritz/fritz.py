"""The FRITZ!Box: session login and the Smart Home REST API (FRITZ!OS 8.20+)."""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass
from urllib.parse import quote, urlencode, urlsplit

NO_SID = "0000000000000000"
API = "/api/v0/smarthome"
PERMISSION_DENIED = 3001
MIN_LOGIN_RETRY = 60.0


class FritzError(Exception):
    """The box could not be reached or refused the request."""


class FritzAuthError(FritzError):
    """The login failed. Retried slowly: the box blocks repeated failures."""


@dataclass
class Unit:
    uid: str
    name: str
    unit_type: str
    connected: bool
    active: bool | None
    outlet_state: str
    locked: bool
    power_mw: float | None
    energy_wh: float | None
    celsius: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def solve_challenge(challenge: str, password: str) -> str:
    """Answer a version 2 challenge: 2$iter1$salt1$iter2$salt2, PBKDF2-SHA256 twice."""
    parts = challenge.split("$")
    if len(parts) != 5 or parts[0] != "2":
        raise FritzAuthError("the box did not offer a version 2 login")
    iter1, salt1, iter2, salt2 = int(parts[1]), bytes.fromhex(parts[2]), int(parts[3]), bytes.fromhex(parts[4])
    first = hashlib.pbkdf2_hmac("sha256", password.encode(), salt1, iter1)
    second = hashlib.pbkdf2_hmac("sha256", first, salt2, iter2)
    return f"{parts[4]}${second.hex()}"


def parse_unit(raw: dict) -> Unit | None:
    """One unit of the overview, or None when it cannot be switched."""
    interfaces = raw.get("interfaces") or {}
    on_off = interfaces.get("onOffInterface")
    if on_off is None or raw.get("isGroupUnit"):
        return None
    meter = interfaces.get("multimeterInterface") or {}
    meter_valid = meter.get("state") == "valid"
    temperature = interfaces.get("temperatureInterface") or {}
    return Unit(
        uid=raw["UID"],
        name=raw.get("name") or raw["UID"],
        unit_type=raw.get("unitType") or "unknown",
        connected=bool(raw.get("isConnected")),
        active=on_off.get("active") if on_off.get("state") == "valid" else None,
        outlet_state=on_off.get("outletState") or "valid",
        locked=bool(on_off.get("isLockedDeviceApi")),
        power_mw=meter.get("power") if meter_valid else None,
        energy_wh=meter.get("energy") if meter_valid else None,
        celsius=temperature.get("celsius") if temperature.get("state") == "valid" else None,
    )


class FritzClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        pin: str = "",
        on_pin: Callable[[str], None] | None = None,
        repin: bool = False,
        on_repin: Callable[[str, str], None] | None = None,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the box's URL must be http:// or https:// with a host")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port
        self.user = user
        self.password = password
        self.pin = pin
        self.on_pin = on_pin
        self.repin = repin
        self.on_repin = on_repin
        self.timeout = timeout
        self.clock = clock
        self.sid = NO_SID
        self.login_not_before = 0.0
        self._lock = threading.RLock()

    def _connect(self) -> http.client.HTTPConnection:
        if self.scheme == "http":
            connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            connection.connect()
            return connection
        # The box's certificate is self-signed for its MyFRITZ name, so it cannot be
        # verified by name. It is pinned instead: remembered on first contact.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)
        connection.connect()
        der = connection.sock.getpeercert(binary_form=True)  # type: ignore[union-attr]
        try:
            self._check_pin(hashlib.sha256(der).hexdigest())
        except FritzError:
            connection.close()
            raise
        return connection

    def _check_pin(self, fingerprint: str) -> None:
        if fingerprint == self.pin:
            return
        previous = self.pin
        if previous and not self.repin:
            raise FritzError(f"the box's certificate changed (now {fingerprint}); refusing to send the login")
        # First contact, or the owner chose to follow the box when it makes itself a
        # new certificate. That trades the pin's protection for not going dark.
        self.pin = fingerprint
        if self.on_pin:
            self.on_pin(fingerprint)
        if previous and self.on_repin:
            self.on_repin(previous, fingerprint)

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
        try:
            connection = self._connect()
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as error:
            raise FritzError(f"cannot reach the box: {error}") from error

    def _login(self) -> None:
        now = self.clock()
        if now < self.login_not_before:
            raise FritzAuthError(f"login paused for {int(self.login_not_before - now)} s after a failure")
        status, payload = self._request("GET", "/login_sid.lua?version=2")
        info = self._session_info(status, payload)
        challenge = info.findtext("Challenge") or ""
        form = urlencode({"username": self.user, "response": solve_challenge(challenge, self.password)})
        status, payload = self._request(
            "POST", "/login_sid.lua?version=2", form.encode(), {"Content-Type": "application/x-www-form-urlencoded"}
        )
        info = self._session_info(status, payload)
        sid = info.findtext("SID") or NO_SID
        if sid == NO_SID:
            block = float(info.findtext("BlockTime") or 0)
            self.login_not_before = self.clock() + max(block, MIN_LOGIN_RETRY)
            raise FritzAuthError("the box refused the username or password")
        self.sid = sid

    @staticmethod
    def _session_info(status: int, payload: bytes) -> ET.Element:
        if status != 200:
            raise FritzError(f"login page answered {status}")
        try:
            return ET.fromstring(payload)
        except ET.ParseError as error:
            raise FritzError("login page did not answer with XML") from error

    @staticmethod
    def _denied(status: int, payload: bytes) -> bool:
        if status in (401, 403):
            return True
        try:
            errors = json.loads(payload).get("errors") or []
        except (ValueError, AttributeError):
            return False
        return any(e.get("code") == PERMISSION_DENIED for e in errors)

    def _api(self, method: str, path: str, document: object = None) -> object:
        body = None if document is None else json.dumps(document).encode()
        with self._lock:
            for attempt in (1, 2):
                if self.sid == NO_SID:
                    self._login()
                headers = {"Authorization": f"AVM-SID {self.sid}", "Accept": "application/json"}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                status, payload = self._request(method, API + path, body, headers)
                if 200 <= status < 300:
                    return json.loads(payload) if payload.strip() else None
                if self._denied(status, payload) and attempt == 1:
                    self.sid = NO_SID  # the session ran out: log in once more
                    continue
                raise FritzError(f"{method} {path} answered {status}: {payload[:200].decode(errors='replace')}")
        raise FritzError("unreachable")

    def units(self) -> list[Unit]:
        document = self._api("GET", "/overview/units")
        if not isinstance(document, list):
            raise FritzError("the unit list was not a list")
        return [unit for unit in map(parse_unit, document) if unit is not None]

    def set_on_off(self, uid: str, active: bool) -> None:
        self._api("PUT", f"/overview/units/{quote(uid, safe='')}", {"interfaces": {"onOffInterface": {"active": active}}})

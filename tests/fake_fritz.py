"""A stand-in FRITZ!Box: the login and the REST endpoints smartfritz uses."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

USER = "testuser"
PASSWORD = "test-password"
SID = "abcdef0123456789"
SALT1, SALT2, ITER1, ITER2 = "aa11", "bb22", 100, 50


def plug(uid: str, name: str, active: bool = False, meter: bool = True, power: int = 0, energy: int = 1000) -> dict:
    interfaces: dict = {"onOffInterface": {"state": "valid", "active": active, "isLockedDeviceApi": False, "isLockedDeviceLocal": False, "outletState": "valid"}}
    if meter:
        interfaces["multimeterInterface"] = {"state": "valid", "power": power, "energy": energy, "voltage": 230000, "current": 0}
        interfaces["temperatureInterface"] = {"state": "valid", "celsius": 24}
    return {"UID": uid, "name": name, "isConnected": True, "unitType": "avmPlugSocket" if meter else "acOutlet", "interfaces": interfaces}


def default_units() -> list[dict]:
    return [plug("11630 0000001", "Tent A"), plug("11630 0000002", "Tent B"), plug("Z0011223344556677-1", "TRETAKT", meter=False)]


class FakeFritz:
    def __init__(self, port: int = 0, units: list[dict] | None = None, watts_when_on: int = 150000) -> None:
        self.units = default_units() if units is None else units
        self.watts_when_on = watts_when_on
        self.puts: list[tuple[str, dict]] = []
        self.logins = 0
        self.fail_login = False
        self.valid_sid = SID
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def _send(self, status, body, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _denied(self):
                self._send(400, json.dumps({"errors": [{"code": 3001, "message": "permission denied"}]}).encode())

            def _authorized(self):
                return self.headers.get("Authorization") == f"AVM-SID {fake.valid_sid}"

            def _xml(self, sid, block=0):
                body = f"<SessionInfo><SID>{sid}</SID><Challenge>2${ITER1}${SALT1}${ITER2}${SALT2}</Challenge><BlockTime>{block}</BlockTime></SessionInfo>"
                self._send(200, body.encode(), "text/xml")

            def do_GET(self):
                if self.path.startswith("/login_sid.lua"):
                    return self._xml("0000000000000000")
                if self.path == "/api/v0/smarthome/overview/units":
                    if not self._authorized():
                        return self._denied()
                    return self._send(200, json.dumps(fake.units).encode())
                self._send(404, b"{}")

            def do_POST(self):
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                first = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), bytes.fromhex(SALT1), ITER1)
                second = hashlib.pbkdf2_hmac("sha256", first, bytes.fromhex(SALT2), ITER2)
                good = form.get("username") == [USER] and form.get("response") == [f"{SALT2}${second.hex()}"]
                fake.logins += 1
                if good and not fake.fail_login:
                    return self._xml(fake.valid_sid)
                self._xml("0000000000000000", block=8)

            def do_PUT(self):
                if not self._authorized():
                    return self._denied()
                uid = unquote(self.path.rsplit("/", 1)[1])
                document = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.puts.append((uid, document))
                for unit in fake.units:
                    if unit["UID"] == uid:
                        active = document["interfaces"]["onOffInterface"]["active"]
                        unit["interfaces"]["onOffInterface"]["active"] = active
                        if "multimeterInterface" in unit["interfaces"]:
                            unit["interfaces"]["multimeterInterface"]["power"] = fake.watts_when_on if active else 0
                        return self._send(200, b"{}")
                self._send(404, b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def active(self, uid: str) -> bool:
        return next(u for u in self.units if u["UID"] == uid)["interfaces"]["onOffInterface"]["active"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

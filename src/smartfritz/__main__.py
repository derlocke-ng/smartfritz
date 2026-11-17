"""Start the loop and the web server from environment variables."""

from __future__ import annotations

import os
import signal
import sys
import threading

from .fritz import FritzClient
from .reconciler import Reconciler
from .store import Store
from .web import App, make_server

PIN_KEY = "fritz_cert_sha256"


def main() -> int:
    env = os.environ
    missing = [k for k in ("SMARTFRITZ_FRITZ_USER", "SMARTFRITZ_FRITZ_PASSWORD", "SMARTFRITZ_APP_PASSWORD") if not env.get(k)]
    if missing:
        print("missing settings: " + ", ".join(missing), file=sys.stderr)
        return 2
    store = Store(env.get("SMARTFRITZ_DB", "/data/smartfritz.db"))
    repin = env.get("SMARTFRITZ_FRITZ_CERT_AUTO", "0") == "1"
    stored = store.get_setting(PIN_KEY)
    fixed = env.get("SMARTFRITZ_FRITZ_CERT_SHA256", "")
    fritz = FritzClient(
        env.get("SMARTFRITZ_FRITZ_URL", "https://192.168.2.1"),
        env["SMARTFRITZ_FRITZ_USER"],
        env["SMARTFRITZ_FRITZ_PASSWORD"],
        # When following the box, the remembered certificate is newer than the configured one.
        pin=(stored or fixed) if repin else (fixed or stored),
        on_pin=lambda fingerprint: store.set_setting(PIN_KEY, fingerprint),
        repin=repin,
        on_repin=lambda old, new: store.add_event(None, "box_cert", f"was {old}, now {new}"),
    )
    reconciler = Reconciler(store, fritz, tick=float(env.get("SMARTFRITZ_TICK", "30")))
    app = App(store, reconciler, env["SMARTFRITZ_APP_PASSWORD"], cookie_secure=env.get("SMARTFRITZ_COOKIE_SECURE", "1") != "0")
    server = make_server(app, env.get("SMARTFRITZ_BIND", "0.0.0.0"), int(env.get("SMARTFRITZ_PORT", "8080")))
    stop = threading.Event()

    def shutdown(*_: object) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=reconciler.run, args=(stop,), daemon=True, name="reconciler").start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

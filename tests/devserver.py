"""Run the app against the fake box, with a throwaway database. Password: dev-password"""

import os
import sys
import tempfile
import threading
import time

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "src"), os.path.dirname(__file__)]

from fake_fritz import PASSWORD, USER, FakeFritz  # noqa: E402

from smartfritz.fritz import FritzClient  # noqa: E402
from smartfritz.reconciler import Reconciler  # noqa: E402
from smartfritz.store import Store  # noqa: E402
from smartfritz.web import App, make_server  # noqa: E402

fake = FakeFritz()
directory = tempfile.mkdtemp(prefix="smartfritz-dev-")
store = Store(os.path.join(directory, "dev.db"))
reconciler = Reconciler(store, FritzClient(fake.url, USER, PASSWORD), tick=5, stagger=0.2)
app = App(store, reconciler, "dev-password", cookie_secure=False)


def seed() -> None:
    """A running grow and two finished ones, so History has something to show."""
    day, now = 86400, time.time()
    tent_a = store.add_zone("Tent A", "11630 0000001", None)
    tent_b = store.add_zone("Tent B", "11630 0000002", None)
    seedlings = store.add_zone("Seedlings", "Z0011223344556677-1", 65)

    def fill(zone: int, start: float, end: float) -> None:
        for number in range(int((end - start) // day)):
            moment = start + number * day
            store.record_state(zone, True, moment)
            store.record_state(zone, False, moment + 15 * 3600)
            if zone != seedlings:
                store.add_sample(zone, moment, 140000, 2500.0 * number, 24)
        if zone != seedlings:
            store.add_sample(zone, end, 140000, 2500.0 * (end - start) / day, 24)

    old = store.start_run(tent_a, "Northern run", now - 130 * day, "veg", 1080, 360)
    store.add_phase(old, "bloom", 720, 720, now - 104 * day, now - 105 * day)
    fill(tent_a, now - 130 * day, now - 40 * day)
    store.end_run(old, now - 40 * day)

    other = store.start_run(seedlings, "Clones", now - 26 * day, "veg", 1200, 240)
    fill(seedlings, now - 26 * day, now - 3 * day)
    store.end_run(other, now - 3 * day)

    current = store.start_run(tent_b, "chernobyl1", now - 31 * day, "veg", 1080, 360)
    store.add_phase(current, "veg", 1200, 240, now - 20 * day, now - 21 * day)
    store.add_phase(current, "bloom", 720, 720, now - 9 * day, now - 10 * day)
    fill(tent_b, now - 31 * day, now)


seed()
threading.Thread(target=reconciler.run, args=(threading.Event(),), daemon=True).start()
make_server(app, "127.0.0.1", 8099).serve_forever()

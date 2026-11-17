import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "src"), os.path.dirname(__file__)]

from fake_fritz import PASSWORD, USER, FakeFritz, plug  # noqa: E402

from smartfritz.fritz import FritzAuthError, FritzClient, FritzError, parse_unit  # noqa: E402
from smartfritz.reconciler import NOT_FOLLOWING_AFTER, Reconciler  # noqa: E402
from smartfritz.schedule import Schedule, evaluate, next_lights_on  # noqa: E402
from smartfritz.store import Store  # noqa: E402
from smartfritz.web import App, ApiError, make_server  # noqa: E402

H, M = 3600, 60
T0 = 1_800_000_000.0
A, B, TRETAKT = "11630 0000001", "11630 0000002", "Z0011223344556677-1"


class ScheduleTests(unittest.TestCase):
    def test_every_requested_preset(self):
        for on, off in ((12, 12), (18, 6), (4, 4), (11, 13), (20, 4)):
            schedule = Schedule(on * M, off * M)
            self.assertEqual(evaluate(schedule, T0, T0), (True, T0 + on * H))
            self.assertEqual(evaluate(schedule, T0, T0 + on * H - 1)[0], True)
            self.assertEqual(evaluate(schedule, T0, T0 + on * H), (False, T0 + (on + off) * H))
            self.assertEqual(evaluate(schedule, T0, T0 + (on + off) * H)[0], True)
            self.assertEqual(evaluate(schedule, T0, T0 + 1000 * (on + off) * H + 1)[0], True)

    def test_constant(self):
        self.assertEqual(evaluate(Schedule(24 * M, 0), T0, T0 + 99 * H), (True, None))
        self.assertEqual(evaluate(Schedule(0, 60), T0, T0 + 99 * H), (False, None))

    def test_custom_minutes(self):
        schedule = Schedule(90, 30)
        self.assertFalse(evaluate(schedule, T0, T0 + 100 * M)[0])
        self.assertTrue(evaluate(schedule, T0, T0 + 121 * M)[0])

    def test_invalid(self):
        for on, off in ((0, 0), (-1, 5), (1.5, 5), (True, 5), (99999, 0)):
            with self.assertRaises(ValueError):
                Schedule(on, off)

    def test_next_lights_on_keeps_the_night(self):
        schedule = Schedule(18 * M, 6 * M)
        self.assertEqual(next_lights_on(schedule, T0, T0 + 20 * H), T0 + 24 * H)
        self.assertEqual(next_lights_on(schedule, T0, T0 + 2 * H), T0 + 24 * H)
        self.assertEqual(next_lights_on(Schedule(24 * M, 0), T0, T0 + 2 * H), T0 + 2 * H)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(self.store.db.close)
        self.fake = FakeFritz()
        self.addCleanup(self.fake.close)
        self.clock = Clock()
        self.slept = []
        self.client = FritzClient(self.fake.url, USER, PASSWORD, clock=self.clock)
        self.reconciler = Reconciler(self.store, self.client, clock=self.clock, sleep=self.slept.append)

    def grow(self, uid=A, on=18, off=6, kind="veg", watts=None):
        zone = self.store.add_zone(f"zone {uid}", uid, watts)
        return zone, self.store.start_run(zone, "grow", self.clock.now, kind, on * M, off * M)


class FritzTests(Base):
    def test_login_and_uid_with_space(self):
        self.client.set_on_off(A, True)
        self.assertTrue(self.fake.active(A))
        self.assertEqual(self.fake.logins, 1)

    def test_expired_session_logs_in_once_more(self):
        self.client.units()
        self.fake.valid_sid = "1111111111111111"
        self.assertEqual(len(self.client.units()), 3)
        self.assertEqual(self.fake.logins, 2)

    def test_failed_login_is_not_hammered(self):
        self.fake.fail_login = True
        for _ in range(4):
            with self.assertRaises(FritzAuthError):
                self.client.units()
        self.assertEqual(self.fake.logins, 1)
        self.clock.now += 61
        with self.assertRaises(FritzAuthError):
            self.client.units()
        self.assertEqual(self.fake.logins, 2)

    def test_zigbee_has_no_meter(self):
        unit = parse_unit(plug(TRETAKT, "x", meter=False))
        self.assertIsNone(unit.power_mw)
        self.assertIs(unit.active, False)

    def test_unknown_state_and_groups(self):
        raw = plug(A, "x")
        raw["interfaces"]["onOffInterface"]["state"] = "notConnected"
        self.assertIsNone(parse_unit(raw).active)
        self.assertIsNone(parse_unit({**plug(A, "x"), "isGroupUnit": True}))
        self.assertIsNone(parse_unit({"UID": "x", "interfaces": {"thermostatInterface": {}}}))


class PinTests(unittest.TestCase):
    def client(self, **options):
        self.pinned, self.repinned = [], []
        return FritzClient(
            "https://192.0.2.1", USER, PASSWORD,
            on_pin=self.pinned.append, on_repin=lambda old, new: self.repinned.append((old, new)), **options,
        )

    def test_first_contact_is_remembered(self):
        client = self.client()
        client._check_pin("aa")
        client._check_pin("aa")
        self.assertEqual((client.pin, self.pinned, self.repinned), ("aa", ["aa"], []))

    def test_a_changed_certificate_is_refused(self):
        client = self.client(pin="aa")
        with self.assertRaisesRegex(FritzError, "certificate changed \\(now bb\\)"):
            client._check_pin("bb")
        self.assertEqual((client.pin, self.pinned), ("aa", []))

    def test_a_changed_certificate_is_followed_when_asked(self):
        client = self.client(pin="aa", repin=True)
        client._check_pin("bb")
        client._check_pin("bb")
        self.assertEqual((client.pin, self.pinned, self.repinned), ("bb", ["bb"], [("aa", "bb")]))


class ReconcilerTests(Base):
    def test_follows_the_cycle_for_any_plug_type(self):
        self.grow(A, 18, 6)
        self.grow(TRETAKT, 4, 4)
        self.reconciler.round()
        self.assertTrue(self.fake.active(A))
        self.assertTrue(self.fake.active(TRETAKT))
        self.assertEqual(self.slept, [3.0])
        self.clock.now += 5 * H
        self.reconciler.round()
        self.assertTrue(self.fake.active(A))
        self.assertFalse(self.fake.active(TRETAKT))
        self.clock.now += 14 * H
        self.reconciler.round()
        self.assertFalse(self.fake.active(A))

    def test_restart_catches_up(self):
        self.grow(A, 12, 12)
        self.reconciler.round()
        self.clock.now += 13 * H
        fresh = Reconciler(self.store, self.client, clock=self.clock, sleep=self.slept.append)
        fresh.round()
        self.assertFalse(self.fake.active(A))

    def test_a_manual_change_is_corrected(self):
        self.grow(A, 12, 12)
        self.reconciler.round()
        self.fake.units[0]["interfaces"]["onOffInterface"]["active"] = False
        self.reconciler.round()
        self.assertTrue(self.fake.active(A))
        self.assertEqual(self.store.events(1)[0]["detail"], "correction")

    def test_no_grow_leaves_the_plug_alone(self):
        self.store.add_zone("idle", A, None)
        self.reconciler.round()
        self.assertEqual(self.fake.puts, [])

    def test_no_writes_when_already_right(self):
        self.grow(A, 12, 12)
        self.reconciler.round()
        self.reconciler.round()
        self.assertEqual(len(self.fake.puts), 1)

    def test_hold_then_back_to_schedule(self):
        zone, _ = self.grow(A, 12, 12)
        self.store.set_hold(zone, False, T0 + 12 * H)
        self.reconciler.round()
        self.assertFalse(self.fake.active(A))
        self.clock.now += 11 * H
        self.reconciler.round()
        self.assertFalse(self.fake.active(A))
        self.clock.now += 14 * H  # hour 25: the hold is over and the next light period has begun
        self.reconciler.round()
        self.assertTrue(self.fake.active(A))
        self.assertIsNone(self.store.zone(zone)["hold_state"])

    def test_phase_change_at_next_lights_on(self):
        zone, run = self.grow(A, 18, 6)
        self.store.add_phase(run, "bloom", 12 * M, 12 * M, T0 + 24 * H, T0 + H)
        self.clock.now += 20 * H
        self.reconciler.round()
        self.assertFalse(self.fake.active(A))
        self.clock.now += 4 * H + 1
        self.reconciler.round()
        self.assertTrue(self.fake.active(A))
        self.clock.now += 13 * H
        self.reconciler.round()
        self.assertFalse(self.fake.active(A))

    def test_box_down_is_recorded_once_and_recovers(self):
        self.grow(A, 12, 12)
        url = self.client.port
        self.client.port = 1
        self.reconciler.round()
        self.reconciler.round()
        self.assertTrue(self.reconciler.box_error)
        self.assertEqual(len([e for e in self.store.events(limit=9) if e["kind"] == "box_error"]), 1)
        self.client.port = url
        self.reconciler.round()
        self.assertEqual(self.reconciler.box_error, "")
        self.assertTrue(self.fake.active(A))

    def test_offline_and_locked_are_problems_not_writes(self):
        zone, _ = self.grow(A, 12, 12)
        self.fake.units[0]["isConnected"] = False
        self.reconciler.round()
        self.assertEqual(self.reconciler.runtime[zone].problem, "plug offline")
        self.fake.units[0]["isConnected"] = True
        self.fake.units[0]["interfaces"]["onOffInterface"]["isLockedDeviceApi"] = True
        self.reconciler.round()
        self.assertIn("locked", self.reconciler.runtime[zone].problem)
        self.assertEqual(self.fake.puts, [])

    def test_plug_that_ignores_commands(self):
        zone, _ = self.grow(A, 12, 12)
        original = self.client.set_on_off
        self.client.set_on_off = lambda uid, active: None
        for _ in range(NOT_FOLLOWING_AFTER):
            self.reconciler.round()
        self.assertIn("not following", self.reconciler.runtime[zone].problem)
        self.client.set_on_off = original
        self.reconciler.round()
        self.reconciler.round()
        self.assertEqual(self.reconciler.runtime[zone].problem, "")

    def test_dead_lamp_on_a_metered_plug_only(self):
        self.fake.watts_when_on = 0
        metered, _ = self.grow(A, 12, 12)
        unmetered, _ = self.grow(TRETAKT, 12, 12)
        for _ in range(5):
            self.reconciler.round()
            self.clock.now += 30
        self.assertIn("check the lamp", self.reconciler.runtime[metered].problem)
        self.assertEqual(self.reconciler.runtime[unmetered].problem, "")

    def test_reads_back_soon_after_a_switch(self):
        self.grow(A, 12, 12)
        self.reconciler.round()
        self.assertEqual(self.reconciler.next_wait(self.clock.now), 5.0)
        self.reconciler.round()
        self.assertEqual(self.reconciler.next_wait(self.clock.now), 30.0)

    def test_wakes_for_a_change_inside_the_tick(self):
        self.grow(A, 1, 1)
        self.clock.now += 59 * M + 50
        self.assertAlmostEqual(self.reconciler.next_wait(self.clock.now), 10.5)


class HistoryTests(Base):
    def test_veg_and_bloom_days_and_weeks(self):
        zone = self.store.add_zone("a", A, None)
        run = self.store.start_run(zone, "g", T0, "veg", 18 * M, 6 * M)
        self.store.add_phase(run, "veg", 20 * M, 4 * M, T0 + 10 * 24 * H, T0 + 10 * 24 * H - 1)
        self.store.add_phase(run, "bloom", 12 * M, 12 * M, T0 + 21 * 24 * H, T0 + 21 * 24 * H - 1)
        summary = self.store.run_summary(run, T0 + 30 * 24 * H + H)
        self.assertEqual(summary["totals"]["veg"]["days"], 21.0)
        self.assertEqual(summary["totals"]["veg"]["weeks"], 3.0)
        self.assertEqual(summary["current"], {"kind": "bloom", "since": T0 + 21 * 24 * H, "day": 10, "week": 2})

    def test_contiguous_veg_counts_as_one_stage(self):
        zone = self.store.add_zone("a", A, None)
        run = self.store.start_run(zone, "g", T0, "veg", 18 * M, 6 * M)
        self.store.add_phase(run, "veg", 20 * M, 4 * M, T0 + 8 * 24 * H, T0 + 8 * 24 * H - 1)
        self.assertEqual(self.store.run_summary(run, T0 + 8 * 24 * H + H)["current"]["week"], 2)

    def test_pending_phase_is_not_counted(self):
        zone, run = self.grow(A)
        self.store.add_phase(run, "bloom", 720, 720, T0 + 24 * H, T0)
        summary = self.store.run_summary(run, T0 + H)
        self.assertEqual(list(summary["totals"]), ["veg"])

    def test_light_hours_come_from_what_really_happened(self):
        zone, run = self.grow(A, 12, 12)
        self.store.record_state(zone, True, T0)
        self.store.record_state(zone, False, T0 + 12 * H)
        self.store.record_state(zone, True, T0 + 24 * H)
        self.assertEqual(self.store.run_summary(run, T0 + 26 * H)["phases"][0]["light_hours"], 14.0)

    def test_record_state_only_on_change(self):
        zone, _ = self.grow(A)
        for ts in (T0, T0 + 1, T0 + 2):
            self.store.record_state(zone, True, ts)
        self.assertEqual(len(self.store._all("SELECT * FROM events WHERE kind='state'")), 1)

    def test_metered_energy_skips_counter_reset(self):
        zone, run = self.grow(A)
        for offset, wh in ((0, 100.0), (H, 200.0), (2 * H, 5.0), (3 * H, 55.0)):
            self.store.add_sample(zone, T0 + offset, 1.0, wh, 20.0)
        phase = self.store.run_summary(run, T0 + 4 * H)["phases"][0]
        self.assertEqual(phase["kwh"], 0.15)
        self.assertFalse(phase["kwh_estimated"])

    def test_estimate_for_the_plug_without_a_meter(self):
        zone, run = self.grow(TRETAKT, watts=100.0)
        self.store.record_state(zone, True, T0)
        phase = self.store.run_summary(run, T0 + 10 * H)["phases"][0]
        self.assertEqual(phase["kwh"], 1.0)
        self.assertTrue(phase["kwh_estimated"])

    def test_one_open_grow_per_plug(self):
        zone, _ = self.grow(A)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.start_run(zone, "again", T0, "veg", 60, 60)

    def test_running_grows_are_listed_first(self):
        finished = self.store.add_zone("a", A, None)
        running = self.store.add_zone("b", B, None)
        first = self.store.start_run(running, "older but running", T0 - 9 * H, "veg", 60, 60)
        second = self.store.start_run(finished, "newer but over", T0, "veg", 60, 60)
        self.store.end_run(second, T0 + H)
        self.assertEqual([run["id"] for run in self.store.runs()], [first, second])

    def test_ended_grow_drops_pending_phase(self):
        zone, run = self.grow(A)
        self.store.add_phase(run, "bloom", 720, 720, T0 + 24 * H, T0)
        self.store.end_run(run, T0 + H)
        self.assertEqual(len(self.store.phases(run)), 1)
        self.assertIsNone(self.store.open_run(zone))


class WebTests(Base):
    PW = "app-test-password"

    def setUp(self):
        super().setUp()
        self.reconciler.round()
        self.app = App(self.store, self.reconciler, self.PW, cookie_secure=False, clock=self.clock)
        self.server = make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cookie = ""

    def call(self, method, path, body=None, content_type="application/json", cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"Cookie": self.cookie if cookie is None else cookie}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = content_type
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data, response

    def login(self):
        status, _, response = self.call("POST", "/api/login", {"password": self.PW})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
        self.assertIn("SameSite=Strict", response.getheader("Set-Cookie"))
        self.cookie = response.getheader("Set-Cookie").split(";")[0]

    def post(self, path, body):
        status, data, _ = self.call("POST", path, body)
        return status, json.loads(data)

    def test_needs_login(self):
        self.assertEqual(self.call("GET", "/api/state")[0], 401)
        self.assertEqual(self.call("GET", "/api/state", cookie="session=123.abc")[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"password": "nope"})[0], 403)

    def test_login_is_rate_limited(self):
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/login", {"password": "x"})[0], 403)
        self.assertEqual(self.call("POST", "/api/login", {"password": self.PW})[0], 429)
        self.clock.now += 61
        self.assertEqual(self.call("POST", "/api/login", {"password": self.PW})[0], 200)

    def test_session_expires(self):
        self.login()
        self.clock.now += 31 * 86400
        self.assertEqual(self.call("GET", "/api/state")[0], 401)

    def test_posts_must_be_json(self):
        self.login()
        self.assertEqual(self.call("POST", "/api/zones", {}, content_type="text/plain")[0], 415)

    def test_static_and_headers(self):
        status, data, response = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"smartfritz", data.lower())
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(self.call("GET", "/../../etc/passwd")[0], 401)

    def test_installable_without_a_login(self):
        status, data, response = self.call("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/manifest+json")
        manifest = json.loads(data)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})
        for icon in manifest["icons"]:
            status, data, response = self.call("GET", icon["src"])
            self.assertEqual((status, response.getheader("Content-Type"), data[:4]), (200, "image/png", b"\x89PNG"))
        status, _, response = self.call("GET", "/sw.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", response.getheader("Content-Type"))
        policy = response.getheader("Content-Security-Policy")
        self.assertIn("manifest-src 'self'", policy)
        self.assertIn("worker-src 'self'", policy)

    def test_whole_flow(self):
        self.login()
        units = json.loads(self.call("GET", "/api/units")[1])
        self.assertEqual({u["uid"] for u in units}, {A, B, TRETAKT})
        status, zone = self.post("/api/zones", {"name": "Tent A", "unit_uid": A})
        self.assertEqual(status, 200)
        self.assertEqual(self.post("/api/zones", {"name": "dup", "unit_uid": A})[0], 409)
        self.assertEqual(self.post("/api/zones", {"name": "x", "unit_uid": "made up"})[0], 400)
        zid = zone["id"]
        status, run = self.post(f"/api/zones/{zid}/runs", {"name": "Grow 1", "kind": "veg", "on_minutes": 1080, "off_minutes": 360})
        self.assertEqual(status, 200)
        self.assertEqual(self.post(f"/api/zones/{zid}/runs", {"name": "two", "kind": "veg", "on_minutes": 60, "off_minutes": 60})[0], 409)
        self.reconciler.round()
        self.assertTrue(self.fake.active(A))
        state = json.loads(self.call("GET", "/api/state")[1])
        self.assertEqual(state["zones"][0]["desired"], True)
        self.assertEqual(state["zones"][0]["run"]["current"]["day"], 1)
        self.clock.now += 20 * H
        status, phase = self.post(f"/api/runs/{run['id']}/phases", {"kind": "bloom", "on_minutes": 720, "off_minutes": 720, "start": "next_on"})
        self.assertEqual(phase["starts"], T0 + 24 * H)
        self.assertEqual(json.loads(self.call("GET", "/api/state")[1])["zones"][0]["next_change"], T0 + 24 * H)
        status, hold = self.post(f"/api/zones/{zid}/hold", {"state": True})
        self.assertEqual(hold["until"], T0 + 24 * H)
        self.reconciler.round()
        self.assertTrue(self.fake.active(A))
        self.assertEqual(self.post(f"/api/zones/{zid}/hold", {"state": None})[0], 200)
        self.reconciler.round()
        self.assertFalse(self.fake.active(A))
        self.assertEqual(self.post(f"/api/runs/{run['id']}/end", {})[0], 200)
        history = json.loads(self.call("GET", "/api/runs")[1])
        self.assertIsNotNone(history[0]["ended_at"])
        self.assertIsNone(history[0]["current"])

    def test_bad_schedules(self):
        self.login()
        _, zone = self.post("/api/zones", {"name": "z", "unit_uid": A})
        for body in ({"kind": "x", "on_minutes": 1, "off_minutes": 1}, {"kind": "veg", "on_minutes": 0, "off_minutes": 0}, {"kind": "veg", "on_minutes": "1", "off_minutes": 1}):
            self.assertEqual(self.post(f"/api/zones/{zone['id']}/runs", {"name": "n", **body})[0], 400)

    def test_constant_schedule_can_be_held(self):
        self.login()
        _, zone = self.post("/api/zones", {"name": "z", "unit_uid": A})
        self.post(f"/api/zones/{zone['id']}/runs", {"name": "n", "kind": "veg", "on_minutes": 1440, "off_minutes": 0})
        _, hold = self.post(f"/api/zones/{zone['id']}/hold", {"state": False})
        self.assertEqual(hold["until"], T0 + H)

    def test_health(self):
        self.assertEqual(self.call("GET", "/healthz")[0], 200)
        self.clock.now += 1000
        self.assertEqual(self.call("GET", "/healthz")[0], 503)


class ErrorTypes(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(issubclass(FritzAuthError, FritzError))
        self.assertEqual(ApiError(400, "x").status, 400)


if __name__ == "__main__":
    unittest.main()

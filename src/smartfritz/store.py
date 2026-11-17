"""SQLite: zones (a named plug), runs (a grow), phases (a schedule in a grow), and what happened."""

from __future__ import annotations

import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zones (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  unit_uid TEXT NOT NULL UNIQUE,
  lamp_watts REAL,
  enabled INTEGER NOT NULL DEFAULT 1,
  hold_state INTEGER,
  hold_until REAL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  zone_id INTEGER NOT NULL REFERENCES zones(id),
  name TEXT NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_run ON runs(zone_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS phases (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  kind TEXT NOT NULL,
  on_minutes INTEGER NOT NULL,
  off_minutes INTEGER NOT NULL,
  started_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS phases_by_run ON phases(run_id, started_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  zone_id INTEGER REFERENCES zones(id),
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  active INTEGER,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_by_zone ON events(zone_id, ts);
CREATE TABLE IF NOT EXISTS samples (
  zone_id INTEGER NOT NULL REFERENCES zones(id),
  ts REAL NOT NULL,
  power_mw REAL,
  energy_wh REAL,
  celsius REAL
);
CREATE INDEX IF NOT EXISTS samples_by_zone ON samples(zone_id, ts);
"""

DAY = 86400
WEEK = 7 * DAY
KINDS = ("veg", "bloom", "other")


class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)

    def _all(self, sql: str, *args: object) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, *args: object) -> dict | None:
        rows = self._all(sql, *args)
        return rows[0] if rows else None

    def _run(self, sql: str, *args: object) -> int:
        with self.lock:
            return int(self.db.execute(sql, args).lastrowid or 0)

    # settings

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key=?", key)
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)

    # zones

    def zones(self) -> list[dict]:
        return self._all("SELECT * FROM zones ORDER BY id")

    def zone(self, zone_id: int) -> dict | None:
        return self._one("SELECT * FROM zones WHERE id=?", zone_id)

    def add_zone(self, name: str, unit_uid: str, lamp_watts: float | None) -> int:
        return self._run("INSERT INTO zones(name,unit_uid,lamp_watts) VALUES(?,?,?)", name, unit_uid, lamp_watts)

    def update_zone(self, zone_id: int, **fields: object) -> None:
        for key, value in fields.items():
            if key not in ("name", "lamp_watts", "enabled"):
                raise ValueError(key)
            self._run(f"UPDATE zones SET {key}=? WHERE id=?", value, zone_id)

    def set_hold(self, zone_id: int, state: bool | None, until: float | None) -> None:
        self._run("UPDATE zones SET hold_state=?, hold_until=? WHERE id=?", None if state is None else int(state), until, zone_id)

    # runs and phases

    def open_run(self, zone_id: int) -> dict | None:
        return self._one("SELECT * FROM runs WHERE zone_id=? AND ended_at IS NULL", zone_id)

    def run(self, run_id: int) -> dict | None:
        return self._one("SELECT * FROM runs WHERE id=?", run_id)

    def runs(self) -> list[dict]:
        return self._all("SELECT * FROM runs ORDER BY ended_at IS NOT NULL, started_at DESC")

    def start_run(self, zone_id: int, name: str, started_at: float, kind: str, on_minutes: int, off_minutes: int) -> int:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                run_id = int(self.db.execute(
                    "INSERT INTO runs(zone_id,name,started_at) VALUES(?,?,?)", (zone_id, name, started_at)
                ).lastrowid or 0)
                self.db.execute(
                    "INSERT INTO phases(run_id,kind,on_minutes,off_minutes,started_at) VALUES(?,?,?,?,?)",
                    (run_id, kind, on_minutes, off_minutes, started_at),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return run_id

    def end_run(self, run_id: int, now: float) -> None:
        with self.lock:
            self._run("DELETE FROM phases WHERE run_id=? AND started_at>?", run_id, now)
            self._run("UPDATE runs SET ended_at=? WHERE id=? AND ended_at IS NULL", now, run_id)

    def phases(self, run_id: int) -> list[dict]:
        return self._all("SELECT * FROM phases WHERE run_id=? ORDER BY started_at, id", run_id)

    def add_phase(self, run_id: int, kind: str, on_minutes: int, off_minutes: int, started_at: float, now: float) -> int:
        """Add the next phase; it replaces one that was waiting to start."""
        with self.lock:
            self._run("DELETE FROM phases WHERE run_id=? AND started_at>?", run_id, now)
            return self._run(
                "INSERT INTO phases(run_id,kind,on_minutes,off_minutes,started_at) VALUES(?,?,?,?,?)",
                run_id, kind, on_minutes, off_minutes, started_at,
            )

    def active_phase(self, run_id: int, now: float) -> dict | None:
        return self._one("SELECT * FROM phases WHERE run_id=? AND started_at<=? ORDER BY started_at DESC, id DESC LIMIT 1", run_id, now)

    def pending_phase(self, run_id: int, now: float) -> dict | None:
        return self._one("SELECT * FROM phases WHERE run_id=? AND started_at>? ORDER BY started_at LIMIT 1", run_id, now)

    # what happened

    def add_event(self, zone_id: int | None, kind: str, detail: str = "", active: bool | None = None, ts: float | None = None) -> None:
        self._run(
            "INSERT INTO events(zone_id,ts,kind,active,detail) VALUES(?,?,?,?,?)",
            zone_id, time.time() if ts is None else ts, kind, None if active is None else int(active), detail,
        )

    def events(self, zone_id: int | None = None, limit: int = 50) -> list[dict]:
        if zone_id is None:
            return self._all("SELECT * FROM events WHERE kind!='state' ORDER BY ts DESC, id DESC LIMIT ?", limit)
        return self._all("SELECT * FROM events WHERE zone_id=? AND kind!='state' ORDER BY ts DESC, id DESC LIMIT ?", zone_id, limit)

    def last_state(self, zone_id: int) -> bool | None:
        row = self._one("SELECT active FROM events WHERE zone_id=? AND kind='state' ORDER BY ts DESC, id DESC LIMIT 1", zone_id)
        return None if row is None or row["active"] is None else bool(row["active"])

    def record_state(self, zone_id: int, active: bool, ts: float) -> None:
        """Note what the plug is doing, when that changed."""
        if self.last_state(zone_id) is not active:
            self.add_event(zone_id, "state", active=active, ts=ts)

    def add_sample(self, zone_id: int, ts: float, power_mw: float | None, energy_wh: float | None, celsius: float | None) -> None:
        self._run("INSERT INTO samples(zone_id,ts,power_mw,energy_wh,celsius) VALUES(?,?,?,?,?)", zone_id, ts, power_mw, energy_wh, celsius)

    def light_seconds(self, zone_id: int, start: float, end: float) -> float:
        """How long the plug really was on between two moments."""
        before = self._one(
            "SELECT active FROM events WHERE zone_id=? AND kind='state' AND ts<=? ORDER BY ts DESC, id DESC LIMIT 1", zone_id, start
        )
        changes = self._all("SELECT ts, active FROM events WHERE zone_id=? AND kind='state' AND ts>? AND ts<? ORDER BY ts, id", zone_id, start, end)
        total, since, on = 0.0, start, bool(before and before["active"])
        for change in changes:
            if on:
                total += change["ts"] - since
            since, on = change["ts"], bool(change["active"])
        return total + (end - since if on else 0.0)

    def energy_wh(self, zone_id: int, start: float, end: float) -> float | None:
        """Metered energy between two moments, None without a meter. Counter resets are skipped."""
        rows = self._all("SELECT energy_wh FROM samples WHERE zone_id=? AND ts>=? AND ts<=? AND energy_wh IS NOT NULL ORDER BY ts", zone_id, start, end)
        if len(rows) < 2:
            return None
        return sum(max(0.0, b["energy_wh"] - a["energy_wh"]) for a, b in zip(rows, rows[1:], strict=False))

    # the numbers a grow is judged by

    def run_summary(self, run_id: int, now: float) -> dict | None:
        run = self.run(run_id)
        if run is None:
            return None
        zone = self.zone(run["zone_id"])
        assert zone is not None
        finish = min(run["ended_at"], now) if run["ended_at"] is not None else now
        started = [p for p in self.phases(run_id) if p["started_at"] <= finish]
        phases, totals = [], {}
        for index, phase in enumerate(started):
            end = started[index + 1]["started_at"] if index + 1 < len(started) else finish
            seconds = max(0.0, end - phase["started_at"])
            light = self.light_seconds(run["zone_id"], phase["started_at"], end)
            wh = self.energy_wh(run["zone_id"], phase["started_at"], end)
            estimated = False
            if wh is None and zone["lamp_watts"]:
                wh, estimated = zone["lamp_watts"] * light / 3600, True
            phases.append({
                **phase, "ended_at": end, "open": index + 1 == len(started) and run["ended_at"] is None,
                "seconds": seconds, "days": round(seconds / DAY, 1), "weeks": round(seconds / WEEK, 1),
                "light_hours": round(light / 3600, 1),
                "kwh": None if wh is None else round(wh / 1000, 2), "kwh_estimated": estimated,
            })
            total = totals.setdefault(phase["kind"], {"seconds": 0.0, "light_hours": 0.0, "kwh": None, "kwh_estimated": False})
            total["seconds"] += seconds
            total["light_hours"] = round(total["light_hours"] + light / 3600, 1)
            if wh is not None:
                total["kwh"] = round((total["kwh"] or 0.0) + wh / 1000, 2)
                total["kwh_estimated"] = total["kwh_estimated"] or estimated
        for total in totals.values():
            total["days"] = round(total["seconds"] / DAY, 1)
            total["weeks"] = round(total["seconds"] / WEEK, 1)
        return {**run, "zone_name": zone["name"], "phases": phases, "totals": totals, "current": self._current(started, finish, run)}

    @staticmethod
    def _current(started: list[dict], finish: float, run: dict) -> dict | None:
        """Day and week of the stage in progress; 18/6 into 20/4 is still the same veg."""
        if not started or run["ended_at"] is not None:
            return None
        kind = started[-1]["kind"]
        begin = started[-1]["started_at"]
        for phase in reversed(started):
            if phase["kind"] != kind:
                break
            begin = phase["started_at"]
        elapsed = max(0.0, finish - begin)
        return {"kind": kind, "since": begin, "day": int(elapsed // DAY) + 1, "week": int(elapsed // WEEK) + 1}

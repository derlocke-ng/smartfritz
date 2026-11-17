"""The loop: work out what each plug should be, compare, correct.

Nothing here is an "at 06:00 turn on" event. Each round computes the wanted state
from the stored schedule, so a restart or a missed switch is corrected on the
next round.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .fritz import FritzClient, FritzError, Unit
from .schedule import Schedule, evaluate
from .store import Store

SAMPLE_EVERY = 300.0
NO_LOAD_MW = 1000.0
NOT_FOLLOWING_AFTER = 4
NO_LOAD_AFTER = 2
RECHECK_AFTER = 5.0


@dataclass
class ZoneRuntime:
    last_desired: bool | None = None
    mismatches: int = 0
    rounds_on: int = 0
    last_sample: float = 0.0
    problem: str = ""


class Reconciler:
    def __init__(
        self,
        store: Store,
        fritz: FritzClient,
        tick: float = 30.0,
        stagger: float = 3.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.fritz = fritz
        self.tick_seconds = tick
        self.stagger = stagger
        self.clock = clock
        self.sleep = sleep
        self.runtime: dict[int, ZoneRuntime] = {}
        self.units: dict[str, Unit] = {}
        self.box_error = ""
        self.last_round = 0.0
        self.recheck = False

    def desired(self, zone: dict, now: float) -> tuple[bool | None, str, float | None]:
        """Return (wanted state or None to leave it alone, why, when it next changes)."""
        if zone["hold_state"] is not None:
            if zone["hold_until"] is not None and now < zone["hold_until"]:
                return bool(zone["hold_state"]), "hold", zone["hold_until"]
            self.store.set_hold(zone["id"], None, None)
            self.store.add_event(zone["id"], "hold_ended", ts=now)
        run = self.store.open_run(zone["id"])
        if run is None:
            return None, "no grow", None
        phase = self.store.active_phase(run["id"], now)
        if phase is None:
            return None, "not started", None
        schedule = Schedule(phase["on_minutes"], phase["off_minutes"])
        state, change = evaluate(schedule, phase["started_at"], now)
        pending = self.store.pending_phase(run["id"], now)
        if pending and (change is None or pending["started_at"] < change):
            change = pending["started_at"]
        return state, "schedule", change

    def round(self) -> None:
        now = self.clock()
        self.recheck = False
        try:
            self.units = {unit.uid: unit for unit in self.fritz.units()}
        except FritzError as error:
            if str(error) != self.box_error:
                self.store.add_event(None, "box_error", str(error), ts=now)
            self.box_error = str(error)
            self.last_round = now
            return
        if self.box_error:
            self.store.add_event(None, "box_ok", ts=now)
        self.box_error = ""
        switched = False
        for zone in self.store.zones():
            if zone["enabled"]:
                switched = self._zone(zone, now, switched) or switched
        self.last_round = now

    def _set_problem(self, zone: dict, runtime: ZoneRuntime, problem: str, now: float) -> None:
        if problem != runtime.problem:
            self.store.add_event(zone["id"], "problem" if problem else "problem_cleared", problem or runtime.problem, ts=now)
        runtime.problem = problem

    def _zone(self, zone: dict, now: float, switched: bool) -> bool:
        runtime = self.runtime.setdefault(zone["id"], ZoneRuntime())
        unit = self.units.get(zone["unit_uid"])
        if unit is None:
            self._set_problem(zone, runtime, "the box does not list this plug", now)
            return False
        if not unit.connected:
            self._set_problem(zone, runtime, "plug offline", now)
            return False
        if unit.active is not None:
            self.store.record_state(zone["id"], unit.active, now)
        if now - runtime.last_sample >= SAMPLE_EVERY and (unit.power_mw is not None or unit.celsius is not None):
            self.store.add_sample(zone["id"], now, unit.power_mw, unit.energy_wh, unit.celsius)
            runtime.last_sample = now
        desired, reason, _ = self.desired(zone, now)
        changed = desired is not None and desired != runtime.last_desired
        cause = "startup" if runtime.last_desired is None else reason if changed else "correction"
        if desired is not None:
            runtime.last_desired = desired
        if desired is None or unit.active is None:
            self._set_problem(zone, runtime, self._condition(unit, runtime, False), now)
            return False
        if unit.active == desired:
            runtime.mismatches = 0
            runtime.rounds_on = runtime.rounds_on + 1 if unit.active else 0
            self._set_problem(zone, runtime, self._condition(unit, runtime, True), now)
            return False
        runtime.rounds_on = 0
        if unit.locked:
            self._set_problem(zone, runtime, "the plug is locked against API switching", now)
            return False
        runtime.mismatches += 1
        if switched:
            self.sleep(self.stagger)  # lamp drivers draw a spike at switch-on: not all at once
        try:
            self.fritz.set_on_off(zone["unit_uid"], desired)
        except FritzError as error:
            self.store.add_event(zone["id"], "switch_failed", str(error), active=desired, ts=now)
            self._set_problem(zone, runtime, f"switching failed: {error}", now)
            return True
        self.store.add_event(zone["id"], "switch", cause, active=desired, ts=now)
        # Read it back soon after the first try, so the page and the light hours do not
        # lag a whole tick. Retries keep the normal pace: radios can be slow to report.
        self.recheck = self.recheck or runtime.mismatches == 1
        if runtime.mismatches >= NOT_FOLLOWING_AFTER:
            self._set_problem(zone, runtime, f"the plug is not following ({runtime.mismatches} tries)", now)
        return True

    @staticmethod
    def _condition(unit: Unit, runtime: ZoneRuntime, settled: bool) -> str:
        if unit.outlet_state != "valid":
            return f"outlet reports {unit.outlet_state}"
        if settled and unit.active and unit.power_mw is not None and runtime.rounds_on > NO_LOAD_AFTER and unit.power_mw < NO_LOAD_MW:
            return "plug is on but nothing draws power: check the lamp"
        return ""

    def next_wait(self, now: float) -> float:
        """Sleep a whole tick, or less when a plug is due to change before then."""
        wait = min(self.tick_seconds, RECHECK_AFTER) if self.recheck else self.tick_seconds
        for zone in self.store.zones():
            if zone["enabled"]:
                _, _, change = self.desired(zone, now)
                if change is not None:
                    wait = min(wait, max(1.0, change - now + 0.5))
        return wait

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.round()
            except Exception as error:  # the lights outlive any one bad round
                self.box_error = f"internal error: {error!r}"
                self.store.add_event(None, "internal_error", repr(error))
            stop.wait(self.next_wait(self.clock()))

"""What a light should be doing at a given moment.

A cycle counts elapsed seconds from its anchor, the moment of its first
lights-on. It never looks at the clock on the wall, so any length works (4/4 as
well as 18/6) and a daylight saving change does not stretch or cut a night.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_MINUTES = 7 * 24 * 60


@dataclass(frozen=True)
class Schedule:
    on_minutes: int
    off_minutes: int

    def __post_init__(self) -> None:
        for value in (self.on_minutes, self.off_minutes):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("minutes must be whole numbers")
            if not 0 <= value <= MAX_MINUTES:
                raise ValueError(f"minutes must be between 0 and {MAX_MINUTES}")
        if self.on_minutes + self.off_minutes == 0:
            raise ValueError("a schedule needs some on or off time")

    @property
    def period(self) -> int:
        return (self.on_minutes + self.off_minutes) * 60

    @property
    def constant(self) -> bool:
        return self.on_minutes == 0 or self.off_minutes == 0


def evaluate(schedule: Schedule, anchor: float, now: float) -> tuple[bool, float | None]:
    """Return (should be on, when that next changes). None: it never changes."""
    if schedule.off_minutes == 0:
        return True, None
    if schedule.on_minutes == 0:
        return False, None
    position = (now - anchor) % schedule.period
    on_seconds = schedule.on_minutes * 60
    if position < on_seconds:
        return True, now + on_seconds - position
    return False, now + schedule.period - position


def next_lights_on(schedule: Schedule, anchor: float, now: float) -> float:
    """The start of the next cycle: where a new schedule can begin without cutting a night."""
    if schedule.constant:
        return now
    return now + schedule.period - (now - anchor) % schedule.period

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _cron_weekday(moment: datetime) -> int:
    return (moment.weekday() + 1) % 7


@dataclass(frozen=True)
class CronField:
    allowed: frozenset[int]

    @classmethod
    def parse(cls, expression: str, minimum: int, maximum: int, *, allow_seven=False) -> "CronField":
        values: set[int] = set()
        for chunk in expression.split(","):
            chunk = chunk.strip()
            if not chunk:
                raise ValueError("empty cron field")
            step = 1
            if "/" in chunk:
                base, step_text = chunk.split("/", 1)
                step = int(step_text)
                if step <= 0:
                    raise ValueError("cron step must be positive")
            else:
                base = chunk
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start_text, end_text = base.split("-", 1)
                start = int(start_text)
                end = int(end_text)
            else:
                start = end = int(base)
            if allow_seven and start == 7:
                start = 0
            if allow_seven and end == 7:
                end = 0
            if start < minimum or start > maximum:
                raise ValueError(f"cron value {start} out of range")
            if end < minimum or end > maximum:
                raise ValueError(f"cron value {end} out of range")
            if start <= end:
                current_values = range(start, end + 1, step)
            else:
                current_values = list(range(start, maximum + 1, step)) + list(
                    range(minimum, end + 1, step)
                )
            values.update(current_values)
        return cls(allowed=frozenset(values))

    def matches(self, value: int) -> bool:
        return value in self.allowed


@dataclass(frozen=True)
class CronSchedule:
    minute: CronField
    hour: CronField
    day: CronField
    month: CronField
    weekday: CronField

    @classmethod
    def parse(cls, expression: str) -> "CronSchedule":
        normalized = MACROS.get(expression.strip().lower(), expression.strip())
        parts = normalized.split()
        if len(parts) != 5:
            raise ValueError(f"expected 5 cron fields, got {len(parts)}")
        return cls(
            minute=CronField.parse(parts[0], 0, 59),
            hour=CronField.parse(parts[1], 0, 23),
            day=CronField.parse(parts[2], 1, 31),
            month=CronField.parse(parts[3], 1, 12),
            weekday=CronField.parse(parts[4], 0, 6, allow_seven=True),
        )

    def matches(self, moment: datetime) -> bool:
        return (
            self.minute.matches(moment.minute)
            and self.hour.matches(moment.hour)
            and self.day.matches(moment.day)
            and self.month.matches(moment.month)
            and self.weekday.matches(_cron_weekday(moment))
        )

    def next_after(self, moment: datetime) -> datetime:
        candidate = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = candidate + timedelta(days=366)
        while candidate <= limit:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise RuntimeError("unable to find next cron match within 366 days")

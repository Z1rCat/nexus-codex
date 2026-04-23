from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from nexus_codex.cron import CronSchedule


class CronScheduleTests(unittest.TestCase):
    def test_next_after_every_fifteen_minutes(self) -> None:
        schedule = CronSchedule.parse("*/15 * * * *")
        moment = datetime(2026, 4, 22, 10, 7, tzinfo=ZoneInfo("UTC"))
        expected = datetime(2026, 4, 22, 10, 15, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(schedule.next_after(moment), expected)


if __name__ == "__main__":
    unittest.main()

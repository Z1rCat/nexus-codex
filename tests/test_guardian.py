from __future__ import annotations

import unittest

from nexus_codex.guardian import (
    GuardianPolicy,
    GuardianState,
    apply_pause_decision,
    can_schedule,
    pause,
    record_run_outcome,
    recommend_pause,
    resume,
)


class GuardianTests(unittest.TestCase):
    def test_state_from_mapping_coerces_store_values(self) -> None:
        state = GuardianState.from_mapping(
            {
                "paused": 1,
                "pause_reason": "operator pause",
                "consecutive_failures": "2",
                "daily_wake_date": "2026-04-22",
                "daily_wake_count": "3",
                "last_run_id": "17",
                "last_status": "failed",
            }
        )

        self.assertTrue(state.paused)
        self.assertEqual(state.pause_reason, "operator pause")
        self.assertEqual(state.consecutive_failures, 2)
        self.assertEqual(state.daily_wake_date, "2026-04-22")
        self.assertEqual(state.daily_wake_count, 3)
        self.assertEqual(state.last_run_id, 17)
        self.assertEqual(state.last_status, "failed")
        self.assertEqual(state.to_mapping()["daily_wake_count"], 3)

    def test_can_schedule_blocks_paused_job_first(self) -> None:
        policy = GuardianPolicy(max_daily_wakes=3, max_consecutive_failures=2)
        state = GuardianState(
            paused=True,
            pause_reason="waiting for operator",
            consecutive_failures=99,
            daily_wake_date="2026-04-22",
            daily_wake_count=99,
        )

        decision = can_schedule(policy, state, local_date="2026-04-22")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "waiting for operator")

    def test_can_schedule_blocks_exhausted_daily_budget(self) -> None:
        policy = GuardianPolicy(max_daily_wakes=3)
        state = GuardianState(daily_wake_date="2026-04-22", daily_wake_count=3)

        decision = can_schedule(policy, state, local_date="2026-04-22")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "daily wake budget exhausted")

    def test_can_schedule_ignores_previous_day_budget(self) -> None:
        policy = GuardianPolicy(max_daily_wakes=1)
        state = GuardianState(daily_wake_date="2026-04-21", daily_wake_count=99)

        decision = can_schedule(policy, state, local_date="2026-04-22")

        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.reason)

    def test_can_schedule_blocks_failure_limit(self) -> None:
        policy = GuardianPolicy(max_consecutive_failures=2)
        state = GuardianState(consecutive_failures=2)

        decision = can_schedule(policy, state, local_date="2026-04-22")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "consecutive failure limit reached")

    def test_record_run_outcome_updates_same_day_counters_for_failure(self) -> None:
        state = GuardianState(
            consecutive_failures=1,
            daily_wake_date="2026-04-22",
            daily_wake_count=2,
        )

        updated = record_run_outcome(
            state,
            status="verification_failed",
            local_date="2026-04-22",
            run_id=11,
        )

        self.assertEqual(updated.daily_wake_date, "2026-04-22")
        self.assertEqual(updated.daily_wake_count, 3)
        self.assertEqual(updated.consecutive_failures, 2)
        self.assertEqual(updated.last_run_id, 11)
        self.assertEqual(updated.last_status, "verification_failed")

    def test_record_run_outcome_resets_new_day_and_success(self) -> None:
        state = GuardianState(
            consecutive_failures=4,
            daily_wake_date="2026-04-21",
            daily_wake_count=7,
        )

        updated = record_run_outcome(
            state,
            status="completed",
            local_date="2026-04-22",
            run_id=12,
        )

        self.assertEqual(updated.daily_wake_date, "2026-04-22")
        self.assertEqual(updated.daily_wake_count, 1)
        self.assertEqual(updated.consecutive_failures, 0)
        self.assertEqual(updated.last_run_id, 12)
        self.assertEqual(updated.last_status, "completed")

    def test_recommend_pause_auto_pauses_at_failure_limit(self) -> None:
        policy = GuardianPolicy(max_consecutive_failures=3)
        state = GuardianState(consecutive_failures=3)

        decision = recommend_pause(policy, state)
        updated = apply_pause_decision(policy, state)

        self.assertTrue(decision.paused)
        self.assertTrue(decision.changed)
        self.assertEqual(
            decision.reason,
            "auto-paused after 3 consecutive failures",
        )
        self.assertTrue(updated.paused)
        self.assertEqual(updated.pause_reason, decision.reason)

    def test_recommend_pause_preserves_existing_pause(self) -> None:
        policy = GuardianPolicy(max_consecutive_failures=1)
        state = GuardianState(paused=True, pause_reason="manual stop", consecutive_failures=5)

        decision = recommend_pause(policy, state)
        updated = apply_pause_decision(policy, state)

        self.assertTrue(decision.paused)
        self.assertFalse(decision.changed)
        self.assertEqual(decision.reason, "manual stop")
        self.assertEqual(updated, state)

    def test_pause_and_resume_helpers_round_trip(self) -> None:
        state = GuardianState()

        paused_state = pause(state, "maintenance window")
        resumed_state = resume(paused_state)

        self.assertTrue(paused_state.paused)
        self.assertEqual(paused_state.pause_reason, "maintenance window")
        self.assertFalse(resumed_state.paused)
        self.assertIsNone(resumed_state.pause_reason)

    def test_negative_policy_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GuardianPolicy(max_daily_wakes=-1)
        with self.assertRaises(ValueError):
            GuardianPolicy(max_consecutive_failures=-1)


if __name__ == "__main__":
    unittest.main()

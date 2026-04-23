from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

FAILURE_STATUSES = frozenset({"failed", "verification_failed"})


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "no", "off"}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)


def _coerce_optional_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


@dataclass(frozen=True)
class GuardianPolicy:
    max_daily_wakes: int | None = None
    max_consecutive_failures: int = 0

    def __post_init__(self) -> None:
        if self.max_daily_wakes is not None and self.max_daily_wakes < 0:
            raise ValueError("max_daily_wakes must be non-negative or None")
        if self.max_consecutive_failures < 0:
            raise ValueError("max_consecutive_failures must be non-negative")


@dataclass(frozen=True)
class GuardianState:
    paused: bool = False
    pause_reason: str | None = None
    consecutive_failures: int = 0
    daily_wake_date: str | None = None
    daily_wake_count: int = 0
    last_run_id: int | None = None
    last_status: str | None = None

    def __post_init__(self) -> None:
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must be non-negative")
        if self.daily_wake_count < 0:
            raise ValueError("daily_wake_count must be non-negative")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> GuardianState:
        return cls(
            paused=_coerce_bool(payload.get("paused", False)),
            pause_reason=_coerce_optional_str(payload.get("pause_reason")),
            consecutive_failures=int(payload.get("consecutive_failures", 0) or 0),
            daily_wake_date=_coerce_optional_str(payload.get("daily_wake_date")),
            daily_wake_count=int(payload.get("daily_wake_count", 0) or 0),
            last_run_id=_coerce_optional_int(payload.get("last_run_id")),
            last_status=_coerce_optional_str(payload.get("last_status")),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "consecutive_failures": self.consecutive_failures,
            "daily_wake_date": self.daily_wake_date,
            "daily_wake_count": self.daily_wake_count,
            "last_run_id": self.last_run_id,
            "last_status": self.last_status,
        }


@dataclass(frozen=True)
class ScheduleDecision:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class PauseDecision:
    paused: bool
    reason: str | None = None
    changed: bool = False


def current_daily_wake_count(state: GuardianState, *, local_date: str) -> int:
    if state.daily_wake_date != local_date:
        return 0
    return state.daily_wake_count


def daily_wake_budget_exhausted(
    policy: GuardianPolicy,
    state: GuardianState,
    *,
    local_date: str,
) -> bool:
    if policy.max_daily_wakes is None:
        return False
    return current_daily_wake_count(state, local_date=local_date) >= policy.max_daily_wakes


def consecutive_failure_limit_reached(
    policy: GuardianPolicy,
    state: GuardianState,
) -> bool:
    if policy.max_consecutive_failures <= 0:
        return False
    return state.consecutive_failures >= policy.max_consecutive_failures


def can_schedule(
    policy: GuardianPolicy,
    state: GuardianState,
    *,
    local_date: str,
) -> ScheduleDecision:
    if state.paused:
        return ScheduleDecision(False, state.pause_reason or "paused")
    if daily_wake_budget_exhausted(policy, state, local_date=local_date):
        return ScheduleDecision(False, "daily wake budget exhausted")
    if consecutive_failure_limit_reached(policy, state):
        return ScheduleDecision(False, "consecutive failure limit reached")
    return ScheduleDecision(True, None)


def record_run_outcome(
    state: GuardianState,
    *,
    status: str,
    local_date: str,
    run_id: int | None = None,
) -> GuardianState:
    wake_count = current_daily_wake_count(state, local_date=local_date) + 1
    consecutive_failures = (
        state.consecutive_failures + 1 if status in FAILURE_STATUSES else 0
    )
    return replace(
        state,
        consecutive_failures=consecutive_failures,
        daily_wake_date=local_date,
        daily_wake_count=wake_count,
        last_run_id=run_id,
        last_status=status,
    )


def recommend_pause(policy: GuardianPolicy, state: GuardianState) -> PauseDecision:
    if state.paused:
        return PauseDecision(True, state.pause_reason or "paused", changed=False)
    if consecutive_failure_limit_reached(policy, state):
        return PauseDecision(
            True,
            f"auto-paused after {state.consecutive_failures} consecutive failures",
            changed=True,
        )
    return PauseDecision(False, None, changed=False)


def apply_pause_decision(policy: GuardianPolicy, state: GuardianState) -> GuardianState:
    decision = recommend_pause(policy, state)
    if not decision.changed:
        return state
    return replace(state, paused=decision.paused, pause_reason=decision.reason)


def pause(state: GuardianState, reason: str) -> GuardianState:
    return replace(state, paused=True, pause_reason=reason)


def resume(state: GuardianState) -> GuardianState:
    return replace(state, paused=False, pause_reason=None)


__all__ = [
    "FAILURE_STATUSES",
    "GuardianPolicy",
    "GuardianState",
    "PauseDecision",
    "ScheduleDecision",
    "apply_pause_decision",
    "can_schedule",
    "consecutive_failure_limit_reached",
    "current_daily_wake_count",
    "daily_wake_budget_exhausted",
    "pause",
    "record_run_outcome",
    "recommend_pause",
    "resume",
]

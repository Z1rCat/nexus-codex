from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from nexus_codex.codex_runner import CodexRunner
from nexus_codex.config import load_config
from nexus_codex.guardian import (
    GuardianPolicy,
    GuardianState,
    can_schedule,
    pause as guardian_pause,
    record_run_outcome,
    resume as guardian_resume,
    apply_pause_decision,
)
from nexus_codex.models import JobConfig, WorkspaceLease
from nexus_codex.store import StateStore
from nexus_codex.verifier import Verifier
from nexus_codex.worktree import WorktreeManager


def _planning_prompt(job: JobConfig) -> str:
    return (
        "Planning mode only. Do not modify files or run mutating commands.\n\n"
        "Original task:\n"
        f"{job.prompt}\n\n"
        "Return a concise execution plan with the exact validation steps you would run."
    )


def _approved_execution_prompt(job: JobConfig, plan_text: str) -> str:
    return (
        "The following plan has been reviewed and approved. Execute it conservatively.\n\n"
        "Approved plan:\n"
        f"{plan_text}\n\n"
        "Original task:\n"
        f"{job.prompt}\n\n"
        "Implement the plan. If the repository changed since planning, adapt carefully and"
        " summarize any deviation in the final message."
    )


def _persistent_wake_prompt(job: JobConfig) -> str:
    if job.wake_prompt:
        return job.wake_prompt
    return (
        "Continue the existing autonomous session. Review the current repository state and any "
        "new context since the previous turn, then take the next best action toward the goal.\n\n"
        f"Original goal:\n{job.prompt}"
    )


def _inbox_prompt(job: JobConfig, task_name: str, task_body: str) -> str:
    return (
        f"{job.prompt}\n\n"
        f"New task source: {task_name}\n\n"
        "Task content:\n"
        f"{task_body.strip()}\n\n"
        "Analyze this task, decide the correct next action, and handle it within the repository "
        "context."
    )


def _tail_text(path: Path, limit: int = 8000) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) <= limit:
        return text
    return text[-limit:]


def _verifier_retry_prompt(
    job: JobConfig,
    *,
    failed_command: str | None,
    verifier_log_path: Path,
    attempt_number: int,
    max_attempts: int,
) -> str:
    return (
        "Verifier failed after your previous turn. Continue in the same session and fix only "
        "what is necessary for the checks to pass.\n\n"
        f"Original task:\n{job.prompt}\n\n"
        f"Repair attempt: {attempt_number} of {max_attempts}\n"
        f"Failed command: {failed_command or 'unknown'}\n\n"
        "Verifier log tail:\n"
        f"{_tail_text(verifier_log_path)}\n\n"
        "Make the smallest credible change, then stop."
    )


def _role_planner_prompt(job: JobConfig, prompt: str) -> str:
    return (
        "Role: planner\n"
        "You are the planning agent. Do not modify files or run mutating commands.\n\n"
        f"Primary task:\n{prompt}\n\n"
        "Return a concise execution plan with risks and validation steps."
    )


def _role_executor_prompt(
    job: JobConfig,
    *,
    base_prompt: str,
    plan_text: str | None,
    review_feedback: str | None,
) -> str:
    parts = [
        "Role: executor",
        "You are the execution agent. Modify only what is necessary and keep the change set tight.",
        f"Primary task:\n{base_prompt}",
    ]
    if plan_text:
        parts.append(f"Planner context:\n{plan_text}")
    if review_feedback:
        parts.append(f"Reviewer feedback:\n{review_feedback}")
    parts.append("Implement the task and summarize the concrete changes you made.")
    return "\n\n".join(parts)


def _parallel_executor_prompt(
    job: JobConfig,
    *,
    base_prompt: str,
    plan_text: str | None,
    branch_label: str,
    branch_strategy: str,
) -> str:
    parts = [
        "Role: executor",
        f"Branch label: {branch_label}",
        f"Branch strategy: {branch_strategy}",
        "You are one executor branch among several parallel candidates. Produce a distinct candidate implementation in your isolated workspace.",
        f"Primary task:\n{base_prompt}",
    ]
    if plan_text:
        parts.append(f"Planner context:\n{plan_text}")
    parts.append("Implement according to your branch strategy and summarize the candidate you produced.")
    return "\n\n".join(parts)


def _role_reviewer_prompt(
    job: JobConfig,
    *,
    base_prompt: str,
    executor_summary: str,
    plan_text: str | None,
) -> str:
    parts = [
        "Role: reviewer",
        "You are the reviewer agent. Do not modify files.",
        "Inspect the current workspace and decide whether the executor output is ready.",
        "Respond with `REVIEW_DECISION: approve` or `REVIEW_DECISION: revise` on the first line.",
        "Then provide a concise review summary and actionable feedback when revision is needed.",
        f"Primary task:\n{base_prompt}",
        f"Executor summary:\n{executor_summary}",
    ]
    if plan_text:
        parts.append(f"Planner context:\n{plan_text}")
    return "\n\n".join(parts)


def _reviewer_branch_selection_prompt(
    *,
    base_prompt: str,
    plan_text: str | None,
    candidates: list[dict[str, object]],
) -> str:
    parts = [
        "Role: reviewer",
        "Branch candidates:",
        "Choose the best executor branch before final review.",
        "The first non-empty line must be `BRANCH_CHOICE: <branch_label>`.",
        f"Primary task:\n{base_prompt}",
    ]
    if plan_text:
        parts.append(f"Planner context:\n{plan_text}")
    for candidate in candidates:
        parts.append(
            f"Candidate {candidate['branch_label']}:\n{candidate['message_text']}"
        )
    return "\n\n".join(parts)


def _parallel_executor_specs(count: int) -> list[tuple[str, str]]:
    defaults = [
        (
            "branch-minimal",
            "Favor the smallest credible diff and the narrowest surface area.",
        ),
        (
            "branch-robust",
            "Favor correctness, validation, and handling likely edge cases.",
        ),
        (
            "branch-maintainable",
            "Favor clarity, explicit structure, and long-term maintainability.",
        ),
        (
            "branch-pragmatic",
            "Favor a balanced implementation that stays simple without being fragile.",
        ),
    ]
    if count <= len(defaults):
        return defaults[:count]
    specs = list(defaults)
    for index in range(len(defaults), count):
        specs.append(
            (
                f"branch-alt-{index + 1}",
                "Produce another distinct conservative candidate with a different tradeoff.",
            )
        )
    return specs


def _goal_protocol_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "Goal supervision protocol:\n"
        "- The first non-empty line of your final message must be exactly one of:\n"
        "  GOAL_STATUS: continue\n"
        "  GOAL_STATUS: idle\n"
        "  GOAL_STATUS: blocked\n"
        "  GOAL_STATUS: done\n"
        "- Use `continue` when the goal should wake again soon.\n"
        "- Use `idle` when there is no productive next step right now but a later wake might help.\n"
        "- Use `blocked` when external input, approval, or missing context prevents progress.\n"
        "- Use `done` only when the goal is complete.\n"
        "- After the status line, summarize progress and the next best action."
    )


class Runtime:
    def __init__(self, config_path: str | Path) -> None:
        self.config = load_config(config_path)
        self.timezone = ZoneInfo(self.config.app.timezone)
        self.config.app.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(self.config.app.state_dir / "state.db")
        self.runner = CodexRunner(self.config.app)
        self.worktrees = WorktreeManager(self.config.app.state_dir)
        self.verifier = Verifier()

    def execute_job(self, job_id: str, *, trigger: str = "manual") -> int:
        task_id = self.dispatch_job(job_id, trigger=trigger)
        return self.run_task(task_id)

    def execute_inbox_task(self, job_id: str, task_path: str | Path) -> int:
        task_id = self.dispatch_inbox_task(job_id, task_path)
        return self.run_task(task_id)

    def dispatch_job(self, job_id: str, *, trigger: str = "manual") -> int:
        job = self._job(job_id)
        return self._create_dispatch_task(
            job,
            trigger=trigger,
            task_path=None,
            task_source=None,
        )

    def dispatch_inbox_task(self, job_id: str, task_path: str | Path) -> int:
        job = self._job(job_id)
        source_path = Path(task_path).resolve()
        claimed_path = self._claim_inbox_item(job, source_path)
        try:
            return self._create_dispatch_task(
                job,
                trigger=f"inbox:{claimed_path.name}",
                task_path=claimed_path,
                task_source=str(source_path),
            )
        except Exception:
            self._archive_inbox_item(job, claimed_path, success=False)
            raise

    def dispatch_inbox(self, job_id: str, *, limit: int | None = None) -> list[int]:
        dispatched: list[int] = []
        while limit is None or len(dispatched) < limit:
            next_item = self.next_inbox_item(job_id)
            if next_item is None:
                break
            dispatched.append(self.dispatch_inbox_task(job_id, next_item))
        return dispatched

    def run_task(self, task_id: int) -> int:
        task = dict(self.store.get_task(task_id))
        job = self._job(str(task["job_id"]))
        task_path = Path(str(task["payload_path"])) if task.get("payload_path") else None
        try:
            run_id = self._with_job_lease(
                job,
                lambda heartbeat: self._run_dispatched_task_locked(
                    job,
                    task,
                    lease_heartbeat=heartbeat,
                ),
            )
        except Exception:
            if task["kind"] == "inbox_item" and task_path is not None:
                self._archive_inbox_item(job, task_path, success=False)
            raise
        self._record_job_state(job, run_id)
        if task["kind"] == "inbox_item" and task_path is not None:
            row = self.show_run(run_id)
            self._archive_inbox_item(job, task_path, success=(row["status"] == "completed"))
        return run_id

    def run_next_task(self, job_id: str) -> int | None:
        next_task = self.store.next_ready_task(job_id)
        if next_task is None:
            return None
        return self.run_task(int(next_task["id"]))

    def next_ready_task(self, job_id: str) -> dict[str, object] | None:
        row = self.store.next_ready_task(job_id)
        return dict(row) if row is not None else None

    def next_inbox_item(self, job_id: str) -> Path | None:
        job = self._job(job_id)
        if not job.inbox_dir:
            return None
        job.inbox_dir.mkdir(parents=True, exist_ok=True)
        candidates = sorted(
            path for path in job.inbox_dir.glob(job.inbox_glob) if path.is_file()
        )
        return candidates[0] if candidates else None

    def list_jobs(self) -> dict[str, JobConfig]:
        return dict(self.config.jobs)

    def dashboard(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for job_id, job in sorted(self.config.jobs.items()):
            task_counts = self.store.count_tasks_by_status(job_id)
            state = dict(self.store.get_job_state(job_id))
            latest_run = self.store.get_latest_run_for_job(job_id)
            latest_goal = self.store.get_latest_goal(job_id) if job.goal_loop else None
            session = self.store.get_session(job_id)
            allowed, reason = self.can_schedule_job(job_id)
            rows.append(
                {
                    "job_id": job_id,
                    "enabled": job.enabled,
                    "mode": job.mode,
                    "paused": bool(state["paused"]),
                    "can_schedule_now": allowed,
                    "schedule_block_reason": reason,
                    "ready_tasks": task_counts.get("ready", 0),
                    "queued_tasks": task_counts.get("queued", 0),
                    "running_tasks": task_counts.get("running", 0),
                    "awaiting_approval_tasks": task_counts.get("awaiting_approval", 0),
                    "failed_tasks": (
                        task_counts.get("failed", 0)
                        + task_counts.get("verification_failed", 0)
                    ),
                    "inbox_backlog": self._inbox_backlog(job),
                    "last_run_id": int(latest_run["id"]) if latest_run else None,
                    "last_run_status": str(latest_run["status"]) if latest_run else None,
                    "last_run_created_at": (
                        str(latest_run["created_at"]) if latest_run else None
                    ),
                    "session_thread_id": (
                        str(session["codex_thread_id"]) if session else None
                    ),
                    "session_last_status": (
                        str(session["last_status"]) if session else None
                    ),
                    "goal_status": (
                        str(latest_goal["status"]) if latest_goal else None
                    ),
                    "goal_last_outcome": (
                        str(latest_goal["last_outcome"]) if latest_goal else None
                    ),
                    "goal_next_wake_at": (
                        str(latest_goal["next_wake_at"]) if latest_goal else None
                    ),
                }
            )
        return rows

    def sessions(self, limit: int = 20, *, job_id: str | None = None) -> list[dict[str, object]]:
        return [dict(row) for row in self.store.list_sessions(limit=limit, job_id=job_id)]

    def job_status(self, job_id: str) -> dict[str, object]:
        job = self._job(job_id)
        state = dict(self.store.get_job_state(job.id))
        session = self.store.get_session(job.id)
        goal = self.store.get_latest_goal(job.id) if job.goal_loop else None
        task_counts = self.store.count_tasks_by_status(job.id)
        state["max_daily_wakes"] = job.max_daily_wakes
        state["max_consecutive_failures"] = job.max_consecutive_failures
        state["persistent_session"] = job.persistent_session
        state["inbox_dir"] = str(job.inbox_dir) if job.inbox_dir else None
        state["inbox_backlog"] = self._inbox_backlog(job)
        state["goal_loop"] = job.goal_loop
        state["task_counts"] = task_counts
        allowed, reason = self.can_schedule_job(job_id)
        state["can_schedule_now"] = allowed
        state["schedule_block_reason"] = reason
        state["session"] = dict(session) if session else None
        state["goal"] = dict(goal) if goal else None
        return state

    def goals(self, limit: int = 20, *, job_id: str | None = None) -> list[dict[str, object]]:
        return [dict(row) for row in self.store.list_goals(limit=limit, job_id=job_id)]

    def show_goal(self, goal_id: int) -> dict[str, object]:
        return dict(self.store.get_goal(goal_id))

    def pause_job(self, job_id: str, reason: str) -> None:
        job = self._job(job_id)
        state = GuardianState.from_mapping(dict(self.store.get_job_state(job.id)))
        self.store.write_job_state(job.id, guardian_pause(state, reason).to_mapping())

    def resume_job_control(self, job_id: str) -> None:
        job = self._job(job_id)
        state = GuardianState.from_mapping(dict(self.store.get_job_state(job.id)))
        self.store.write_job_state(job.id, guardian_resume(state).to_mapping())

    def add_operator_note(
        self,
        job_id: str,
        note: str,
        *,
        dispatch: bool = False,
        run_now: bool = False,
    ) -> dict[str, object]:
        job = self._job(job_id)
        note_text = note.strip()
        if not note_text:
            raise ValueError("operator note cannot be empty")
        note_path = self._operator_note_path(job)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(note_text + "\n", encoding="utf-8")
        memory_id = self.store.create_memory(
            job_id=job.id,
            kind="operator_note",
            summary=self._memory_summary(note_text),
            detail=note_text,
            tags=["operator", "manual"],
            cluster_key="operator-note",
        )
        result: dict[str, object] = {
            "memory_id": memory_id,
            "note_path": str(note_path),
            "task_id": None,
            "run_id": None,
        }
        if dispatch or run_now:
            task_id = self.store.create_task(
                goal_id=None,
                job_id=job.id,
                kind="operator_note",
                trigger="operator:note",
                status="ready",
                priority=10,
                title=f"Operator note: {self._memory_summary(note_text)}",
                source_ref="operator",
                payload_path=note_path,
                metadata={"note_path": str(note_path), "memory_id": memory_id},
            )
            result["task_id"] = task_id
            if run_now:
                result["run_id"] = self.run_task(task_id)
        return result

    def can_schedule_job(self, job_id: str) -> tuple[bool, str | None]:
        job = self._job(job_id)
        state = GuardianState.from_mapping(dict(self.store.get_job_state(job.id)))
        policy = self._guardian_policy(job)
        decision = can_schedule(policy, state, local_date=self._local_date_string())
        if not decision.allowed:
            return decision.allowed, decision.reason
        if job.goal_loop:
            return self._goal_schedule_gate(job)
        return decision.allowed, decision.reason

    def _with_job_lease(
        self,
        job: JobConfig,
        callback: Callable[[Callable[[], None]], int],
    ) -> int:
        lease_owner = str(uuid.uuid4())
        lease_key = f"job:{job.id}"
        if not self.store.acquire_lease(lease_key, lease_owner, self.config.app.lease_ttl_seconds):
            raise RuntimeError(f"job {job.id} is already leased by another worker")

        def lease_heartbeat() -> None:
            if not self.store.renew_lease(
                lease_key,
                lease_owner,
                self.config.app.lease_ttl_seconds,
            ):
                raise RuntimeError(f"lost lease for job {job.id}")

        try:
            return callback(lease_heartbeat)
        finally:
            self.store.release_lease(lease_key, lease_owner)

    def _create_dispatch_task(
        self,
        job: JobConfig,
        *,
        trigger: str,
        task_path: Path | None,
        task_source: str | None,
    ) -> int:
        priority = self._task_priority(job, trigger=trigger, task_path=task_path)
        dedupe_key = self._task_dedupe_key(job, trigger=trigger, task_path=task_path, task_source=task_source)
        goal_id = self._goal_for_dispatch(job, trigger=trigger)
        if dedupe_key:
            existing = self.store.find_active_task_by_dedupe(job.id, dedupe_key)
            if existing is not None:
                return int(existing["id"])
        return self.store.create_task(
            goal_id=goal_id,
            job_id=job.id,
            kind=self._task_kind(job, trigger=trigger, task_path=task_path),
            trigger=trigger,
            status="ready",
            priority=priority,
            dedupe_key=dedupe_key,
            title=self._task_title(job, trigger=trigger, task_path=task_path),
            source_ref=task_source,
            payload_path=task_path,
            metadata=self._task_metadata(job, trigger=trigger, task_path=task_path),
        )

    def _run_dispatched_task_locked(
        self,
        job: JobConfig,
        task_row: dict[str, object],
        *,
        lease_heartbeat: Callable[[], None] | None = None,
    ) -> int:
        trigger = str(task_row["trigger"])
        task_id = int(task_row["id"])
        goal_id = int(task_row["goal_id"]) if task_row.get("goal_id") else None
        task_path = Path(str(task_row["payload_path"])) if task_row.get("payload_path") else None
        if job.mode == "plan_then_execute" and task_path is not None:
            raise RuntimeError("plan_then_execute jobs do not support inbox execution")

        task_prompt = None
        if task_path is not None:
            task_prompt = _inbox_prompt(
                job,
                task_name=task_path.name,
                task_body=task_path.read_text(encoding="utf-8"),
            )
        self.store.update_task_status(task_id, status="queued")
        if goal_id is not None:
            goal_row = dict(self.store.get_goal(goal_id))
            self.store.update_goal(
                goal_id,
                status="active",
                last_task_id=task_id,
                wake_count=int(goal_row["wake_count"]) + 1,
                next_wake_at=None,
            )

        try:
            if job.mode == "plan_then_execute":
                return self._run_plan(
                    job,
                    trigger=trigger,
                    task_id=task_id,
                    lease_heartbeat=lease_heartbeat,
                )

            resume_row = self._latest_resumable_row(job) if job.persistent_session else None
            prompt = task_prompt or (
            _persistent_wake_prompt(job) if resume_row else job.prompt
            )
            if goal_id is not None:
                prompt = _goal_protocol_prompt(prompt)
            if job.agent_roles:
                return self._run_multi_agent_execution(
                    job,
                    task_id=task_id,
                    trigger=trigger,
                    parent_run_id=(int(resume_row["id"]) if resume_row else None),
                    prompt=prompt,
                    resume_from_row=resume_row,
                    lease_heartbeat=lease_heartbeat,
                )
            return self._run_execution(
                job,
                task_id=task_id,
                trigger=trigger,
                parent_run_id=(int(resume_row["id"]) if resume_row else None),
                prompt=prompt,
                resume_from_row=resume_row,
                lease_heartbeat=lease_heartbeat,
            )
        except Exception as exc:
            self.store.update_task_status(
                task_id,
                status="failed",
                error_message=str(exc),
                finished=True,
            )
            raise

    def approve_run(self, run_id: int) -> int:
        row = dict(self.store.get_run(run_id))
        if row["status"] != "awaiting_approval":
            raise RuntimeError(f"run {run_id} is not awaiting approval")
        job = self._job(str(row["job_id"]))
        plan_path = Path(row["final_message_path"])
        plan_text = plan_path.read_text(encoding="utf-8")
        self.store.update_status(run_id, "approved")
        task_id = int(row["task_id"]) if row.get("task_id") else self.store.create_task(
            job_id=job.id,
            kind="approved_plan",
            trigger=f"approval:{run_id}",
            status="queued",
            title=f"Approved plan for {job.id}",
            metadata={"source_run_id": run_id},
        )
        prompt = _approved_execution_prompt(job, plan_text)
        execute_run_id = self._with_job_lease(
            job,
            lambda heartbeat: (
                self._run_multi_agent_execution(
                    job,
                    task_id=task_id,
                    trigger=f"approval:{run_id}",
                    parent_run_id=run_id,
                    prompt=prompt,
                    resume_from_row=row,
                    lease_heartbeat=heartbeat,
                )
                if job.agent_roles
                else self._run_execution(
                    job,
                    task_id=task_id,
                    trigger=f"approval:{run_id}",
                    parent_run_id=run_id,
                    prompt=prompt,
                    resume_from_row=row,
                    lease_heartbeat=heartbeat,
                )
            ),
        )
        self._record_job_state(job, execute_run_id)
        return execute_run_id

    def resume_run(self, run_id: int, prompt: str) -> int:
        row = dict(self.store.get_run(run_id))
        job = self._job(str(row["job_id"]))
        task_id = int(row["task_id"]) if row.get("task_id") else self.store.create_task(
            job_id=job.id,
            kind="resumed_run",
            trigger=f"resume:{run_id}",
            status="queued",
            title=f"Resume run {run_id}",
            metadata={"source_run_id": run_id},
        )
        resumed_run_id = self._with_job_lease(
            job,
            lambda heartbeat: (
                self._run_multi_agent_execution(
                    job,
                    task_id=task_id,
                    trigger=f"resume:{run_id}",
                    parent_run_id=run_id,
                    prompt=prompt,
                    resume_from_row=row,
                    lease_heartbeat=heartbeat,
                )
                if job.agent_roles
                else self._run_execution(
                    job,
                    task_id=task_id,
                    trigger=f"resume:{run_id}",
                    parent_run_id=run_id,
                    prompt=prompt,
                    resume_from_row=row,
                    lease_heartbeat=heartbeat,
                )
            ),
        )
        self._record_job_state(job, resumed_run_id)
        return resumed_run_id

    def history(self, limit: int = 20) -> list[dict[str, object]]:
        return [dict(row) for row in self.store.list_runs(limit=limit)]

    def show_run(self, run_id: int) -> dict[str, object]:
        return dict(self.store.get_run(run_id))

    def tasks(self, limit: int = 20, *, job_id: str | None = None) -> list[dict[str, object]]:
        return [dict(row) for row in self.store.list_tasks(limit=limit, job_id=job_id)]

    def show_task(self, task_id: int) -> dict[str, object]:
        return dict(self.store.get_task(task_id))

    def memories(self, limit: int = 20, *, job_id: str | None = None) -> list[dict[str, object]]:
        return [dict(row) for row in self.store.list_memories(limit=limit, job_id=job_id)]

    def show_memory(self, memory_id: int) -> dict[str, object]:
        return dict(self.store.get_memory(memory_id))

    def search_memories(self, job_id: str, query: str, *, limit: int = 5) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in self.store.search_memories(job_id=job_id, query=query, limit=limit)
        ]

    def _run_plan(
        self,
        job: JobConfig,
        *,
        trigger: str,
        task_id: int,
        lease_heartbeat: Callable[[], None] | None,
    ) -> int:
        artifact_dir = self._artifact_dir(None)
        run_id = self.store.create_run(
            task_id=task_id,
            job_id=job.id,
            phase="plan",
            trigger=trigger,
            status="queued",
            config_path=self.config.path,
            repo_path=job.repo,
            artifact_dir=artifact_dir,
            job_snapshot=job.to_dict(),
        )
        artifact_dir = self._artifact_dir(run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.bind_artifact_dir(run_id, artifact_dir)
        (artifact_dir / "job.json").write_text(
            json.dumps(job.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        workspace = self.worktrees.prepare(job, run_id)
        self.store.mark_running(
            run_id,
            workspace_path=workspace.workdir,
            branch_name=workspace.branch_name,
            base_head=workspace.base_head,
        )
        self.store.update_task_status(
            task_id,
            status="running",
            last_run_id=run_id,
            started=True,
        )
        preserve = True
        try:
            prompt = self._augment_prompt_with_memory(
                job,
                _planning_prompt(job),
                task_id=task_id,
            )
            result = self.runner.run(
                job,
                prompt=prompt,
                cwd=workspace.workdir,
                artifact_dir=artifact_dir,
                planning_mode=True,
                heartbeat=lease_heartbeat,
            )
            if result.returncode != 0:
                self.store.mark_completed(
                    run_id,
                    status="failed",
                    final_message_path=result.final_message_path,
                    events_path=result.events_path,
                    stderr_path=result.stderr_path,
                    error_message="planning command failed",
                )
                self.store.update_task_status(
                    task_id,
                    status="failed",
                    last_run_id=run_id,
                    error_message="planning command failed",
                    finished=True,
                )
                return run_id
            self.store.bind_codex_thread_id(run_id, result.thread_id)
            self.store.mark_completed(
                run_id,
                status="awaiting_approval",
                final_message_path=result.final_message_path,
                events_path=result.events_path,
                stderr_path=result.stderr_path,
            )
            self.store.update_task_status(
                task_id,
                status="awaiting_approval",
                last_run_id=run_id,
            )
            return run_id
        finally:
            self.worktrees.finalize(job, workspace, preserve=preserve)

    def _run_multi_agent_execution(
        self,
        job: JobConfig,
        *,
        task_id: int,
        trigger: str,
        parent_run_id: int | None,
        prompt: str,
        resume_from_row: dict[str, object] | None,
        lease_heartbeat: Callable[[], None] | None,
    ) -> int:
        artifact_dir = self._artifact_dir(None)
        run_id = self.store.create_run(
            task_id=task_id,
            job_id=job.id,
            phase="execute",
            trigger=trigger,
            status="queued",
            config_path=self.config.path,
            repo_path=job.repo,
            artifact_dir=artifact_dir,
            job_snapshot=job.to_dict(),
            parent_run_id=parent_run_id,
        )
        artifact_dir = self._artifact_dir(run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.bind_artifact_dir(run_id, artifact_dir)
        (artifact_dir / "job.json").write_text(
            json.dumps(job.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        workspace = self._prepare_execution_workspace(job, run_id, resume_from_row)
        self.store.mark_running(
            run_id,
            workspace_path=workspace.workdir,
            branch_name=workspace.branch_name,
            base_head=workspace.base_head,
        )
        self.store.update_task_status(
            task_id,
            status="running",
            last_run_id=run_id,
            started=True,
            metadata={"agent_roles": list(job.agent_roles), "mode": "multi_agent"},
        )

        preserve = bool(job.persistent_session)
        executor_session_id = (
            str(resume_from_row["codex_thread_id"])
            if resume_from_row and resume_from_row["codex_thread_id"]
            else None
        )
        current_feedback: str | None = None
        review_round = 0
        verifier_attempt = 0
        sequence = 0
        plan_text: str | None = None
        role_records: list[dict[str, object]] = []
        pipeline_manifest = artifact_dir / "pipeline.jsonl"

        try:
            if "planner" in job.agent_roles:
                planner = self._run_agent_role(
                    job,
                    parent_task_id=task_id,
                    pipeline_run_id=run_id,
                    role="planner",
                    role_label=None,
                    sequence=sequence,
                    prompt=_role_planner_prompt(job, prompt),
                    workspace=workspace.workdir,
                    artifact_dir=artifact_dir,
                    planning_mode=True,
                    session_id=None,
                    lease_heartbeat=lease_heartbeat,
                )
                sequence += 1
                plan_text = planner["message_text"]
                role_records.append(planner)
                self._append_pipeline_record(pipeline_manifest, planner)

            while True:
                use_parallel_executors = (
                    job.parallel_executor_count > 1
                    and current_feedback is None
                    and review_round == 0
                    and verifier_attempt == 0
                    and not executor_session_id
                    and not job.persistent_session
                )
                if use_parallel_executors:
                    parallel_records, selector, executor, sequence = (
                        self._run_parallel_executor_candidates(
                            job,
                            parent_task_id=task_id,
                            pipeline_run_id=run_id,
                            sequence=sequence,
                            prompt=prompt,
                            plan_text=plan_text,
                            workspace=workspace,
                            artifact_dir=artifact_dir,
                            lease_heartbeat=lease_heartbeat,
                        )
                    )
                    role_records.extend(parallel_records)
                    for record in parallel_records:
                        self._append_pipeline_record(pipeline_manifest, record)
                    if selector is not None:
                        role_records.append(selector)
                        self._append_pipeline_record(pipeline_manifest, selector)
                    if executor is None:
                        summary_path = self._write_multi_agent_summary(
                            artifact_dir,
                            prompt=prompt,
                            role_records=role_records,
                            final_status="failed",
                        )
                        self.store.mark_completed(
                            run_id,
                            status="failed",
                            final_message_path=summary_path,
                            events_path=pipeline_manifest,
                            stderr_path=None,
                            error_message="all parallel executor branches failed",
                        )
                        self.store.update_task_status(
                            task_id,
                            status="failed",
                            last_run_id=run_id,
                            error_message="all parallel executor branches failed",
                            finished=True,
                        )
                        self.worktrees.finalize(job, workspace, preserve=True)
                        return run_id
                else:
                    executor = self._run_agent_role(
                        job,
                        parent_task_id=task_id,
                        pipeline_run_id=run_id,
                        role="executor",
                        role_label=None,
                        sequence=sequence,
                        prompt=_role_executor_prompt(
                            job,
                            base_prompt=prompt,
                            plan_text=plan_text,
                            review_feedback=current_feedback,
                        ),
                        workspace=workspace.workdir,
                        artifact_dir=artifact_dir,
                        planning_mode=False,
                        session_id=executor_session_id,
                        lease_heartbeat=lease_heartbeat,
                    )
                    sequence += 1
                    role_records.append(executor)
                    self._append_pipeline_record(pipeline_manifest, executor)
                    executor_session_id = str(executor["thread_id"] or executor_session_id or "")
                    if executor_session_id:
                        self.store.bind_codex_thread_id(run_id, executor_session_id)

                if "reviewer" in job.agent_roles:
                    reviewer = self._run_agent_role(
                        job,
                        parent_task_id=task_id,
                        pipeline_run_id=run_id,
                        role="reviewer",
                        role_label=None,
                        sequence=sequence,
                        prompt=_role_reviewer_prompt(
                            job,
                            base_prompt=prompt,
                            executor_summary=str(executor["message_text"]),
                            plan_text=plan_text,
                        ),
                        workspace=workspace.workdir,
                        artifact_dir=artifact_dir,
                        planning_mode=True,
                        session_id=None,
                        lease_heartbeat=lease_heartbeat,
                    )
                    sequence += 1
                    role_records.append(reviewer)
                    self._append_pipeline_record(pipeline_manifest, reviewer)
                    review_decision = self._parse_review_decision(str(reviewer["message_text"]))
                    if review_decision == "revise":
                        if review_round >= job.reviewer_max_rounds:
                            summary_path = self._write_multi_agent_summary(
                                artifact_dir,
                                prompt=prompt,
                                role_records=role_records,
                                final_status="failed",
                            )
                            self.store.mark_completed(
                                run_id,
                                status="failed",
                                final_message_path=summary_path,
                                events_path=pipeline_manifest,
                                stderr_path=None,
                                error_message="reviewer requested more revisions than allowed",
                            )
                            self.store.update_task_status(
                                task_id,
                                status="failed",
                                last_run_id=run_id,
                                error_message="reviewer requested more revisions than allowed",
                                finished=True,
                            )
                            self.worktrees.finalize(job, workspace, preserve=True)
                            return run_id
                        review_round += 1
                        current_feedback = str(reviewer["message_text"])
                        continue

                verify_dir = artifact_dir / f"verify-{verifier_attempt:02d}"
                verify_dir.mkdir(parents=True, exist_ok=True)
                verification = self.verifier.run(
                    job.verifier,
                    cwd=workspace.workdir,
                    artifact_dir=verify_dir,
                )
                self._append_attempt_record(
                    artifact_dir / "attempts.jsonl",
                    attempt_number=verifier_attempt,
                    phase="verified" if verification.success else "verification_failed",
                    session_id=executor_session_id or None,
                    result=self._pipeline_result(executor),
                    verification=verification,
                )
                if verification.success:
                    summary_path = self._write_multi_agent_summary(
                        artifact_dir,
                        prompt=prompt,
                        role_records=role_records,
                        final_status="completed",
                    )
                    self.store.mark_completed(
                        run_id,
                        status="completed",
                        final_message_path=summary_path,
                        events_path=pipeline_manifest,
                        stderr_path=None,
                        verifier_log_path=verification.log_path,
                    )
                    self.store.update_task_status(
                        task_id,
                        status="completed",
                        last_run_id=run_id,
                        finished=True,
                    )
                    self.worktrees.finalize(job, workspace, preserve=preserve)
                    return run_id

                if verifier_attempt >= job.verifier_max_attempts:
                    summary_path = self._write_multi_agent_summary(
                        artifact_dir,
                        prompt=prompt,
                        role_records=role_records,
                        final_status="verification_failed",
                    )
                    self.store.mark_completed(
                        run_id,
                        status="verification_failed",
                        final_message_path=summary_path,
                        events_path=pipeline_manifest,
                        stderr_path=None,
                        verifier_log_path=verification.log_path,
                        verifier_failed_command=verification.failed_command,
                        error_message="verifier attempts exhausted",
                    )
                    self.store.update_task_status(
                        task_id,
                        status="verification_failed",
                        last_run_id=run_id,
                        error_message="verifier attempts exhausted",
                        finished=True,
                    )
                    self.worktrees.finalize(job, workspace, preserve=True)
                    return run_id

                verifier_attempt += 1
                current_feedback = _verifier_retry_prompt(
                    job,
                    failed_command=verification.failed_command,
                    verifier_log_path=verification.log_path,
                    attempt_number=verifier_attempt,
                    max_attempts=job.verifier_max_attempts,
                )
        finally:
            if job.persistent_session and executor_session_id:
                self.store.bind_codex_thread_id(run_id, executor_session_id)

    def _run_agent_role(
        self,
        job: JobConfig,
        *,
        parent_task_id: int,
        pipeline_run_id: int,
        role: str,
        role_label: str | None,
        sequence: int,
        prompt: str,
        workspace: Path,
        artifact_dir: Path,
        planning_mode: bool,
        session_id: str | None,
        lease_heartbeat: Callable[[], None] | None,
        raise_on_failure: bool = True,
    ) -> dict[str, object]:
        parent_task = dict(self.store.get_task(parent_task_id))
        metadata = {"role": role, "sequence": sequence}
        if role_label:
            metadata["role_label"] = role_label
        task_title = f"{role} step {sequence}"
        if role_label:
            task_title = f"{role} {role_label} step {sequence}"
        role_task_id = self.store.create_task(
            goal_id=(int(parent_task["goal_id"]) if parent_task.get("goal_id") else None),
            job_id=job.id,
            kind=f"agent_{role}",
            trigger=f"agent:{role}",
            status="queued",
            title=task_title,
            parent_task_id=parent_task_id,
            metadata=metadata,
        )
        artifact_name = f"{sequence:02d}-{role}"
        if role_label:
            artifact_name = f"{artifact_name}-{role_label.replace(' ', '-').lower()}"
        role_artifact_dir = artifact_dir / artifact_name
        role_artifact_dir.mkdir(parents=True, exist_ok=True)
        role_run_id = self.store.create_run(
            task_id=role_task_id,
            job_id=job.id,
            phase=f"agent:{role}",
            trigger=f"agent:{role}",
            status="queued",
            config_path=self.config.path,
            repo_path=job.repo,
            artifact_dir=role_artifact_dir,
            job_snapshot=job.to_dict(),
            parent_run_id=pipeline_run_id,
        )
        self.store.mark_running(
            role_run_id,
            workspace_path=workspace,
            branch_name=None,
            base_head=None,
        )
        self.store.update_task_status(
            role_task_id,
            status="running",
            last_run_id=role_run_id,
            started=True,
        )
        role_prompt = self._augment_prompt_with_memory(job, prompt, task_id=role_task_id)
        if session_id:
            result = self.runner.resume(
                job,
                session_id=session_id,
                prompt=role_prompt,
                cwd=workspace,
                artifact_dir=role_artifact_dir,
                heartbeat=lease_heartbeat,
            )
        else:
            result = self.runner.run(
                job,
                prompt=role_prompt,
                cwd=workspace,
                artifact_dir=role_artifact_dir,
                planning_mode=planning_mode,
                heartbeat=lease_heartbeat,
            )
        self.store.bind_codex_thread_id(role_run_id, result.thread_id)
        role_status = "completed" if result.returncode == 0 else "failed"
        error_message = None if result.returncode == 0 else f"{role} agent command failed"
        self.store.mark_completed(
            role_run_id,
            status=role_status,
            final_message_path=result.final_message_path,
            events_path=result.events_path,
            stderr_path=result.stderr_path,
            error_message=error_message,
        )
        self.store.update_task_status(
            role_task_id,
            status=role_status,
            last_run_id=role_run_id,
            error_message=error_message,
            finished=True,
        )
        role_row = dict(self.store.get_run(role_run_id))
        self._capture_run_memory(job, role_row)
        record = {
            "role": role,
            "role_label": role_label,
            "branch_label": role_label,
            "run_id": role_run_id,
            "task_id": role_task_id,
            "thread_id": result.thread_id,
            "message_text": result.final_message_path.read_text(encoding="utf-8").strip(),
            "events_path": str(result.events_path),
            "stderr_path": str(result.stderr_path),
            "final_message_path": str(result.final_message_path),
            "returncode": result.returncode,
            "status": role_status,
            "error_message": error_message,
        }
        if result.returncode != 0 and raise_on_failure:
            raise RuntimeError(f"{role} agent command failed")
        return record

    def _run_parallel_executor_candidates(
        self,
        job: JobConfig,
        *,
        parent_task_id: int,
        pipeline_run_id: int,
        sequence: int,
        prompt: str,
        plan_text: str | None,
        workspace: WorkspaceLease,
        artifact_dir: Path,
        lease_heartbeat: Callable[[], None] | None,
    ) -> tuple[list[dict[str, object]], dict[str, object] | None, dict[str, object] | None, int]:
        branch_specs = _parallel_executor_specs(job.parallel_executor_count)
        branch_workspaces: list[WorkspaceLease] = []
        completed_records: dict[str, tuple[dict[str, object], WorkspaceLease]] = {}
        try:
            with ThreadPoolExecutor(max_workers=len(branch_specs)) as executor_pool:
                future_map = {}
                for offset, (branch_label, branch_strategy) in enumerate(branch_specs):
                    branch_workspace = self.worktrees.fork_workspace(
                        workspace,
                        job_id=job.id,
                        label=branch_label,
                        run_id=pipeline_run_id,
                    )
                    branch_workspaces.append(branch_workspace)
                    future = executor_pool.submit(
                        self._run_agent_role,
                        job,
                        parent_task_id=parent_task_id,
                        pipeline_run_id=pipeline_run_id,
                        role="executor",
                        role_label=branch_label,
                        sequence=sequence + offset,
                        prompt=_parallel_executor_prompt(
                            job,
                            base_prompt=prompt,
                            plan_text=plan_text,
                            branch_label=branch_label,
                            branch_strategy=branch_strategy,
                        ),
                        workspace=branch_workspace.workdir,
                        artifact_dir=artifact_dir,
                        planning_mode=False,
                        session_id=None,
                        lease_heartbeat=lease_heartbeat,
                        raise_on_failure=False,
                    )
                    future_map[future] = (branch_label, branch_workspace)

                for future in as_completed(future_map):
                    branch_label, branch_workspace = future_map[future]
                    completed_records[branch_label] = (future.result(), branch_workspace)

            ordered_entries = [
                completed_records[branch_label]
                for branch_label, _strategy in branch_specs
                if branch_label in completed_records
            ]
            candidate_records = [record for record, _workspace in ordered_entries]
            successful_entries = [
                (record, branch_workspace)
                for record, branch_workspace in ordered_entries
                if record["status"] == "completed"
            ]
            next_sequence = sequence + len(branch_specs)
            if not successful_entries:
                return candidate_records, None, None, next_sequence

            selector_record: dict[str, object] | None = None
            selected_record, selected_workspace = successful_entries[0]
            if len(successful_entries) > 1 and "reviewer" in job.agent_roles:
                selector_record = self._run_agent_role(
                    job,
                    parent_task_id=parent_task_id,
                    pipeline_run_id=pipeline_run_id,
                    role="reviewer",
                    role_label="branch-select",
                    sequence=next_sequence,
                    prompt=_reviewer_branch_selection_prompt(
                        base_prompt=prompt,
                        plan_text=plan_text,
                        candidates=[record for record, _workspace in successful_entries],
                    ),
                    workspace=workspace.workdir,
                    artifact_dir=artifact_dir,
                    planning_mode=True,
                    session_id=None,
                    lease_heartbeat=lease_heartbeat,
                )
                next_sequence += 1
                choice = self._parse_branch_choice(
                    str(selector_record["message_text"]),
                    [
                        str(record["role_label"])
                        for record, _workspace in successful_entries
                        if record.get("role_label")
                    ],
                )
                selector_record["selected_branch"] = choice
                for record, branch_workspace in successful_entries:
                    if record.get("role_label") == choice:
                        selected_record = record
                        selected_workspace = branch_workspace
                        break

            selected_record["selected_branch"] = str(selected_record.get("role_label") or "")
            self.worktrees.merge_workspace(selected_workspace, workspace)
            return candidate_records, selector_record, selected_record, next_sequence
        finally:
            for branch_workspace in branch_workspaces:
                self.worktrees.cleanup_branch_workspace(branch_workspace)

    def _run_execution(
        self,
        job: JobConfig,
        *,
        task_id: int,
        trigger: str,
        parent_run_id: int | None,
        prompt: str,
        resume_from_row: dict[str, object] | None,
        lease_heartbeat: Callable[[], None] | None,
    ) -> int:
        artifact_dir = self._artifact_dir(None)
        run_id = self.store.create_run(
            task_id=task_id,
            job_id=job.id,
            phase="execute",
            trigger=trigger,
            status="queued",
            config_path=self.config.path,
            repo_path=job.repo,
            artifact_dir=artifact_dir,
            job_snapshot=job.to_dict(),
            parent_run_id=parent_run_id,
        )
        artifact_dir = self._artifact_dir(run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.bind_artifact_dir(run_id, artifact_dir)
        (artifact_dir / "job.json").write_text(
            json.dumps(job.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        workspace = self._prepare_execution_workspace(job, run_id, resume_from_row)
        self.store.mark_running(
            run_id,
            workspace_path=workspace.workdir,
            branch_name=workspace.branch_name,
            base_head=workspace.base_head,
        )
        self.store.update_task_status(
            task_id,
            status="running",
            last_run_id=run_id,
            started=True,
        )

        preserve = False
        if job.persistent_session:
            preserve = True
        attempts_manifest = artifact_dir / "attempts.jsonl"
        session_id = (
            str(resume_from_row["codex_thread_id"])
            if resume_from_row and resume_from_row["codex_thread_id"]
            else None
        )
        current_prompt = prompt
        attempt_number = 0

        while True:
            attempt_dir = artifact_dir / f"attempt-{attempt_number:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            attempt_prompt = current_prompt
            if attempt_number == 0:
                attempt_prompt = self._augment_prompt_with_memory(
                    job,
                    current_prompt,
                    task_id=task_id,
                )
            if session_id:
                result = self.runner.resume(
                    job,
                    session_id=session_id,
                    prompt=attempt_prompt,
                    cwd=workspace.workdir,
                    artifact_dir=attempt_dir,
                    heartbeat=lease_heartbeat,
                )
            else:
                result = self.runner.run(
                    job,
                    prompt=attempt_prompt,
                    cwd=workspace.workdir,
                    artifact_dir=attempt_dir,
                    heartbeat=lease_heartbeat,
                )

            session_id = result.thread_id or session_id
            self.store.bind_codex_thread_id(run_id, session_id)

            if result.returncode != 0:
                self._append_attempt_record(
                    attempts_manifest,
                    attempt_number=attempt_number,
                    phase="codex_failed",
                    session_id=session_id,
                    result=result,
                    verification=None,
                )
                self.store.mark_completed(
                    run_id,
                    status="failed",
                    final_message_path=result.final_message_path,
                    events_path=result.events_path,
                    stderr_path=result.stderr_path,
                    error_message="codex execution failed",
                )
                self.store.update_task_status(
                    task_id,
                    status="failed",
                    last_run_id=run_id,
                    error_message="codex execution failed",
                    finished=True,
                )
                self.worktrees.finalize(job, workspace, preserve=True)
                return run_id

            verification = self.verifier.run(
                job.verifier,
                cwd=workspace.workdir,
                artifact_dir=attempt_dir,
            )
            self._append_attempt_record(
                attempts_manifest,
                attempt_number=attempt_number,
                phase="verified" if verification.success else "verification_failed",
                session_id=session_id,
                result=result,
                verification=verification,
            )

            if verification.success:
                self.store.mark_completed(
                    run_id,
                    status="completed",
                    final_message_path=result.final_message_path,
                    events_path=result.events_path,
                    stderr_path=result.stderr_path,
                    verifier_log_path=verification.log_path,
                )
                self.store.update_task_status(
                    task_id,
                    status="completed",
                    last_run_id=run_id,
                    finished=True,
                )
                self.worktrees.finalize(job, workspace, preserve=preserve)
                return run_id

            if attempt_number >= job.verifier_max_attempts or not session_id:
                self.store.mark_completed(
                    run_id,
                    status="verification_failed",
                    final_message_path=result.final_message_path,
                    events_path=result.events_path,
                    stderr_path=result.stderr_path,
                    verifier_log_path=verification.log_path,
                    verifier_failed_command=verification.failed_command,
                    error_message="verifier attempts exhausted",
                )
                self.store.update_task_status(
                    task_id,
                    status="verification_failed",
                    last_run_id=run_id,
                    error_message="verifier attempts exhausted",
                    finished=True,
                )
                self.worktrees.finalize(job, workspace, preserve=True)
                return run_id

            attempt_number += 1
            current_prompt = _verifier_retry_prompt(
                job,
                failed_command=verification.failed_command,
                verifier_log_path=verification.log_path,
                attempt_number=attempt_number,
                max_attempts=job.verifier_max_attempts,
            )

    def _artifact_dir(self, run_id: int | None) -> Path:
        if run_id is None:
            return self.config.app.state_dir / "runs" / "pending"
        return self.config.app.state_dir / "runs" / f"{run_id:06d}"

    def _job(self, job_id: str) -> JobConfig:
        try:
            return self.config.jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"job {job_id!r} not found in {self.config.path}") from exc

    def _prepare_execution_workspace(
        self,
        job: JobConfig,
        run_id: int,
        resume_from_row: dict[str, object] | None,
    ):
        if resume_from_row is None:
            return self.worktrees.prepare(job, run_id)
        workspace_path = Path(str(resume_from_row["workspace_path"] or job.repo))
        if not workspace_path.exists():
            raise RuntimeError(
                f"workspace for run {resume_from_row['id']} is missing: {workspace_path}"
            )
        return self._workspace_from_row(resume_from_row, workspace_path)

    def _workspace_from_row(self, row: dict[str, object], workspace_path: Path):
        using_worktree = bool(row["branch_name"])
        return self.worktrees.restore(
            repo_root=Path(str(row["repo_path"])),
            workdir=workspace_path,
            using_worktree=using_worktree,
            branch_name=(str(row["branch_name"]) if row["branch_name"] else None),
            base_head=(str(row["base_head"]) if row["base_head"] else None),
        )

    def _append_attempt_record(
        self,
        manifest_path: Path,
        *,
        attempt_number: int,
        phase: str,
        session_id: str | None,
        result,
        verification,
    ) -> None:
        record = {
            "attempt_number": attempt_number,
            "phase": phase,
            "session_id": session_id,
            "returncode": result.returncode,
            "events_path": str(result.events_path),
            "stderr_path": str(result.stderr_path),
            "final_message_path": str(result.final_message_path),
            "verifier_log_path": str(verification.log_path) if verification else None,
            "verifier_success": verification.success if verification else None,
            "verifier_failed_command": verification.failed_command if verification else None,
        }
        with manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True))
            handle.write("\n")

    def _append_pipeline_record(self, manifest_path: Path, record: dict[str, object]) -> None:
        with manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True))
            handle.write("\n")

    def _write_multi_agent_summary(
        self,
        artifact_dir: Path,
        *,
        prompt: str,
        role_records: list[dict[str, object]],
        final_status: str,
    ) -> Path:
        summary_path = artifact_dir / "final.md"
        lines = [
            f"Multi-agent execution status: {final_status}",
            "",
            "Original task:",
            prompt,
            "",
            "Role outputs:",
        ]
        for record in role_records:
            role_name = str(record["role"])
            if record.get("role_label"):
                role_name = f"{role_name}[{record['role_label']}]"
            lines.extend(
                [
                    (
                        f"- role={role_name} status={record.get('status', 'completed')} "
                        f"run_id={record['run_id']} task_id={record['task_id']}"
                    ),
                    (
                        f"Selected branch: {record['selected_branch']}"
                        if record.get("selected_branch")
                        else ""
                    ),
                    (
                        f"Error: {record['error_message']}"
                        if record.get("error_message")
                        else ""
                    ),
                    str(record["message_text"]),
                    "",
                ]
            )
        summary_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return summary_path

    def _parse_review_decision(self, review_text: str) -> str:
        for line in review_text.splitlines():
            candidate = line.strip().lower()
            if not candidate:
                continue
            if candidate.startswith("review_decision:"):
                decision = candidate.split(":", 1)[1].strip()
                return "revise" if decision.startswith("revise") else "approve"
        return "approve"

    def _parse_branch_choice(
        self,
        review_text: str,
        candidate_labels: list[str],
    ) -> str:
        if not candidate_labels:
            raise RuntimeError("no candidate labels available for branch selection")
        label_map = {label.lower(): label for label in candidate_labels}
        for line in review_text.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if candidate.lower().startswith("branch_choice:"):
                choice = candidate.split(":", 1)[1].strip().lower()
                return label_map.get(choice, candidate_labels[0])
        return candidate_labels[0]

    def _pipeline_result(self, record: dict[str, object]):
        class _Result:
            def __init__(self, payload: dict[str, object]) -> None:
                self.returncode = int(payload["returncode"])
                self.events_path = Path(str(payload["events_path"]))
                self.stderr_path = Path(str(payload["stderr_path"]))
                self.final_message_path = Path(str(payload["final_message_path"]))

        return _Result(record)

    def _record_job_state(self, job: JobConfig, run_id: int) -> None:
        row = self.show_run(run_id)
        state = GuardianState.from_mapping(dict(self.store.get_job_state(job.id)))
        updated = record_run_outcome(
            state,
            status=str(row["status"]),
            local_date=self._local_date_string(),
            run_id=run_id,
        )
        updated = apply_pause_decision(self._guardian_policy(job), updated)
        self.store.write_job_state(job.id, updated.to_mapping())
        if job.persistent_session and row.get("codex_thread_id") and row.get("workspace_path"):
            self.store.upsert_session(
                job.id,
                repo_path=Path(str(row["repo_path"])),
                workspace_path=Path(str(row["workspace_path"])),
                branch_name=(str(row["branch_name"]) if row.get("branch_name") else None),
                base_head=(str(row["base_head"]) if row.get("base_head") else None),
                codex_thread_id=str(row["codex_thread_id"]),
                last_run_id=run_id,
                last_status=str(row["status"]),
            )
        if row.get("task_id"):
            task_row = dict(self.store.get_task(int(row["task_id"])))
            if task_row.get("goal_id"):
                self._record_goal_progress(job, task_row, row)
        self._capture_run_memory(job, row)

    def _latest_resumable_row(self, job: JobConfig) -> dict[str, object] | None:
        session = self.store.get_session(job.id)
        if session is not None:
            payload = dict(session)
            workspace_path = Path(str(payload["workspace_path"]))
            if workspace_path.exists():
                return {
                    "id": payload["last_run_id"],
                    "repo_path": payload["repo_path"],
                    "workspace_path": payload["workspace_path"],
                    "branch_name": payload["branch_name"],
                    "base_head": payload["base_head"],
                    "codex_thread_id": payload["codex_thread_id"],
                }
        row = self.store.get_latest_resumable_run(job.id)
        if row is None:
            return None
        payload = dict(row)
        workspace_path = Path(str(payload["workspace_path"]))
        if not workspace_path.exists():
            return None
        return payload

    def _claim_inbox_item(self, job: JobConfig, source_path: Path) -> Path:
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        staging_dir = self.config.app.state_dir / "inbox" / job.id / "in-progress"
        staging_dir.mkdir(parents=True, exist_ok=True)
        target = staging_dir / source_path.name
        counter = 1
        while target.exists():
            target = staging_dir / f"{source_path.stem}-{counter}{source_path.suffix}"
            counter += 1
        source_path.replace(target)
        return target

    def _archive_inbox_item(self, job: JobConfig, staging_path: Path, *, success: bool) -> Path:
        bucket = "processed" if success else "failed"
        target_dir = self.config.app.state_dir / "inbox" / job.id / bucket
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / staging_path.name
        counter = 1
        while target.exists():
            target = target_dir / f"{staging_path.stem}-{counter}{staging_path.suffix}"
            counter += 1
        if staging_path.exists():
            staging_path.replace(target)
        return target

    def _inbox_backlog(self, job: JobConfig) -> int:
        if not job.inbox_dir:
            return 0
        job.inbox_dir.mkdir(parents=True, exist_ok=True)
        return sum(1 for path in job.inbox_dir.glob(job.inbox_glob) if path.is_file())

    def _operator_note_path(self, job: JobConfig) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        return (
            self.config.app.state_dir
            / "operator-notes"
            / job.id
            / f"{stamp}-{suffix}.md"
        )

    def _local_date_string(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    def _guardian_policy(self, job: JobConfig) -> GuardianPolicy:
        return GuardianPolicy(
            max_daily_wakes=job.max_daily_wakes,
            max_consecutive_failures=job.max_consecutive_failures,
        )

    def _augment_prompt_with_memory(
        self,
        job: JobConfig,
        prompt: str,
        *,
        task_id: int | None,
        limit: int = 4,
    ) -> str:
        query = prompt
        if task_id is not None:
            task = dict(self.store.get_task(task_id))
            query = " ".join(
                part
                for part in (
                    str(task.get("title") or ""),
                    str(task.get("source_ref") or ""),
                    prompt,
                )
                if part.strip()
            )
        memories = self.store.search_memories(job_id=job.id, query=query, limit=limit)
        if not memories:
            return prompt
        lines = [
            "Relevant memory from previous runs. Use it only when it still fits the current repository state.",
        ]
        for row in memories:
            tags = self._parse_tags(row["tags_json"])
            tag_suffix = f" tags={','.join(tags)}" if tags else ""
            lines.append(f"- [{row['kind']}] {row['summary']}{tag_suffix}")
        return f"{prompt}\n\n" + "\n".join(lines)

    def _capture_run_memory(self, job: JobConfig, row: dict[str, object]) -> None:
        run_id = int(row["id"])
        task_id = int(row["task_id"]) if row.get("task_id") else None
        session_id = str(row["codex_thread_id"]) if row.get("codex_thread_id") else None
        status = str(row["status"])
        phase = str(row["phase"])
        trigger = str(row["trigger"])
        tags = [status, phase, trigger]
        final_message_path = (
            Path(str(row["final_message_path"])) if row.get("final_message_path") else None
        )
        if final_message_path and final_message_path.exists():
            final_message = final_message_path.read_text(encoding="utf-8").strip()
            if final_message:
                self.store.create_memory(
                    job_id=job.id,
                    kind="plan" if status == "awaiting_approval" else "run_outcome",
                    summary=self._memory_summary(final_message),
                    detail=self._memory_detail(final_message),
                    tags=tags,
                    source_task_id=task_id,
                    source_run_id=run_id,
                    source_session_id=session_id,
                )

        if status in {"failed", "verification_failed"}:
            verifier_log_path = (
                Path(str(row["verifier_log_path"])) if row.get("verifier_log_path") else None
            )
            failure_bits: list[str] = []
            failed_command = str(row["verifier_failed_command"] or "").strip()
            if failed_command:
                failure_bits.append(f"Failed command: {failed_command}")
            if verifier_log_path and verifier_log_path.exists():
                failure_bits.append(_tail_text(verifier_log_path, limit=3000))
            error_message = str(row["error_message"] or "").strip()
            if error_message:
                failure_bits.append(error_message)
            detail = "\n\n".join(bit for bit in failure_bits if bit)
            self.store.create_memory(
                job_id=job.id,
                kind="failure_pattern",
                summary=self._failure_summary(row),
                detail=detail or None,
                cluster_key=self._failure_cluster_key(row),
                tags=tags + ["failure"],
                source_task_id=task_id,
                source_run_id=run_id,
                source_session_id=session_id,
            )

    def _memory_summary(self, text: str, limit: int = 160) -> str:
        for line in text.splitlines():
            candidate = line.strip().lstrip("#").strip()
            if candidate:
                return candidate[:limit]
        compact = " ".join(text.split())
        return compact[:limit]

    def _memory_detail(self, text: str, limit: int = 4000) -> str:
        return text[:limit]

    def _failure_summary(self, row: dict[str, object]) -> str:
        failed_command = str(row.get("verifier_failed_command") or "").strip()
        if failed_command:
            return f"Verifier failed on `{failed_command}`"
        return f"Run {row['id']} ended with {row['status']}"

    def _failure_cluster_key(self, row: dict[str, object]) -> str:
        failed_command = str(row.get("verifier_failed_command") or "").strip().lower()
        if failed_command:
            return failed_command
        error_message = str(row.get("error_message") or "").strip().lower()
        return error_message or str(row.get("status") or "").strip().lower()

    def _parse_tags(self, raw_tags: object) -> list[str]:
        if raw_tags is None:
            return []
        try:
            tags = json.loads(str(raw_tags))
        except json.JSONDecodeError:
            return []
        return [str(tag) for tag in tags if str(tag).strip()]

    def _goal_for_dispatch(self, job: JobConfig, *, trigger: str) -> int | None:
        if not job.goal_loop:
            return None
        active = self.store.get_active_goal(job.id)
        if active is not None:
            return int(active["id"])
        latest = self.store.get_latest_goal(job.id)
        if latest is not None and trigger == "schedule" and str(latest["status"]) in {"completed", "stopped"}:
            raise RuntimeError(f"goal for job {job.id} is {latest['status']}")
        title = f"Goal for {job.id}"
        return self.store.create_goal(
            job_id=job.id,
            status="active",
            title=title,
            metadata={"prompt": job.prompt},
        )

    def _goal_schedule_gate(self, job: JobConfig) -> tuple[bool, str | None]:
        goal = self.store.get_active_goal(job.id)
        if goal is None:
            latest = self.store.get_latest_goal(job.id)
            if latest is not None and str(latest["status"]) in {"completed", "stopped"}:
                return False, f"goal {latest['status']}"
            return True, None
        status = str(goal["status"])
        if status == "completed":
            return False, "goal completed"
        if status == "stopped":
            return False, "goal stopped"
        next_wake_at = goal["next_wake_at"]
        if next_wake_at:
            wake_time = datetime.fromisoformat(str(next_wake_at))
            if wake_time > datetime.now(timezone.utc):
                return False, "goal backoff active"
        return True, None

    def _record_goal_progress(
        self,
        job: JobConfig,
        task_row: dict[str, object],
        run_row: dict[str, object],
    ) -> None:
        goal_id = int(task_row["goal_id"])
        goal_row = dict(self.store.get_goal(goal_id))
        run_status = str(run_row["status"])
        final_message_path = (
            Path(str(run_row["final_message_path"])) if run_row.get("final_message_path") else None
        )
        final_message = (
            final_message_path.read_text(encoding="utf-8").strip()
            if final_message_path and final_message_path.exists()
            else ""
        )
        outcome = self._parse_goal_status(final_message)
        idle_count = int(goal_row["idle_count"])
        next_wake_at: str | None = None
        goal_status = "active"
        completed = False

        if run_status in {"failed", "verification_failed"}:
            outcome = "blocked"

        if outcome == "done":
            goal_status = "completed"
            completed = True
            idle_count = 0
        elif outcome == "idle":
            idle_count += 1
            if job.goal_max_idle_wakes and idle_count >= job.goal_max_idle_wakes:
                goal_status = "stopped"
                completed = True
            else:
                goal_status = "idle"
                next_wake_at = self._goal_next_wake(job.goal_backoff_seconds)
        elif outcome == "blocked":
            goal_status = "blocked"
            next_wake_at = self._goal_next_wake(job.goal_backoff_seconds)
        else:
            goal_status = "active"
            idle_count = 0

        self.store.update_goal(
            goal_id,
            status=goal_status,
            next_wake_at=next_wake_at,
            idle_count=idle_count,
            last_task_id=int(task_row["id"]),
            last_run_id=int(run_row["id"]),
            last_outcome=outcome,
            source_session_id=(
                str(run_row["codex_thread_id"]) if run_row.get("codex_thread_id") else None
            ),
            metadata={
                "last_trigger": run_row["trigger"],
                "last_status": run_status,
                "goal_outcome": outcome,
            },
            completed=completed,
        )

    def _parse_goal_status(self, final_message: str) -> str:
        for line in final_message.splitlines():
            candidate = line.strip().lower()
            if not candidate:
                continue
            if candidate.startswith("goal_status:"):
                value = candidate.split(":", 1)[1].strip()
                if value.startswith("done"):
                    return "done"
                if value.startswith("blocked"):
                    return "blocked"
                if value.startswith("idle"):
                    return "idle"
                return "continue"
        return "continue"

    def _goal_next_wake(self, backoff_seconds: int) -> str | None:
        if backoff_seconds <= 0:
            return None
        return (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()

    def _task_kind(
        self,
        job: JobConfig,
        *,
        trigger: str,
        task_path: Path | None,
    ) -> str:
        if task_path is not None:
            return "inbox_item"
        if job.mode == "plan_then_execute":
            return "plan_request"
        if trigger == "schedule":
            return "scheduled_wake"
        if trigger == "manual":
            return "manual_run"
        return "job_run"

    def _task_title(
        self,
        job: JobConfig,
        *,
        trigger: str,
        task_path: Path | None,
    ) -> str:
        if task_path is not None:
            return task_path.name
        if trigger == "schedule":
            return f"Scheduled wake for {job.id}"
        if trigger == "manual":
            return f"Manual run for {job.id}"
        return f"{job.id} ({trigger})"

    def _task_metadata(
        self,
        job: JobConfig,
        *,
        trigger: str,
        task_path: Path | None,
    ) -> dict[str, object]:
        priority = self._task_priority(job, trigger=trigger, task_path=task_path)
        return {
            "job_mode": job.mode,
            "persistent_session": job.persistent_session,
            "trigger": trigger,
            "payload_name": task_path.name if task_path else None,
            "priority": priority,
        }

    def _task_priority(
        self,
        job: JobConfig,
        *,
        trigger: str,
        task_path: Path | None,
    ) -> int:
        if trigger == "manual":
            return 10
        if task_path is None:
            return 50
        text = task_path.read_text(encoding="utf-8", errors="replace").lower()
        high = ("urgent", "critical", "security", "sev1", "blocker", "outage", "prod")
        medium = ("fail", "broken", "incident", "bug", "regression", "review")
        if any(token in text for token in high):
            return 5
        if any(token in text for token in medium):
            return 20
        return 40

    def _task_dedupe_key(
        self,
        job: JobConfig,
        *,
        trigger: str,
        task_path: Path | None,
        task_source: str | None,
    ) -> str | None:
        if trigger == "schedule":
            return f"schedule:{job.id}"
        if task_path is not None:
            source = task_source or str(task_path)
            return f"inbox:{job.id}:{source}"
        return None

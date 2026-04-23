from __future__ import annotations

import argparse
import json
from pathlib import Path

from nexus_codex.config import load_config, write_sample_config
from nexus_codex.runtime import Runtime
from nexus_codex.scheduler import JobScheduler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nexus-codex")
    parser.add_argument("--config", default="jobs.toml", help="Path to jobs.toml")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Write a sample jobs.toml")
    init_parser.add_argument("--output", default="jobs.toml", help="Destination config path")

    subparsers.add_parser("list-jobs", help="List configured jobs")

    run_once = subparsers.add_parser("run-once", help="Run one job immediately")
    run_once.add_argument("job_id")

    dispatch_job = subparsers.add_parser("dispatch-job", help="Dispatch one job into the task queue")
    dispatch_job.add_argument("job_id")
    dispatch_job.add_argument("--trigger", default="manual")

    subparsers.add_parser("worker", help="Start the scheduler loop")

    subparsers.add_parser("dashboard", help="Show an operational dashboard across jobs")

    drain_inbox = subparsers.add_parser("drain-inbox", help="Run the next inbox task for a job")
    drain_inbox.add_argument("job_id")

    dispatch_inbox = subparsers.add_parser("dispatch-inbox", help="Dispatch inbox items into queued tasks")
    dispatch_inbox.add_argument("job_id")
    dispatch_inbox.add_argument("--limit", type=int)

    history = subparsers.add_parser("history", help="Show recent runs")
    history.add_argument("--limit", type=int, default=20)

    tasks = subparsers.add_parser("tasks", help="Show recent tasks")
    tasks.add_argument("--limit", type=int, default=20)
    tasks.add_argument("--job-id")

    goals = subparsers.add_parser("goals", help="Show goal supervision state")
    goals.add_argument("--limit", type=int, default=20)
    goals.add_argument("--job-id")

    memories = subparsers.add_parser("memories", help="Show or search memory entries")
    memories.add_argument("--limit", type=int, default=20)
    memories.add_argument("--job-id")
    memories.add_argument("--query")

    job_status = subparsers.add_parser("job-status", help="Show state for one job")
    job_status.add_argument("job_id")

    pause_job = subparsers.add_parser("pause-job", help="Pause a job")
    pause_job.add_argument("job_id")
    pause_job.add_argument("--reason", default="paused by operator")

    resume_job = subparsers.add_parser("resume-job", help="Resume a job")
    resume_job.add_argument("job_id")

    sessions = subparsers.add_parser("sessions", help="Show persistent session state")
    sessions.add_argument("--limit", type=int, default=20)
    sessions.add_argument("--job-id")

    operator_note = subparsers.add_parser("operator-note", help="Attach an operator note to a job")
    operator_note.add_argument("job_id")
    operator_note.add_argument("note")
    operator_note.add_argument("--dispatch", action="store_true")
    operator_note.add_argument("--run-now", action="store_true")

    show_run = subparsers.add_parser("show-run", help="Show one run")
    show_run.add_argument("run_id", type=int)

    run_task = subparsers.add_parser("run-task", help="Execute one queued task")
    run_task.add_argument("task_id", type=int)

    run_next_task = subparsers.add_parser("run-next-task", help="Execute the next queued task for a job")
    run_next_task.add_argument("job_id")

    show_task = subparsers.add_parser("show-task", help="Show one task")
    show_task.add_argument("task_id", type=int)

    show_goal = subparsers.add_parser("show-goal", help="Show one goal")
    show_goal.add_argument("goal_id", type=int)

    show_memory = subparsers.add_parser("show-memory", help="Show one memory entry")
    show_memory.add_argument("memory_id", type=int)

    approve = subparsers.add_parser("approve", help="Approve a plan run and start execution")
    approve.add_argument("run_id", type=int)

    resume = subparsers.add_parser("resume-run", help="Resume a previous run with a follow-up prompt")
    resume.add_argument("run_id", type=int)
    resume.add_argument("prompt")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        target = write_sample_config(args.output)
        print(f"Wrote sample config to {target}")
        return 0

    if args.command == "list-jobs":
        loaded = load_config(args.config)
        for job in loaded.jobs.values():
            inbox = str(job.inbox_dir) if job.inbox_dir else "-"
            print(
                f"{job.id}\tmode={job.mode}\tschedule={job.schedule}\t"
                f"persistent={job.persistent_session}\tinbox={inbox}\tenabled={job.enabled}"
            )
        return 0

    runtime = Runtime(args.config)

    if args.command == "run-once":
        run_id = runtime.execute_job(args.job_id)
        print(run_id)
        return 0

    if args.command == "dispatch-job":
        task_id = runtime.dispatch_job(args.job_id, trigger=args.trigger)
        print(task_id)
        return 0

    if args.command == "worker":
        scheduler = JobScheduler(args.config)
        scheduler.run_forever()
        return 0

    if args.command == "dashboard":
        for row in runtime.dashboard():
            print(
                f"{row['job_id']}\tpaused={int(bool(row['paused']))}\tready={row['ready_tasks']}\t"
                f"queued={row['queued_tasks']}\trunning={row['running_tasks']}\t"
                f"awaiting_approval={row['awaiting_approval_tasks']}\tinbox={row['inbox_backlog']}\t"
                f"last={row.get('last_run_status') or '-'}\t"
                f"session={_short_id(row.get('session_thread_id'))}\t"
                f"goal={row.get('goal_status') or '-'}\t"
                f"blocked={row.get('schedule_block_reason') or '-'}"
            )
        return 0

    if args.command == "drain-inbox":
        next_item = runtime.next_inbox_item(args.job_id)
        if next_item is None:
            print("no inbox items")
            return 0
        run_id = runtime.execute_inbox_task(args.job_id, next_item)
        print(run_id)
        return 0

    if args.command == "dispatch-inbox":
        task_ids = runtime.dispatch_inbox(args.job_id, limit=args.limit)
        for task_id in task_ids:
            print(task_id)
        return 0

    if args.command == "history":
        rows = runtime.history(limit=args.limit)
        for row in rows:
            thread_id = row.get("codex_thread_id") or "-"
            print(
                f"{row['id']}\t{row.get('task_id') or '-'}\t{row['job_id']}\t{row['phase']}\t{row['status']}\t"
                f"{thread_id}\t{row['created_at']}"
            )
        return 0

    if args.command == "tasks":
        rows = runtime.tasks(limit=args.limit, job_id=args.job_id)
        for row in rows:
            print(
                f"{row['id']}\t{row.get('goal_id') or '-'}\t{row['job_id']}\t{row['kind']}\t{row['status']}\t{row.get('priority') or '-'}\t"
                f"{row.get('last_run_id') or '-'}\t{row['created_at']}"
            )
        return 0

    if args.command == "goals":
        rows = runtime.goals(limit=args.limit, job_id=args.job_id)
        for row in rows:
            print(
                f"{row['id']}\t{row['job_id']}\t{row['status']}\t{row.get('wake_count') or 0}\t"
                f"{row.get('last_outcome') or '-'}\t{row.get('next_wake_at') or '-'}\t{row['created_at']}"
            )
        return 0

    if args.command == "memories":
        if args.query:
            if not args.job_id:
                parser.error("--job-id is required when --query is used")
            rows = runtime.search_memories(args.job_id, args.query, limit=args.limit)
        else:
            rows = runtime.memories(limit=args.limit, job_id=args.job_id)
        for row in rows:
            print(
                f"{row['id']}\t{row['job_id']}\t{row['kind']}\t{row.get('occurrence_count') or 1}\t{row['summary']}\t"
                f"{row['created_at']}"
            )
        return 0

    if args.command == "job-status":
        row = runtime.job_status(args.job_id)
        print(json.dumps(_json_safe(row), indent=2, sort_keys=True))
        return 0

    if args.command == "pause-job":
        runtime.pause_job(args.job_id, args.reason)
        print(f"paused {args.job_id}")
        return 0

    if args.command == "resume-job":
        runtime.resume_job_control(args.job_id)
        print(f"resumed {args.job_id}")
        return 0

    if args.command == "sessions":
        rows = runtime.sessions(limit=args.limit, job_id=args.job_id)
        for row in rows:
            print(
                f"{row['job_id']}\t{_short_id(row['codex_thread_id'])}\t{row['last_status']}\t"
                f"{row['last_run_id']}\t{row['updated_at']}\t{row['workspace_path']}"
            )
        return 0

    if args.command == "operator-note":
        result = runtime.add_operator_note(
            args.job_id,
            args.note,
            dispatch=args.dispatch or args.run_now,
            run_now=args.run_now,
        )
        print(json.dumps(_json_safe(result), indent=2, sort_keys=True))
        return 0

    if args.command == "show-run":
        row = runtime.show_run(args.run_id)
        print(json.dumps(_json_safe(row), indent=2, sort_keys=True))
        return 0

    if args.command == "run-task":
        run_id = runtime.run_task(args.task_id)
        print(run_id)
        return 0

    if args.command == "run-next-task":
        run_id = runtime.run_next_task(args.job_id)
        if run_id is None:
            print("no ready tasks")
        else:
            print(run_id)
        return 0

    if args.command == "show-task":
        row = runtime.show_task(args.task_id)
        print(json.dumps(_json_safe(row), indent=2, sort_keys=True))
        return 0

    if args.command == "show-goal":
        row = runtime.show_goal(args.goal_id)
        print(json.dumps(_json_safe(row), indent=2, sort_keys=True))
        return 0

    if args.command == "show-memory":
        row = runtime.show_memory(args.memory_id)
        print(json.dumps(_json_safe(row), indent=2, sort_keys=True))
        return 0

    if args.command == "approve":
        new_run_id = runtime.approve_run(args.run_id)
        print(new_run_id)
        return 0

    if args.command == "resume-run":
        new_run_id = runtime.resume_run(args.run_id, args.prompt)
        print(new_run_id)
        return 0

    parser.error("unknown command")
    return 2


def _json_safe(payload: dict[str, object]) -> dict[str, object]:
    converted: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, Path):
            converted[key] = str(value)
        else:
            converted[key] = value
    return converted


def _short_id(value: object) -> str:
    text = str(value or "-")
    if text == "-":
        return text
    return text if len(text) <= 12 else text[:12]

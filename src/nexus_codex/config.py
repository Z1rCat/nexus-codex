from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from nexus_codex.models import AppConfig, JobConfig, LoadedConfig, WorktreeConfig


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _load_worktree_config(raw: object) -> WorktreeConfig:
    data = raw if isinstance(raw, dict) else {}
    return WorktreeConfig(
        strategy=str(data.get("strategy", "off")),
        cleanup=str(data.get("cleanup", "if-clean")),
    )


def _load_agent_roles(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("agent_roles must be an array")
    roles = tuple(str(value).strip().lower() for value in raw if str(value).strip())
    valid = {"planner", "executor", "reviewer"}
    invalid = sorted({role for role in roles if role not in valid})
    if invalid:
        raise ValueError(f"unsupported agent roles: {', '.join(invalid)}")
    if roles and "executor" not in roles:
        raise ValueError("agent_roles must include executor")
    if "executor" in roles and roles[0] == "reviewer":
        raise ValueError("reviewer cannot be the first agent role")
    return roles


def load_config(path: str | Path) -> LoadedConfig:
    config_path = Path(path).resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    base_dir = config_path.parent
    app_raw = raw.get("app", {})
    if not isinstance(app_raw, dict):
        raise ValueError("[app] must be a table")

    codex_command = app_raw.get("codex_command", ["codex"])
    if isinstance(codex_command, str):
        codex_command = [codex_command]
    if not isinstance(codex_command, list) or not codex_command:
        raise ValueError("app.codex_command must be a non-empty array")

    app = AppConfig(
        timezone=str(app_raw.get("timezone", "UTC")),
        state_dir=_resolve_path(base_dir, str(app_raw.get("state_dir", ".nexus"))),
        poll_interval_seconds=int(app_raw.get("poll_interval_seconds", 15)),
        max_concurrent_runs=int(app_raw.get("max_concurrent_runs", 1)),
        lease_ttl_seconds=int(app_raw.get("lease_ttl_seconds", 1800)),
        codex_command=tuple(str(part) for part in codex_command),
    )

    jobs_raw = raw.get("jobs", [])
    if not isinstance(jobs_raw, list):
        raise ValueError("jobs must be an array of tables")

    jobs: dict[str, JobConfig] = {}
    for item in jobs_raw:
        if not isinstance(item, dict):
            raise ValueError("each [[jobs]] entry must be a table")
        job_id = str(item["id"])
        if job_id in jobs:
            raise ValueError(f"duplicate job id: {job_id}")
        add_dirs = tuple(
            _resolve_path(base_dir, str(path))
            for path in item.get("add_dirs", [])
        )
        verifier = tuple(str(command) for command in item.get("verifier", []))
        overrides = tuple(str(entry) for entry in item.get("config_overrides", []))
        inbox_dir = (
            _resolve_path(base_dir, str(item["inbox_dir"]))
            if "inbox_dir" in item
            else None
        )
        jobs[job_id] = JobConfig(
            id=job_id,
            schedule=str(item["schedule"]),
            repo=_resolve_path(base_dir, str(item["repo"])),
            prompt=str(item["prompt"]).strip(),
            mode=str(item.get("mode", "analysis")),
            enabled=bool(item.get("enabled", True)),
            sandbox=str(item.get("sandbox", "workspace-write")),
            approval=str(item.get("approval", "never")),
            skip_git_repo_check=bool(item.get("skip_git_repo_check", False)),
            search=bool(item.get("search", False)),
            model=(str(item["model"]) if "model" in item else None),
            profile=(str(item["profile"]) if "profile" in item else None),
            add_dirs=add_dirs,
            config_overrides=overrides,
            verifier=verifier,
            verifier_max_attempts=int(item.get("verifier_max_attempts", 0)),
            persistent_session=bool(item.get("persistent_session", False)),
            wake_prompt=(str(item["wake_prompt"]).strip() if "wake_prompt" in item else None),
            inbox_dir=inbox_dir,
            inbox_glob=str(item.get("inbox_glob", "*.md")),
            max_daily_wakes=(
                int(item["max_daily_wakes"]) if "max_daily_wakes" in item else None
            ),
            max_consecutive_failures=int(item.get("max_consecutive_failures", 0)),
            goal_loop=bool(item.get("goal_loop", False)),
            goal_backoff_seconds=int(item.get("goal_backoff_seconds", 0)),
            goal_max_idle_wakes=int(item.get("goal_max_idle_wakes", 0)),
            agent_roles=_load_agent_roles(item.get("agent_roles")),
            parallel_executor_count=max(1, int(item.get("parallel_executor_count", 1))),
            reviewer_max_rounds=int(item.get("reviewer_max_rounds", 0)),
            worktree=_load_worktree_config(item.get("worktree")),
        )

    return LoadedConfig(path=config_path, app=app, jobs=jobs)


def write_sample_config(destination: str | Path) -> Path:
    target = Path(destination).resolve()
    sample = Path(__file__).resolve().parents[2] / "jobs.toml.example"
    shutil.copyfile(sample, target)
    return target

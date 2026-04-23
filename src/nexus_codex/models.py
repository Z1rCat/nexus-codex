from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalMode = Literal["untrusted", "on-request", "never"]
JobMode = Literal["analysis", "execute", "plan_then_execute"]
WorktreeStrategy = Literal["off", "auto"]
CleanupMode = Literal["never", "if-clean"]
AgentRole = Literal["planner", "executor", "reviewer"]
RunStatus = Literal[
    "queued",
    "running",
    "awaiting_approval",
    "approved",
    "completed",
    "failed",
    "verification_failed",
    "skipped",
]


@dataclass(frozen=True)
class WorktreeConfig:
    strategy: WorktreeStrategy = "off"
    cleanup: CleanupMode = "if-clean"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobConfig:
    id: str
    schedule: str
    repo: Path
    prompt: str
    mode: JobMode = "analysis"
    enabled: bool = True
    sandbox: SandboxMode = "workspace-write"
    approval: ApprovalMode = "never"
    skip_git_repo_check: bool = False
    search: bool = False
    model: str | None = None
    profile: str | None = None
    add_dirs: tuple[Path, ...] = ()
    config_overrides: tuple[str, ...] = ()
    verifier: tuple[str, ...] = ()
    verifier_max_attempts: int = 0
    persistent_session: bool = False
    wake_prompt: str | None = None
    inbox_dir: Path | None = None
    inbox_glob: str = "*.md"
    max_daily_wakes: int | None = None
    max_consecutive_failures: int = 0
    goal_loop: bool = False
    goal_backoff_seconds: int = 0
    goal_max_idle_wakes: int = 0
    agent_roles: tuple[AgentRole, ...] = ()
    parallel_executor_count: int = 1
    reviewer_max_rounds: int = 0
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["repo"] = str(self.repo)
        data["add_dirs"] = [str(path) for path in self.add_dirs]
        data["inbox_dir"] = str(self.inbox_dir) if self.inbox_dir else None
        return data


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    state_dir: Path
    poll_interval_seconds: int = 15
    max_concurrent_runs: int = 1
    lease_ttl_seconds: int = 1800
    codex_command: tuple[str, ...] = ("codex",)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state_dir"] = str(self.state_dir)
        return data


@dataclass(frozen=True)
class LoadedConfig:
    path: Path
    app: AppConfig
    jobs: dict[str, JobConfig]


@dataclass(frozen=True)
class WorkspaceLease:
    repo_root: Path
    workdir: Path
    using_worktree: bool
    branch_name: str | None = None
    base_head: str | None = None


@dataclass(frozen=True)
class CodexRunResult:
    command: tuple[str, ...]
    returncode: int
    events_path: Path
    stderr_path: Path
    final_message_path: Path
    thread_id: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    success: bool
    log_path: Path
    failed_command: str | None = None

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from nexus_codex.models import JobConfig, WorkspaceLease


def _slug(text: str) -> str:
    lowered = text.lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return cleaned or "job"


class WorktreeManager:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def prepare(self, job: JobConfig, run_id: int) -> WorkspaceLease:
        repo_root = job.repo.resolve()
        if job.worktree.strategy == "off":
            return WorkspaceLease(repo_root=repo_root, workdir=repo_root, using_worktree=False)
        git_root = self._git_root(repo_root)
        base_head = self._git(["-C", str(git_root), "rev-parse", "HEAD"]).strip()
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        branch_name = f"nexus/{_slug(job.id)}/{stamp}-{run_id}"
        worktree_path = self.state_dir / "worktrees" / _slug(job.id) / f"{stamp}-{run_id}"
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            [
                "-C",
                str(git_root),
                "worktree",
                "add",
                "-b",
                branch_name,
                str(worktree_path),
            ]
        )
        return WorkspaceLease(
            repo_root=git_root,
            workdir=worktree_path,
            using_worktree=True,
            branch_name=branch_name,
            base_head=base_head,
        )

    def restore(
        self,
        *,
        repo_root: Path,
        workdir: Path,
        using_worktree: bool,
        branch_name: str | None,
        base_head: str | None,
    ) -> WorkspaceLease:
        return WorkspaceLease(
            repo_root=repo_root.resolve(),
            workdir=workdir.resolve(),
            using_worktree=using_worktree,
            branch_name=branch_name,
            base_head=base_head,
        )

    def finalize(self, job: JobConfig, workspace: WorkspaceLease, *, preserve: bool) -> None:
        if not workspace.using_worktree:
            return
        if preserve or job.worktree.cleanup == "never":
            return
        if not self._is_clean_checkout(workspace):
            return
        self._git(["-C", str(workspace.repo_root), "worktree", "remove", "--force", str(workspace.workdir)])
        if workspace.branch_name:
            self._git(["-C", str(workspace.repo_root), "branch", "-D", workspace.branch_name])
        shutil.rmtree(workspace.workdir, ignore_errors=True)

    def fork_workspace(
        self,
        workspace: WorkspaceLease,
        *,
        job_id: str,
        label: str,
        run_id: int,
    ) -> WorkspaceLease:
        slug = _slug(label)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        branch_path = self.state_dir / "branches" / _slug(job_id) / f"{stamp}-{run_id}-{slug}"
        branch_path.parent.mkdir(parents=True, exist_ok=True)
        if workspace.using_worktree:
            head = self._git(["-C", str(workspace.workdir), "rev-parse", "HEAD"]).strip()
            self._git(
                [
                    "-C",
                    str(workspace.repo_root),
                    "worktree",
                    "add",
                    "--detach",
                    str(branch_path),
                    head,
                ]
            )
            return WorkspaceLease(
                repo_root=workspace.repo_root,
                workdir=branch_path,
                using_worktree=True,
                branch_name=None,
                base_head=head,
            )
        shutil.copytree(
            workspace.workdir,
            branch_path,
            dirs_exist_ok=False,
            ignore=shutil.ignore_patterns(".git"),
        )
        return WorkspaceLease(
            repo_root=workspace.repo_root,
            workdir=branch_path,
            using_worktree=False,
            branch_name=None,
            base_head=workspace.base_head,
        )

    def merge_workspace(self, source: WorkspaceLease, target: WorkspaceLease) -> None:
        if source.using_worktree and target.using_worktree:
            self._git(["-C", str(source.workdir), "add", "-N", "."])
            diff = self._git(["-C", str(source.workdir), "diff", "--binary", "HEAD"])
            if not diff.strip():
                return
            subprocess.run(
                ["git", "-C", str(target.workdir), "apply", "--whitespace=nowarn", "-"],
                input=diff,
                text=True,
                capture_output=True,
                check=True,
            )
            return
        self._copy_workspace_contents(source.workdir, target.workdir)

    def cleanup_branch_workspace(self, workspace: WorkspaceLease) -> None:
        if workspace.using_worktree:
            self._git(
                [
                    "-C",
                    str(workspace.repo_root),
                    "worktree",
                    "remove",
                    "--force",
                    str(workspace.workdir),
                ]
            )
            shutil.rmtree(workspace.workdir, ignore_errors=True)
            return
        shutil.rmtree(workspace.workdir, ignore_errors=True)

    def _is_clean_checkout(self, workspace: WorkspaceLease) -> bool:
        status = self._git(["-C", str(workspace.workdir), "status", "--porcelain"]).strip()
        if status:
            return False
        head = self._git(["-C", str(workspace.workdir), "rev-parse", "HEAD"]).strip()
        return bool(workspace.base_head and head == workspace.base_head)

    def _git_root(self, repo: Path) -> Path:
        output = self._git(["-C", str(repo), "rev-parse", "--show-toplevel"]).strip()
        return Path(output)

    def _git(self, args: list[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            capture_output=True,
            check=True,
            text=True,
        )
        return completed.stdout

    def _copy_workspace_contents(self, source: Path, target: Path) -> None:
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if relative.parts and relative.parts[0] == ".git":
                continue
            destination = target / relative
            if path.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

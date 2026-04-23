from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from nexus_codex.models import AppConfig, CodexRunResult, JobConfig


class CodexRunner:
    def __init__(self, app: AppConfig) -> None:
        self.app = app

    def run(
        self,
        job: JobConfig,
        *,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        planning_mode: bool = False,
        heartbeat: Callable[[], None] | None = None,
    ) -> CodexRunResult:
        return self._invoke(
            job,
            cwd=cwd,
            artifact_dir=artifact_dir,
            prompt=prompt,
            planning_mode=planning_mode,
            resume_session_id=None,
            heartbeat=heartbeat,
        )

    def resume(
        self,
        job: JobConfig,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        heartbeat: Callable[[], None] | None = None,
    ) -> CodexRunResult:
        return self._invoke(
            job,
            cwd=cwd,
            artifact_dir=artifact_dir,
            prompt=prompt,
            planning_mode=False,
            resume_session_id=session_id,
            heartbeat=heartbeat,
        )

    def _invoke(
        self,
        job: JobConfig,
        *,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        planning_mode: bool,
        resume_session_id: str | None,
        heartbeat: Callable[[], None] | None,
    ) -> CodexRunResult:
        events_path = artifact_dir / "events.ndjson"
        stderr_path = artifact_dir / "stderr.log"
        final_message_path = artifact_dir / ("plan.md" if planning_mode else "final.md")

        sandbox = "read-only" if planning_mode else job.sandbox
        command = [*self.app.codex_command, "--ask-for-approval", job.approval, "exec"]
        if resume_session_id:
            command.extend(["resume", resume_session_id])
        command.extend(["--cd", str(cwd), "--json", "-o", str(final_message_path)])
        command.extend(["-s", sandbox])
        if job.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if job.search:
            command.append("--search")
        if job.model:
            command.extend(["-m", job.model])
        if job.profile:
            command.extend(["-p", job.profile])
        for path in job.add_dirs:
            command.extend(["--add-dir", str(path)])
        for entry in job.config_overrides:
            command.extend(["-c", entry])
        command.append(prompt)

        try:
            with events_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w",
                encoding="utf-8",
            ) as stderr_handle:
                process = subprocess.Popen(
                    command,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    cwd=cwd,
                )
                self._wait_for_process(process, heartbeat)
                returncode = process.wait()
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Codex command not found. Install Codex CLI or override app.codex_command."
            ) from exc

        if not final_message_path.exists():
            final_message_path.write_text("", encoding="utf-8")
        stdout = events_path.read_text(encoding="utf-8", errors="replace")
        thread_id = self._extract_thread_id(stdout) or resume_session_id

        return CodexRunResult(
            command=tuple(command),
            returncode=returncode,
            events_path=events_path,
            stderr_path=stderr_path,
            final_message_path=final_message_path,
            thread_id=thread_id,
        )

    def _wait_for_process(
        self,
        process: subprocess.Popen[str],
        heartbeat: Callable[[], None] | None,
    ) -> None:
        if heartbeat is None:
            process.wait()
            return
        poll_interval = 0.5
        heartbeat_interval = max(1.0, min(30.0, self.app.lease_ttl_seconds / 3))
        next_heartbeat = time.monotonic() + heartbeat_interval
        while True:
            if process.poll() is not None:
                return
            now = time.monotonic()
            if now >= next_heartbeat:
                heartbeat()
                next_heartbeat = now + heartbeat_interval
            time.sleep(poll_interval)

    def _extract_thread_id(self, stdout: str) -> str | None:
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    return thread_id
        return None

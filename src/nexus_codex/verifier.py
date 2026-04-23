from __future__ import annotations

import os
import subprocess
from pathlib import Path

from nexus_codex.models import VerificationResult


def _shell_command(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell", "-NoLogo", "-NoProfile", "-Command", command]
    return ["/bin/bash", "-lc", command]


class Verifier:
    def run(self, commands: tuple[str, ...], *, cwd: Path, artifact_dir: Path) -> VerificationResult:
        log_path = artifact_dir / "verifier.log"
        if not commands:
            log_path.write_text("No verifier commands configured.\n", encoding="utf-8")
            return VerificationResult(success=True, log_path=log_path)
        with log_path.open("w", encoding="utf-8") as handle:
            for command in commands:
                handle.write(f"$ {command}\n")
                completed = subprocess.run(
                    _shell_command(command),
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                )
                handle.write(completed.stdout)
                handle.write(completed.stderr)
                handle.write(f"[exit={completed.returncode}]\n\n")
                if completed.returncode != 0:
                    return VerificationResult(
                        success=False,
                        log_path=log_path,
                        failed_command=command,
                    )
        return VerificationResult(success=True, log_path=log_path)

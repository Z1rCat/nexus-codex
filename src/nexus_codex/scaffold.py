from __future__ import annotations

from pathlib import Path


def write_rl_research_starter(destination: str | Path) -> list[Path]:
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"destination is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)

    files = {
        "README.md": _project_readme(),
        "ideas/idea.md": _idea_template(),
        "inbox/README.md": _inbox_readme(),
        "results/README.md": _results_readme(),
        "experiments/smoke_train.py": _smoke_train_script(),
        "tests/test_smoke.py": _smoke_test(),
        "jobs.rl.toml": _jobs_toml(root),
    }

    written: list[Path] = []
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def _project_readme() -> str:
    return """# RL Research Starter

This project is a starter kit for running RL idea-to-prototype workflows with
Nexus Codex.

Recommended loop:

1. Put the original idea PDF in `ideas/`
2. Convert the key parts into `ideas/idea.md`
3. Use `jobs.rl.toml` with Nexus Codex to plan, implement, and validate
4. Keep fast local checks in `tests/` and `experiments/smoke_train.py`
5. Save lightweight outputs in `results/`

The included smoke training script is intentionally simple. It is a placeholder
for early local verification, not a real RL benchmark.
"""


def _idea_template() -> str:
    return """# RL Idea Template

## Problem

What problem is the agent supposed to solve?

## Environment

- environment name:
- observation / state:
- action space:
- episode termination:

## Reward

How is reward defined? What reward shaping is proposed?

## Core Hypothesis

What is the actual algorithmic idea?

## Baselines

What simple baselines should be compared first?

## Minimal Prototype

What is the smallest credible implementation that can run locally?

## Local Verification

- smoke test command:
- expected output files:
- pass condition:

## Risks

What is most likely to fail first?
"""


def _inbox_readme() -> str:
    return """Drop short task notes here as Markdown files.

Examples:

- simplify the baseline before scaling up
- reduce training steps and validate CPU execution
- compare reward shaping against the raw reward
"""


def _results_readme() -> str:
    return """Store lightweight local outputs here.

Recommended first artifacts:

- smoke_result.json
- short run summaries
- tiny baseline comparisons
"""


def _smoke_train_script() -> str:
    return """from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default="results/smoke/smoke_result.json")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    cumulative_reward = 0.0
    checkpoints: list[dict[str, float | int]] = []
    interval = max(1, args.steps // 5)

    for step in range(1, args.steps + 1):
        reward = 1.0 + rng.uniform(-0.2, 0.2)
        cumulative_reward += reward
        if step % interval == 0 or step == args.steps:
            checkpoints.append(
                {
                    "step": step,
                    "avg_reward": round(cumulative_reward / step, 4),
                }
            )

    payload = {
        "seed": args.seed,
        "steps": args.steps,
        "final_average_reward": round(cumulative_reward / args.steps, 4),
        "checkpoints": checkpoints,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _smoke_test() -> str:
    return """from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SmokeTrainTests(unittest.TestCase):
    def test_smoke_train_writes_result_file(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "experiments" / "smoke_train.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "smoke_result.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--steps",
                    "25",
                    "--seed",
                    "0",
                    "--out",
                    str(out_path),
                ],
                check=True,
                cwd=repo_root,
            )
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["steps"], 25)
        self.assertEqual(payload["seed"], 0)
        self.assertGreater(payload["final_average_reward"], 0.0)


if __name__ == "__main__":
    unittest.main()
"""


def _jobs_toml(root: Path) -> str:
    repo_path = root.as_posix()
    inbox_dir = (root / "inbox").as_posix()
    return f'''[app]
timezone = "Asia/Shanghai"
state_dir = ".nexus"
poll_interval_seconds = 15
max_concurrent_runs = 1
lease_ttl_seconds = 1800
codex_command = ["codex"]

[[jobs]]
id = "rl-plan"
enabled = true
schedule = "0 9 * * *"
repo = "{repo_path}"
mode = "plan_then_execute"
skip_git_repo_check = true
approval = "never"
verifier = ["python -m unittest tests/test_smoke.py -v"]
prompt = """
Read `ideas/idea.md` as the primary machine-readable spec.
Use any PDF in `ideas/` only as reference context.

Turn the idea into:
1. a minimal RL prototype plan
2. code modules to create or edit
3. a smallest local verification plan
4. likely risks and failure modes
"""

[[jobs]]
id = "rl-prototype"
enabled = true
schedule = "0 10 * * *"
repo = "{repo_path}"
mode = "execute"
skip_git_repo_check = true
approval = "never"
agent_roles = ["planner", "executor", "reviewer"]
parallel_executor_count = 3
reviewer_max_rounds = 1
verifier = [
  "python -m unittest tests/test_smoke.py -v",
  "python experiments/smoke_train.py --steps 200 --seed 0"
]
verifier_max_attempts = 1
prompt = """
Use `ideas/idea.md` as the main spec.
Implement the smallest credible RL prototype and make the local smoke checks pass.
Do not optimize for final benchmark performance yet.
"""

[[jobs]]
id = "rl-loop"
enabled = true
schedule = "*/30 * * * *"
repo = "{repo_path}"
mode = "analysis"
skip_git_repo_check = true
persistent_session = true
goal_loop = true
goal_max_idle_wakes = 3
wake_prompt = """
Continue the RL research loop.
Review `ideas/idea.md`, current code, and files under `results/`.
Decide the next best action toward a stable runnable prototype.
"""
prompt = """
Build and supervise the RL prototype until it can run locally in a stable way.
"""

[[jobs]]
id = "rl-inbox"
enabled = true
schedule = "*/15 * * * *"
repo = "{repo_path}"
mode = "analysis"
skip_git_repo_check = true
inbox_dir = "{inbox_dir}"
inbox_glob = "*.md"
prompt = """
Read the research task note, decide what it means for the project, and convert
it into the right next action.
"""
'''

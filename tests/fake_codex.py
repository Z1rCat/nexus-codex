from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    while args and args[0] in {"--ask-for-approval", "-a"}:
        if len(args) < 2:
            print("missing approval value", file=sys.stderr)
            return 2
        args = args[2:]
    if not args or args[0] != "exec":
        print("expected exec subcommand", file=sys.stderr)
        return 2

    final_message_path: Path | None = None
    cwd = Path.cwd()
    prompt = ""
    thread_id = None
    index = 1
    if index < len(args) and args[index] == "resume":
        thread_id = args[index + 1]
        index += 2
    while index < len(args):
        item = args[index]
        if item in {"--cd", "-C"}:
            cwd = Path(args[index + 1])
            index += 2
            continue
        if item in {"--output-last-message", "-o"}:
            final_message_path = Path(args[index + 1])
            index += 2
            continue
        if item in {"--json", "-a", "--ask-for-approval", "-s", "--sandbox", "--model", "-m", "-p", "--profile", "-c", "--config", "--add-dir"}:
            index += 2 if item in {"-a", "--ask-for-approval", "-s", "--sandbox", "--model", "-m", "-p", "--profile", "-c", "--config", "--add-dir"} else 1
            continue
        if item in {"--skip-git-repo-check", "--search"}:
            index += 1
            continue
        prompt = item
        index += 1

    final_message_path = final_message_path or (cwd / "final.md")
    final_message_path.parent.mkdir(parents=True, exist_ok=True)
    thread_id = thread_id or str(uuid.uuid4())

    for token in prompt.split():
        if token.startswith("sleep="):
            time.sleep(float(token.split("=", 1)[1]))
            break

    branch_label = None
    branch_strategy = None
    for line in prompt.splitlines():
        if line.startswith("Branch label:"):
            branch_label = line.split(":", 1)[1].strip()
        if line.startswith("Branch strategy:"):
            branch_strategy = line.split(":", 1)[1].strip()

    if "goal-mode-done-after-two" in prompt:
        counter_path = cwd / ".goal-done-counter"
        count = int(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else 0
        counter_path.write_text(str(count + 1), encoding="utf-8")
        if count >= 1:
            final_message = "GOAL_STATUS: done\nGoal completed successfully."
        else:
            final_message = "GOAL_STATUS: continue\nMade progress and should wake again."
    elif "goal-mode-idle" in prompt:
        final_message = "GOAL_STATUS: idle\nNo productive next step right now."
    elif "Role: planner" in prompt:
        final_message = "Plan:\n1. Inspect the workspace.\n2. Make the smallest change.\n3. Request review."
    elif "Branch candidates:" in prompt:
        final_message = (
            "BRANCH_CHOICE: branch-robust\n"
            "Prefer the robust branch because it balances correctness and validation."
        )
    elif "Role: reviewer" in prompt:
        if (cwd / ".review-pass").exists():
            final_message = "REVIEW_DECISION: approve\nThe current workspace is ready."
        else:
            final_message = "REVIEW_DECISION: revise\nAdd the review-pass marker before approval."
    elif "Role: executor" in prompt:
        if branch_label:
            marker_path = cwd / f"candidate-{branch_label}.txt"
            marker_path.write_text((branch_strategy or "") + "\n", encoding="utf-8")
            final_message = (
                f"Executor candidate {branch_label} completed.\n"
                f"Strategy: {branch_strategy or 'n/a'}"
            )
        elif "Reviewer feedback" in prompt:
            (cwd / ".review-pass").write_text("ok\n", encoding="utf-8")
            final_message = "Executor applied reviewer feedback and updated the workspace."
        else:
            final_message = "Executor completed the first implementation pass."
    elif "Planning mode only" in prompt:
        final_message = "Plan:\n1. Inspect the repo.\n2. Implement the fix.\n3. Run verification."
    elif "Verifier failed" in prompt:
        (cwd / ".nexus-pass").write_text("ok\n", encoding="utf-8")
        final_message = "Applied the smallest fix required to satisfy the verifier."
    elif "Relevant memory from previous runs" in prompt:
        final_message = "Execution complete with memory.\nRemembered prior context successfully."
    else:
        final_message = "Execution complete.\nAll requested work finished successfully."

    final_message_path.write_text(final_message, encoding="utf-8")
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "message", "role": "assistant", "content": "starting"},
        {"type": "message", "role": "assistant", "content": final_message},
    ]
    for event in events:
        print(json.dumps(event))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

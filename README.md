# Nexus Codex

Nexus Codex is a minimal automation runtime that turns Codex CLI into a
scheduled worker.

This MVP focuses on a few operational primitives:

- `jobs.toml` driven scheduling
- SQLite-backed run history and lease tracking
- optional `git worktree` isolation
- Codex CLI non-interactive execution
- verifier commands after Codex finishes
- verifier retry loop using the same Codex thread
- persistent Codex sessions with scheduled wake-ups
- task inbox auto-discovery for file-based automation
- first-class task records on top of run attempts
- local long-term memory distilled from previous runs
- bounded multi-agent orchestration with planner/executor/reviewer roles
- dispatcher-style intake with queued tasks, dedupe, and priority ordering
- goal-loop supervision with durable goals, backoff, idle detection, and stop conditions
- supervised Codex execution with lease heartbeats
- plan-only runs that can be approved later

## Quick start

1. Create a sample config:

```bash
python -m nexus_codex init
```

2. Edit `jobs.toml` and point a job at a local repository.

3. Run one job immediately:

```bash
python -m nexus_codex run-once daily-issue-triage
```

4. Start the scheduler:

```bash
python -m nexus_codex worker
```

Experimental branch note:
`feature/rl-research-assistant` also adds `python -m nexus_codex init-rl-starter`
to scaffold an RL idea-to-prototype starter project. See
[docs/RL-RESEARCH-MODE.md](docs/RL-RESEARCH-MODE.md).

## Commands

- `python -m nexus_codex init`
- `python -m nexus_codex init-rl-starter --output <dir>`
- `python -m nexus_codex list-jobs`
- `python -m nexus_codex run-once <job-id>`
- `python -m nexus_codex dispatch-job <job-id> --trigger schedule`
- `python -m nexus_codex drain-inbox <job-id>`
- `python -m nexus_codex dispatch-inbox <job-id>`
- `python -m nexus_codex worker`
- `python -m nexus_codex dashboard`
- `python -m nexus_codex history`
- `python -m nexus_codex tasks`
- `python -m nexus_codex goals --job-id <job-id>`
- `python -m nexus_codex memories --job-id <job-id>`
- `python -m nexus_codex sessions`
- `python -m nexus_codex job-status <job-id>`
- `python -m nexus_codex pause-job <job-id> --reason "<reason>"`
- `python -m nexus_codex resume-job <job-id>`
- `python -m nexus_codex operator-note <job-id> "<note>" --dispatch`
- `python -m nexus_codex show-run <run-id>`
- `python -m nexus_codex run-task <task-id>`
- `python -m nexus_codex run-next-task <job-id>`
- `python -m nexus_codex show-task <task-id>`
- `python -m nexus_codex show-goal <goal-id>`
- `python -m nexus_codex show-memory <memory-id>`
- `python -m nexus_codex approve <run-id>`
- `python -m nexus_codex resume-run <run-id> "<follow-up prompt>"`

## Notes

- Codex CLI is not bundled. The default command is `codex`.
- `plan_then_execute` runs in planning mode first, stores a plan, and stops in
  `awaiting_approval`. `approve <run-id>` resumes the same Codex exec session
  when a stored `thread_id` is available, preserving context across stages.
- `resume-run` is the generic continuation primitive for any previous run with a
  stored Codex `thread_id`.
- Set `verifier_max_attempts` on a job to let Nexus feed verifier failures back
  into the same Codex session before giving up.
- Set `persistent_session = true` on a job to turn scheduled wakes into
  `codex exec resume ...` on the latest resumable thread for that job.
- Set `inbox_dir` on a job to let the worker automatically consume matching task
  files and run Codex against them.
- Use `tasks` and `show-task` to inspect the task control plane separately from
  raw run attempts.
- Use `dashboard` for a one-line operational view of backlog, active sessions,
  approval stalls, and scheduler block reasons across all jobs.
- Use `sessions` to inspect which jobs currently have a resumable Codex thread,
  which workspace it lives in, and the last known session status.
- Scheduler intake is now split into `dispatch -> ready queue -> execution`.
  Scheduled wakes dedupe per job, and inbox tasks get a simple priority score so
  urgent/security/failure wording is consumed before routine work.
- Nexus now writes distilled memory entries from plans, successful outcomes, and
  failure patterns, then injects relevant memory back into future prompts.
- Use `memories --job-id <job-id> --query "<text>"` to inspect what the local
  retriever would bring back for a given task.
- Repeated memories are compacted instead of appended forever. Matching memory
  entries now merge by fingerprint/cluster key, keep an `occurrence_count`, and
  let repeated failure patterns become stronger retrieval candidates.
- Set `agent_roles = ["planner", "executor", "reviewer"]` to run a bounded
  multi-agent pipeline in one workspace. Set `parallel_executor_count > 1` to
  let Nexus fork several executor candidate workspaces, ask the reviewer to
  choose the best branch, merge that branch back, and then continue the normal
  review and verifier loop. `reviewer_max_rounds` limits how many
  reviewer-requested repair cycles are allowed before the task fails.
- Set `goal_loop = true` to turn a job into a supervised long-running goal.
  Goal jobs are prompted to emit `GOAL_STATUS: continue|idle|blocked|done`.
  `goal_backoff_seconds` delays future scheduled wakes after `idle` or `blocked`,
  and `goal_max_idle_wakes` can automatically stop a goal that keeps idling.
- Set `max_daily_wakes` to cap how many scheduled/inbox runs a job can perform
  per local day.
- Set `max_consecutive_failures` to auto-pause a job after repeated failures.
- Long-running Codex processes now renew their job lease while they run, so
  active work is less likely to be mistaken for a stale worker.
- Use `operator-note` to inject human guidance into a job's long-term memory.
  With `--dispatch` or `--run-now`, the note also becomes a concrete task the
  runtime can execute immediately.
- This version intentionally keeps dependencies to the Python standard library.

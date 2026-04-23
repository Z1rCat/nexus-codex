# RL Research Mode

This branch adds an experimental RL-oriented starter workflow on top of the
core Nexus Codex runtime.

The goal is not to turn Nexus into a full RL framework. The goal is to make it
easy to drive this loop:

1. idea PDF and idea summary
2. plan
3. prototype code
4. local smoke verification
5. iterative refinement

## Fast Start

Create a starter project:

```powershell
$env:PYTHONPATH='src'
python -m nexus_codex init-rl-starter --output A:/path/to/rl-research-starter
```

The scaffold creates:

- `ideas/idea.md`
- `inbox/`
- `results/`
- `experiments/smoke_train.py`
- `tests/test_smoke.py`
- `jobs.rl.toml`

## Suggested Workflow

1. Put the original PDF into `ideas/`
2. Rewrite the core idea into `ideas/idea.md`
3. Run planning first:

```powershell
python -m nexus_codex --config A:/path/to/rl-research-starter/jobs.rl.toml run-once rl-plan
```

4. Inspect the plan and approve it if needed:

```powershell
python -m nexus_codex --config A:/path/to/rl-research-starter/jobs.rl.toml approve <run-id>
```

5. Build the prototype:

```powershell
python -m nexus_codex --config A:/path/to/rl-research-starter/jobs.rl.toml run-once rl-prototype
```

6. Start the long-running loop:

```powershell
python -m nexus_codex --config A:/path/to/rl-research-starter/jobs.rl.toml worker
```

## Notes

- Keep the verifier cheap. Prefer smoke tests and short CPU runs.
- Use `operator-note` to steer the research loop without rewriting prompts.
- Use `inbox/` for small experimental tasks and follow-ups.
- Treat this branch as experimental product surface. Keep the core Codex runtime
  generic on `main`.

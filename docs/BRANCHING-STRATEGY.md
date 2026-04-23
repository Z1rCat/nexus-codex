# Branching Strategy

## Main Branch

`main` remains the product line for the core Nexus for Codex runtime:

- autonomous Codex execution
- scheduling and dispatch
- persistent sessions
- verifier loop
- task / goal / memory control plane
- operational controls for Codex-first workflows

Research-assistant or domain-specific automation should not be added to `main`
until it proves useful as a generic Codex capability.

## Feature Branch

`feature/rl-research-assistant` is the isolation branch for experimental
research-workflow features, especially:

- idea PDF / Markdown ingestion
- RL experiment planning workflows
- baseline / smoke-run orchestration
- experiment result summarization
- research-specific prompts, jobs, and templates

This branch is allowed to evolve faster and take on more domain assumptions than
the main Codex automation product line.

## Merge Rule

Only merge pieces back to `main` when they satisfy both conditions:

1. the capability is useful outside RL / ML research
2. the capability strengthens Nexus as a Codex automation runtime rather than
   turning it into a niche research product

Examples that may merge back:

- stronger experiment supervision primitives
- better long-running task monitoring
- more generic artifact / result tracking
- safer operator handoff tools

Examples that should usually stay off `main`:

- RL-specific training templates
- paper / hypothesis generation workflows
- environment-specific experiment recipes
- benchmark-specific evaluation glue

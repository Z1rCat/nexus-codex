# Codex 自动执行 / 定时执行研究笔记

更新时间: 2026-04-22

## 目标

研究如何把 Codex 从“交互式编码助手”扩展成“可自动触发、可后台运行、可恢复、可审计”的执行系统，接近 OpenClaw / SwarmClaw 这类 runtime 的能力，但尽量复用 Codex 官方现有接口，而不是改 Codex 本体。

## 先说结论

最合理的路线不是“魔改 Codex”，而是把 Codex 当作执行内核，在外层补齐下面这些能力：

- 调度: `cron` / 定时器 / webhook / GitHub 事件触发
- 状态: `job`、`run`、`thread`、`approval`、`lease`、`worktree`
- 隔离: `git worktree` 或容器
- 审批: `plan -> review -> execute`
- 恢复: 进程崩溃恢复、僵尸任务检测、断点续跑
- 治理: sandbox、rules、hooks、最小权限 token

一句话概括:

> Codex 负责“做事”，外层 runtime 负责“什么时候做、在哪里做、做到什么程度、失败怎么恢复、结果怎么交付”。

## 最值得研究的项目

### 1. Codex 官方能力

- Codex App Automations
  - 文档: https://developers.openai.com/codex/app/automations
  - 价值: 官方已经支持定时任务、fresh runs、thread heartbeat、worktree、triage inbox。
  - 关键点:
    - 支持独立 automation 和 thread automation
    - 支持 cron 语法
    - 支持 worktree
    - 默认走 unattended 模式
  - 限制:
    - 项目级 automation 仍依赖 app 正在运行，项目也要在本地磁盘上
    - 更像桌面端 background automation，不是自建 daemon runtime

- Codex non-interactive mode
  - 文档: https://developers.openai.com/codex/noninteractive
  - 价值: 最适合做 MVP。`codex exec` 可以直接接脚本、CI、调度器。
  - 关键点:
    - 支持结构化 JSON 输出
    - 支持 `resume`
    - 官方给了“CI 失败后自动修复”的模式

- Codex SDK
  - 文档: https://developers.openai.com/codex/sdk
  - 价值: 适合做服务端编排程序，比单纯 shell 调用更强。
  - 关键点:
    - `startThread()`
    - `thread.run(...)`
    - `resumeThread(threadId)`

- Codex App Server
  - 文档: https://developers.openai.com/codex/app-server
  - 价值: 这是最接近“自建 runtime 内核接口”的官方入口。
  - 关键点:
    - JSON-RPC 2.0 风格
    - 支持 `stdio` JSONL
    - 支持 thread / turn / notification 流
    - 结构化事件远好于 PTY 抓屏

- Codex Rules / Hooks / Security
  - 文档:
    - https://developers.openai.com/codex/rules
    - https://developers.openai.com/codex/hooks
    - https://developers.openai.com/codex/agent-approvals-security
  - 价值:
    - Rules 适合做 allowlist
    - Hooks 适合做 guardrail
  - 注意:
    - hooks 目前不是完整强制边界
    - `PreToolUse` / `PostToolUse` 目前主要拦 Bash，不覆盖全部工具

### 2. openclaw/acpx

- 仓库: https://github.com/openclaw/acpx
- 我认为这是最有技术含量的“会话层样板”
- 关键启发:
  - persistent sessions
  - named sessions
  - prompt queueing
  - crash reconnect
  - structured NDJSON output
  - flow runtime

应该重点借鉴的不是它的品牌，而是它的设计原则:

- 不要抓 PTY 文本
- 会话要可命名、可恢复、可并行
- prompt 队列要内建，不要自己乱抢锁
- 输出必须是结构化事件流，而不是终端字符流

### 3. goldmar/openclaw-code-agent

- 仓库: https://github.com/goldmar/openclaw-code-agent
- 这是目前最接近“把 Codex 变成后台 coding session”的参考实现
- 最值得借鉴的设计:
  - `Plan -> Review -> Execute`
  - worktree 生命周期管理
  - session manager 控制平面
  - 与 Codex App Server 的结构化对接
  - `goal` 循环和 verifier loop
  - 交互式审批按钮和 follow-through

这个项目的价值不在聊天渠道，而在下面几件事：

- 把“后台任务”建模成 session，而不是一次性命令
- 把“审批状态”建模成显式状态机，而不是靠文本总结猜测
- 把 worktree cleanup 建模成生命周期系统，而不是简单删目录

### 4. zxkane/autonomous-dev-team

- 仓库: https://github.com/zxkane/autonomous-dev-team
- 这是“GitHub issue -> agent -> PR -> review -> merge”的流水线模板
- 最值得借鉴:
  - label state machine
  - cron dispatcher
  - `MAX_CONCURRENT`
  - zombie/stale recovery
  - isolated worktree
  - review loop

这个项目说明了一件事:

> 真正的自治不是“让模型自己想”，而是“让调度器有明确状态机、并发限制、失败恢复和验证回路”。

### 5. SwarmClaw

- 仓库: https://github.com/swarmclawai/swarmclaw
- 适合研究“多代理 runtime”
- 值得看的点:
  - heartbeats
  - schedules
  - durable memory
  - branching / joins
  - operator controls

如果你以后想做的不只是定时 coding task，而是“多角色代理团队”，SwarmClaw 比 OpenClaw 主仓更值得抄 runtime 思路。

## 推荐架构

## 1. 控制平面

建议做成一个独立服务，而不是 shell 脚本拼接。

核心组件:

- `Scheduler`
  - 负责 cron / interval / webhook / 手动触发
- `RunCoordinator`
  - 负责选仓库、加锁、分配 worker、状态迁移
- `PolicyEngine`
  - 负责 sandbox 模式、可执行命令 allowlist、人工审批门槛
- `NotificationService`
  - 负责结果投递到 GitHub / Slack / Telegram / 本地 UI

## 2. 数据面

最少要有这些实体:

- `job`
  - 定义任务模板
  - 字段: `id`, `name`, `schedule`, `prompt`, `repo`, `mode`, `enabled`
- `run`
  - 某次实际执行
  - 字段: `id`, `job_id`, `status`, `started_at`, `ended_at`, `result_summary`
- `thread_ref`
  - Codex thread/session 引用
  - 字段: `run_id`, `backend`, `thread_id`, `resume_token`
- `workspace_ref`
  - 执行目录或 worktree
  - 字段: `run_id`, `repo_path`, `worktree_path`, `branch_name`
- `approval_state`
  - 审批状态
  - 字段: `run_id`, `state`, `plan_digest`, `approved_by`, `approved_at`
- `lease`
  - 防并发和僵尸检测
  - 字段: `run_id`, `worker_id`, `heartbeat_at`, `expires_at`

MVP 用 SQLite 就够了，不要一开始上 Redis + Postgres + MQ。

## 3. 执行层接口选型

### 方案 A: `codex exec --json`

优点:

- 最快落地
- 脚本友好
- 很适合 cron / CI / GitHub Action

缺点:

- 长会话能力有限
- 控制粒度不如 App Server
- 后台多 turn session 管理会逐渐吃力

适用:

- 你的第一版
- 定时 triage
- 固定脚本式修复任务
- CI autofix

### 方案 B: Codex App Server

优点:

- 结构化 JSON-RPC
- thread / turn 模型清晰
- 更适合长期 session、恢复、编排、UI 对接

缺点:

- 实现复杂度高于 `codex exec`
- 需要你自己处理事件流、状态同步、错误恢复

适用:

- 你想认真做成一个 runtime
- 需要会话恢复
- 需要后台长期执行和 operator 控制台

### 方案 C: Codex SDK

优点:

- 对应用层更友好
- 比 shell 调用更干净

缺点:

- 如果你最终需要最底层事件流和 transport control，还是会靠近 App Server

适用:

- 做服务端 orchestrator
- 想先避开 shell 级别编排

### 不推荐: PTY 抓终端输出

理由:

- 不稳定
- 难恢复
- 难做精确状态机
- 不适合多任务并行

这也是 `acpx` 一开始就明确反对的路线。

## 推荐执行模式

### 模式 1: 计划型任务

适合:

- 每日 issue triage
- release brief
- PR review summary
- CI failure summary

流程:

1. 调度器触发
2. 启动 run
3. 使用 `codex exec --json` 或 SDK
4. 生成 markdown / JSON 结果
5. 投递到 GitHub issue / Slack / 本地 dashboard

### 模式 2: 计划后执行

适合:

- 自动改代码
- 自动修 CI
- 自动批量重构

流程:

1. 先让 Codex 输出 plan
2. 保存 plan 摘要和 diff 风险评估
3. 自动审批或人工审批
4. 审批通过后在同一 thread / resumed session 中执行
5. 跑 verifier
6. 自动开 PR 或挂起等待决策

### 模式 3: verifier loop

适合:

- “修到测试通过为止”
- “修到 lint + test + typecheck 全绿”

流程:

1. 执行一轮改动
2. 跑 verifier commands
3. 不通过则把失败信息喂回同一 session
4. 直到通过或达到预算上限

`openclaw-code-agent` 的 goal/verifier 模式非常值得照抄。

## 推荐的安全边界

## 默认原则

- 默认 `workspace-write`
- 不默认 `danger-full-access`
- 一条任务一个 worktree
- 只在可信仓库启用“自动写代码 + 自动推送”
- 公开仓库默认禁用全自动 merge

## 具体建议

- 用 Rules allowlist 少数命令
  - 例如 `git status`, `git diff`, `gh pr view`, `pytest`, `npm test`
- 用 Hooks 做 guardrail
  - 例如提交前要求 verifier
  - 例如拦主分支提交
- secrets 文件默认不可读
  - `.env`
  - 私钥
  - token 文件
- 所有自动写代码任务都在 worktree 或容器里跑
- 只给最小权限 GitHub token
- 对公开输入源加 gating
  - issue label 只能由维护者加
  - 评论不能直接触发高权限任务

## 一个容易踩坑的点

Codex Hooks 目前不是完整强制边界，尤其不是“全工具全覆盖”的审计墙。它更像 guardrail，不是沙箱替代品。

所以:

- Rules + sandbox 是硬边界
- Hooks 是软治理
- worktree / container 是隔离层

## 我建议的最小可行版本

### V0

目标:

- 先让系统真的能定时跑起来

实现:

- `SQLite`
- `cron` 调度
- `codex exec --json`
- 单仓库
- 结果写 markdown 文件
- 无自动写入，仅分析和汇总

适合任务:

- 每日提交简报
- issue triage
- PR 风险扫描

### V1

目标:

- 支持代码修改，但保守

实现:

- `git worktree`
- `plan -> approve -> execute`
- verifier commands
- 自动开 PR，不自动 merge

适合任务:

- CI autofix
- 小型重构
- review comment addressing

### V2

目标:

- 让任务真正可恢复

实现:

- 切到 Codex App Server 或 SDK
- thread / turn 持久化
- stale run recovery
- output event store
- operator dashboard

### V3

目标:

- 接近 OpenClaw 风格 runtime

实现:

- 多 repo
- 多 agent role
- webhook / GitHub event trigger
- memory / profile / project context
- escalation policy

## 你应该抄什么，不该抄什么

### 应该抄

- `acpx` 的结构化会话和 prompt queueing
- `openclaw-code-agent` 的 plan-review-execute 和 worktree lifecycle
- `autonomous-dev-team` 的 dispatcher + stale recovery + label state machine
- Codex 官方的 App Server / SDK / Rules / non-interactive patterns

### 不该抄

- 直接抓终端输出做状态机
- 默认高权限 host 执行
- 一开始就做“多渠道 + 多代理 + 记忆系统 + UI + 调度 + GitHub 集成”全家桶
- 没有 verifier 就自动 merge

## 我认为最合理的技术路线

如果你的目标是“把 Codex 变成 OpenClaw 风格的自动化 coding runtime”，我建议按下面顺序做：

1. 用 `codex exec --json` 做单机版 scheduler
2. 加 SQLite run store、worktree manager、verifier loop
3. 加 GitHub dispatcher 和 PR delivery
4. 再切 App Server，做真正的长会话恢复
5. 最后再考虑多 agent / memory / chat channel

这样做的好处是:

- 前两周就能跑出结果
- 不会一开始就被 runtime 复杂度拖死
- 架构上和官方 Codex 未来路线一致

## 下一步可以直接做的原型

我建议你先做这四个模块:

- `scheduler`
  - 读取任务定义，按 cron 触发
- `runner`
  - 封装 `codex exec --json`
- `workspace_manager`
  - 创建 / 清理 `git worktree`
- `verifier`
  - 统一跑 `pytest` / `npm test` / `cargo test` / `go test`

任务配置可以先长这样:

```yaml
jobs:
  - id: daily-issue-triage
    schedule: "*/30 * * * *"
    repo: "A:/repos/my-project"
    mode: "analysis"
    prompt: "Review new GitHub issues from the last 24 hours and produce a triage report."

  - id: ci-autofix
    schedule: "*/10 * * * *"
    repo: "A:/repos/my-project"
    mode: "plan_then_execute"
    prompt: "Check the latest failed CI run, propose a minimal fix, implement it, and run tests."
    verifier:
      - "pytest -q"
      - "ruff check ."
```

## 参考来源

- Codex Automations: https://developers.openai.com/codex/app/automations
- Codex Non-interactive: https://developers.openai.com/codex/noninteractive
- Codex SDK: https://developers.openai.com/codex/sdk
- Codex App Server: https://developers.openai.com/codex/app-server
- Codex Rules: https://developers.openai.com/codex/rules
- Codex Hooks: https://developers.openai.com/codex/hooks
- Codex Agent approvals & security: https://developers.openai.com/codex/agent-approvals-security
- Codex GitHub Action: https://developers.openai.com/codex/github-action
- OpenClaw: https://github.com/openclaw/openclaw
- ACPX: https://github.com/openclaw/acpx
- SwarmClaw: https://github.com/swarmclawai/swarmclaw
- openclaw-code-agent: https://github.com/goldmar/openclaw-code-agent
- autonomous-dev-team: https://github.com/zxkane/autonomous-dev-team

## 当前判断

如果你只想快速得到一个“能定时让 Codex 干活”的版本，做一个轻量 scheduler 就够了。

如果你想做“OpenClaw for Codex”，真正要投入精力的是:

- session model
- approval model
- worktree lifecycle
- verifier loop
- stale recovery
- security boundary

这些才是 runtime 的本体。

# Nexus Codex 前端建议文档

更新日期：2026-04-22

## 1. 调研结论

### 1.1 这个项目当前是什么

Nexus Codex 目前不是一个面向普通用户的 Web 产品，而是一个围绕 Codex CLI 构建的本地自动化运行时。它的核心能力是：

- 基于 `jobs.toml` 的任务调度
- 基于 SQLite 的运行记录、租约和作业状态管理
- `plan -> approve -> execute` 的分阶段执行
- 同一 Codex 线程的 `resume` / 持久会话复用
- verifier 重试闭环
- inbox 任务自动发现和归档
- 可选 `git worktree` 隔离

这些能力在当前代码里都已经有明确建模：

- `src/nexus_codex/cli.py` 暴露了 `history`、`job-status`、`show-run`、`approve`、`resume-run` 等操作入口
- `src/nexus_codex/runtime.py` 管理执行流转、会话恢复、审批恢复、verifier 重试和 inbox 归档
- `src/nexus_codex/store.py` 定义了 `runs`、`leases`、`job_state` 三张核心表
- `jobs.toml.example` 已经体现了 `analysis`、`plan_then_execute`、`persistent_session`、`inbox` 等前端最该展示的产品概念
- `tests/test_runtime.py` 覆盖了计划审批、同线程恢复、持久会话、失败重试、每日唤醒预算、自动暂停等高价值流程

### 1.2 前端应该服务什么目标

这个项目最值得做的前端，不是营销官网，也不是通用管理后台，而是一个“本地运行的 operator console（操作控制台）”。

它应该帮助操作者在最短时间内回答这几个问题：

- 哪些 job 正在运行，哪些被暂停，哪些快出问题了
- 最近 24 小时发生了什么，失败点在哪里
- 哪些 run 在等待审批
- 某个 run 的最终消息、事件流、stderr、verifier 日志分别是什么
- 哪个 job 触发了自动暂停，为什么暂停
- 哪个持久会话还可以继续唤醒或 `resume`

结论很明确：前端的北极星页面应该是“控制面板 + 运行细节页 + 审批中心”，而不是一堆静态配置表单。

## 2. 推荐的前端定位

### 2.1 产品定位

推荐把前端定义为：

> 一个面向单人或小团队操作者的本地任务控制台，用来观察、审批、追踪和恢复 Codex 自动化任务。

### 2.2 不建议的方向

- 不建议先做官网式首页。当前项目的价值不在品牌展示，而在操作可见性。
- 不建议直接套一层通用 Admin 模板。会很快变成“表格 + 卡片 + 蓝紫渐变”的同质化界面。
- 不建议一开始就做可视化配置编辑器。当前项目最缺的不是“改配置”，而是“看懂运行状态”。

## 3. 推荐的视觉方向

### 3.1 主视觉建议：Editorial Mission Control

我建议这套前端采用“编辑感 + 控制台感”的混合方向，而不是传统 SaaS dashboard 风格。

核心气质：

- 像运行控制室，不像企业 OA
- 像产品化工具，不像脚手架后台
- 有技术感，但不走霓虹赛博朋克
- 以信息密度和阅读节奏取胜，而不是浮夸玻璃拟态

### 3.2 视觉语言

- 主背景使用偏纸感、矿物感的浅暖色，不要纯白
- 面板使用深墨色、铁灰色和少量高饱和提示色
- 关键状态色固定映射：成功、运行中、等待审批、失败、暂停
- 加入细网格、分隔线、编号、时间轴刻度，让界面更像“调度台”
- 日志区、事件流、命令和 thread id 使用等宽字体，状态摘要和大数字使用更有识别度的标题字体

### 3.3 建议配色

- 背景：`#F3EFE7`
- 墨色：`#171717`
- 次级面板：`#22252B`
- 运行中：`#2D6F73`
- 等待审批：`#C9791A`
- 失败：`#A83232`
- 成功：`#4A6B2F`
- 强调色：`#D95D39`

### 3.4 字体建议

- 英文标题 / 数字：`IBM Plex Sans Condensed`
- 日志 / 线程 / 命令：`IBM Plex Mono`
- 中文正文回退：`Source Han Sans SC` 或 `Noto Sans SC`

这样可以同时保留工具气质和中文环境下的可读性。

### 3.5 动效建议

- 页面首屏使用分层进入动画，不要每个组件都加小动效
- 状态变化用颜色和位置变化表达，不依赖弹跳动画
- 运行中的任务使用低频脉冲或扫描线提示，避免持续强闪烁
- 时间轴、事件流和日志面板支持“自动滚动 / 暂停跟随”两种模式

## 4. 信息架构建议

推荐至少做下面 6 个页面。

| 页面 | 目标 | 关键模块 |
| --- | --- | --- |
| 总览 Dashboard | 一眼看懂系统状态 | 全局统计、最近失败、等待审批、活跃 job、24h 运行时间线 |
| Jobs 列表 | 看配置和当前 job 状态 | 状态筛选、下一次调度、连续失败数、每日唤醒预算、是否持久会话 |
| Job 详情 | 观察单个 job 的生命体征 | prompt、mode、verifier、worktree 策略、最近 runs、pause 原因、inbox 情况 |
| Runs 列表 | 回看历史执行记录 | 状态筛选、phase、trigger、thread id、创建时间、耗时 |
| Run 详情 | 深入排障和审批 | final message、events、stderr、verifier log、artifact 列表、approve / resume 操作 |
| Approval Center | 处理待审批计划 | plan 摘要、job 上下文、风险提示、批准或驳回入口 |

如果第二阶段继续扩展，再加两个页面：

| 页面 | 目标 | 关键模块 |
| --- | --- | --- |
| Inbox | 跟踪文件任务输入 | 待处理、处理中、已归档、失败归档 |
| Settings / Config | 只读查看配置 | app 配置、jobs.toml 解析结果、路径、时区、并发上限 |

## 5. 页面层级与布局建议

### 5.1 Dashboard

首页不要做成卡片堆叠墙。建议做成三段式布局：

- 顶部：关键指标条
- 中部：24h 运行时间线 + 最近 run 活动流
- 右侧：审批队列和失败告警

首页最重要的不是“总共多少 job”，而是“操作者下一步该点哪里”。

### 5.2 Job 详情页

建议采用“概要 + 配置 + 运行历史”三段结构：

- 顶部放当前状态、下一次运行、暂停原因、预算消耗
- 中段放 prompt、mode、verifier、sandbox、approval、persistent session 等配置
- 底部放最近 runs 表格和 inbox 入口

### 5.3 Run 详情页

这会是整个产品里最重要的页面，建议做成左右双栏：

- 左栏：summary、final message、events、stderr、verifier log 切换区
- 右栏：状态、job 信息、thread id、trigger、workspace、approve / resume / reopen 操作区

这个页面必须支持高信息密度，因为它承担了排障、审计、审批三种任务。

## 6. 推荐技术方案

### 6.1 总体建议

推荐采用：

- 前端：`React + TypeScript + Vite`
- 路由：`TanStack Router`
- 数据层：`TanStack Query`
- 后端接口：`FastAPI`
- 图表：`Apache ECharts`
- 样式：`CSS Modules + CSS Variables`

同时建议把 Web 相关依赖做成可选层，而不是直接破坏当前“核心运行时尽量只依赖标准库”的方向。比较稳妥的做法是：

- 核心继续保留在 `src/nexus_codex/`
- 新增 `web/` 作为独立前端工程
- Python 侧用 `project.optional-dependencies.web` 提供 `fastapi`、`uvicorn` 等可选依赖

### 6.2 为什么这样选

#### React

React 官方文档明确说明，从零开始搭项目需要自己决定路由、数据获取等方案。这个项目不是内容站点，而是状态复杂、交互密集的控制台，组件化组织方式非常合适。

官方参考：

- https://react.dev/learn
- https://react.dev/learn/creating-a-react-app

#### Vite

Vite 适合给这个项目提供一个独立、启动快、改起来快的前端工作区。它很适合这种“Python 核心 + 单独前端工程”的开发模式。

官方参考：

- https://vite.dev/guide/

#### TanStack Query

这个项目天然存在轮询、缓存、后台刷新、分页和筛选。`runs`、`jobs`、`job_status`、`show_run` 这些查询都非常适合交给 Query 管理，不要手写散乱的 `useEffect + fetch`。

官方参考：

- https://tanstack.com/query/latest/docs/framework/react/overview

#### TanStack Router

当前产品的数据视图天然会有大量 URL 状态：筛选状态、job id、run id、tab、日志视图、时间范围。TanStack Router 的路由树和搜索参数能力更适合这类控制台。

官方文档也明确更推荐 file-based route tree，因为同样能力下代码更少。

官方参考：

- https://tanstack.com/router/latest

#### FastAPI

当前 Python 核心还没有 HTTP 接口。若要做漂亮且可扩展的前端，建议在不污染核心运行时逻辑的前提下，增加一个轻量 API 层。FastAPI 官方文档对 REST 和 WebSocket 支持都很清晰，适合后续做实时状态流。

官方参考：

- https://fastapi.tiangolo.com/
- https://fastapi.tiangolo.com/advanced/websockets/

### 6.3 为什么不建议先上这些

- 不建议先上 Next.js。当前不是 SEO 或 SSR 驱动型产品。
- 不建议先上重型组件库。这个项目需要辨识度，不需要又一个标准企业后台皮肤。
- 不建议先做 Electron / Tauri。浏览器本地控制台已经够用，桌面壳不是当前瓶颈。
- 不建议样式层直接依赖 Tailwind 生态模板堆砌。即便使用原子类，也不应把设计语言外包给模板。

## 7. 与当前 Python 核心的边界设计

### 7.1 最重要的原则

前端不要直接绑定 SQLite 表结构，也不要在前端里拼业务语义。应该在 Python 层提供“前端友好模型”。

建议新增一个可选的 Web/API 层，把 `Runtime` 和 `StateStore` 暴露为稳定接口。

### 7.2 建议的 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/overview` | 首页统计、近期失败、审批队列、活跃任务 |
| `GET` | `/api/jobs` | 列出 jobs 及其当前状态 |
| `GET` | `/api/jobs/{job_id}` | 单个 job 详情 |
| `POST` | `/api/jobs/{job_id}/run` | 手动触发一次执行 |
| `POST` | `/api/jobs/{job_id}/pause` | 暂停 job |
| `POST` | `/api/jobs/{job_id}/resume` | 恢复 job |
| `GET` | `/api/runs` | runs 列表，支持筛选 |
| `GET` | `/api/runs/{run_id}` | run 详情 |
| `POST` | `/api/runs/{run_id}/approve` | 批准计划并继续执行 |
| `POST` | `/api/runs/{run_id}/resume` | 继续之前 run |
| `GET` | `/api/runs/{run_id}/artifacts/{name}` | 拉取 final/events/stderr/verifier 等产物 |
| `GET` | `/api/stream` | SSE 或 WebSocket 实时事件流 |

### 7.3 建议的目录结构

```text
src/
  nexus_codex/
    cli.py
    runtime.py
    store.py
    web_api.py
    api_models.py
    api_service.py

web/
  package.json
  vite.config.ts
  src/
    app/
    routes/
    components/
    features/
      dashboard/
      jobs/
      runs/
      approvals/
    lib/
    styles/
```

## 8. 视觉组件建议

建议优先设计下面这些“项目特有组件”，而不是先搭通用按钮和表单。

- `StatusPill`
- `RunTimeline`
- `JobHealthRail`
- `ApprovalQueue`
- `ArtifactTabs`
- `EventStreamPanel`
- `VerifierResultCard`
- `ThreadBadge`
- `WakeBudgetMeter`
- `FailureStreakIndicator`

这些组件会比普通 `Card` / `Table` 更能体现产品特征。

## 9. 分阶段落地建议

### Phase 1：只读控制台

目标：

- 先让操作者能看懂系统状态

范围：

- Dashboard
- Jobs 列表
- Runs 列表
- Run 详情基础版
- 只读 API

这一阶段不要做配置编辑，不要做复杂权限。

### Phase 2：操作闭环

目标：

- 让前端不仅能看，还能操作

范围：

- 手动触发 run
- pause / resume job
- approve run
- resume run
- 实时事件流

### Phase 3：高级操作台

目标：

- 让前端成为真正的日常操作入口

范围：

- Approval Center 完整版
- Inbox 追踪页
- 配置只读解析页
- 失败分组和趋势图
- run 对比视图

## 10. 我最推荐的第一版产品样子

如果只做一个“第一眼就有质感，也真正有用”的版本，我建议：

- 首页做成 Mission Control Dashboard
- 第二页做 Job Detail
- 第三页做 Run Detail
- 右上角固定 Approval Queue 入口

第一版就应该让用户能完成这条路径：

1. 打开首页
2. 看到某个 job 连续失败或等待审批
3. 点进对应 run
4. 读 final message / verifier log
5. 直接 approve 或 resume

如果这条路径顺了，这个前端就已经有价值了。

## 11. 成功标准

前端上线后，建议用下面几个标准判断是否做对了：

- 进入首页 10 秒内能知道系统是否健康
- 进入 run 详情 30 秒内能定位失败点
- 待审批 run 不需要回到命令行即可处理
- 常见操作路径不超过 3 次点击
- 首页和详情页在 1440px 宽屏和 768px 平板下都可正常使用

## 12. 最终建议

对这个项目来说，最值得做的“美观前端”不是漂亮的包装，而是漂亮的可观察性。

建议你优先做一个有鲜明调度台气质的本地 Web 控制台，保留 CLI 作为底层能力，把前端作为：

- 状态总览层
- 审批层
- 排障层
- 会话恢复入口

这样前端不会和现有项目脱节，反而会把 `Runtime`、`StateStore`、`jobs.toml` 这些核心能力真正产品化。

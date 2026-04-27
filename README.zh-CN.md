<h1 align="center">北斗 (Beidou)</h1>

<p align="center">
  <strong>面向 Anthropic Agent SDK 的硬运行时多智能体编排器。</strong>
</p>

<p align="center">
  <img alt="Status" src="https://img.shields.io/badge/status-early%20prototype-orange?style=for-the-badge">
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="SDK" src="https://img.shields.io/badge/built%20on-claude--agent--sdk-d97757?style=for-the-badge">
  <img alt="License" src="https://img.shields.io/badge/license-TBD-lightgrey?style=for-the-badge">
</p>

<p align="center">
  <a href="docs/README.md">Specs</a> ·
  <a href="docs/architecture.md">架构</a> ·
  <a href="docs/limits.md">边界</a> ·
  <a href="docs/agent-runtime.md">Agent 运行时</a> ·
  <a href="README.md">English</a>
</p>

**北斗** 是一个建立在 `claude-agent-sdk` 之上的 Python 编排器：按项目派生
多智能体团队、实时观测，并用代码而非提示词来强约束行为边界。让团队图保持
理智的，是 harness (运行时)，不是提示词——调用方身份在 spawn 时被绑定，
谁调用 `create_team` 就是这个团队的 leader，只有 leader 能终止子节点，
递归深度与扇出在代码里硬限。

如果你想用一个已经能跑通、可观测性紧密、不变量被强制执行的 harness 去验证
自己的多智能体想法，但又还不需要一个通用插件框架，那这个项目适合你。

---

## 目录

- [状态](#状态)
- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [CLI 参考](#cli-参考)
- [配置](#配置)
- [仓库结构](#仓库结构)
- [核心概念](#核心概念)
- [北斗如何接入 SDK](#北斗如何接入-sdk)
- [与 OpenClaw 的差异](#与-openclaw-的差异)
- [为什么用 Anthropic SDK](#为什么用-anthropic-sdk)
- [为什么先做 coding 团队](#为什么先做-coding-团队)
- [设计取向：先做出 MVP](#设计取向先做出-mvp)
- [项目是怎么走到今天的——简史](#项目是怎么走到今天的简史)
- [按目标查文档](#按目标查文档)
- [贡献与开发](#贡献与开发)
- [License](#license)

---

## 状态

早期原型。北斗目前只在 coding 这一个域上跑过。API、事件 schema、每个
primitive 的 wire shape 都仍可能变化。按 [`CLAUDE.md`](CLAUDE.md) 的规则，
spec 改动需要显式批准。**目前没有公开的扩展 API**——见
[设计取向：先做出 MVP](#设计取向先做出-mvp)。

---

## 核心特性

- **per-spawn in-process MCP server。** 每个 agent 拥有独立的
  `create_sdk_mcp_server` 实例，工具闭包在 spawn 时绑定 `caller_id` 和
  orchestrator 句柄。模型从来不从工具入参里读自己的身份——无法伪造。
- **持久 agent。** 任何 agent 都不会自行退出。完成是一种状态
  (`report_status(state="done")`)，不是进程结束。再分配会作为同一 SDK
  会话的下一个 user-role turn 进来。
- **self-lead 不变量。** 谁调用 `create_team` 谁就是这个团队的 leader。
  Leader 是通过 spawn 这个动作获得的，不是按 skill 或角色标签预设。
- **leader-only 终止。** 只有 leader 能终止直属子节点。北斗本身只能终止
  root。级联由运行时按深度优先处理，并配有 watchdog 兜底。
- **`[REVIEW REQUIRED]` 信封约定。** 子节点报告 done 时，PostToolUse hook
  读取该 turn 内最后一段 assistant 文本，投递到 leader 收件箱；leader 必
  须先 `terminate_child`（批准）或 `send_message`（打回重做），才能继续
  做别的事。
- **liveness watchdog。** 评审 pending 的 ping、空闲提醒、三振升级到
  user gateway，跑在独立的 asyncio task 上。
- **代码里的硬限。** 扇出 8、深度 5、收件箱 1000、契约违规三振——
  [`docs/limits.md`](docs/limits.md) 里每一行都是边界，改任何值都需要
  user 批准。
- **可观测性是一等公民。** 每一次工具调用、每一个 turn、每一次完成评审、
  每一笔 cost 汇总，都被追加到 `~/.beidou/events/{task_id}.jsonl`
  （权威）并汇总到 `~/.beidou/stats.db`。Svelte 5 web UI 实时回放。
- **可插拔人审 gateway。** Web、终端、TUI 三种 gateway 走同一接口；
  结构化 `AskUserQuestion` + answer-as-bubble；终端里输入 `approve`/
  `yes` 等自由文本也能识别。
- **三层 workspace。** Project (用户通过 `--workspace` 提供，整次运行共
  享)、team (按 `create_team` 创建)，外加无团队 root agent 的 scratch
  目录。

---

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
beidou init
```

跑一个任务：

```bash
beidou run --model claude-opus-4-7 "Build a REST API with auth and tests"
beidou run --model claude-haiku-4-5-20251001 --skill orchestrator "Write a Python parser"
```

观测一次运行：

```bash
beidou status [task_id]
beidou teams <task_id>
beidou events --follow --task <task_id>
beidou stats <task_id>
```

`ANTHROPIC_API_KEY` 写在 `.env` 里（通过 `python-dotenv` 自动加载）。

> **跑任何 `beidou` 或 Python 命令前先激活 venv。** 北斗在
> **Python 3.12+** 上测过。

---

## CLI 参考

| 命令 | 用途 |
|---|---|
| `beidou init` | 初始化 `~/.beidou/`（事件目录、stats DB、默认配置）。装完跑一次。 |
| `beidou run [OPTIONS] TASK` | 派生 root agent 并执行 `TASK`。常用 flag 见下。 |
| `beidou status [task_id]` | 任务 / 团队 / agent 状态快照。 |
| `beidou teams <task_id>` | 打印某次任务的团队图。 |
| `beidou events --follow --task <task_id>` | 实时跟踪 JSONL 事件流。 |
| `beidou stats <task_id>` | Cost / usage / turn 的聚合 rollup。 |
| `beidou web` | 启动实时观测 UI（Svelte 5 前端）。 |

### `beidou run` 常用 flag

| Flag | 含义 |
|---|---|
| `--model <id>` | 给本机 Claude Code CLI 的提示。权威模型字串以 `AssistantMessage.model` 为准。详见 [`docs/agent-runtime.md`](docs/agent-runtime.md) §6。 |
| `--skill <name>` | Root skill。默认 `orchestrator`。`beidou/skills/` 下任何 skill 都行。 |
| `--workspace <path>` | 项目工作区，整次运行共享。不传就建一个临时目录。 |
| `--template <name>` | 已弃用——会带 warning 转发为 `--skill <name>`。 |

完整 flag 列表跑 `beidou run --help`。

---

## 配置

北斗从环境变量里读两样东西，都通过 cwd 下的 `.env` 注入：

```dotenv
# 必填。自动加载。
ANTHROPIC_API_KEY=sk-ant-...
```

其他都在 `~/.beidou/` 下：

| 路径 | 作用 |
|---|---|
| `~/.beidou/events/{task_id}.jsonl` | 权威事件日志（append-only）。 |
| `~/.beidou/stats.db` | SQLite (WAL)；聚合 rollup 缓存。 |
| `~/.beidou/workspaces/` | 老的 workspace 目录（每次运行的工作区现在走 `--workspace`）。 |

目前没有 `beidou.toml`。边界值（扇出、深度、收件箱上限、hook 超时等）写在
`beidou/orchestrator.py` 和 [`docs/limits.md`](docs/limits.md) 里——刻意
不做成运行时可调。

---

## 仓库结构

| 路径 | 作用 |
|---|---|
| `beidou/orchestrator.py` | 团队注册表、收件箱路由、liveness watchdog、root-only 终止、完成评审接线。 |
| `beidou/sdk_agent.py` | `claude_agent_sdk.query(...)` 的薄包装；消费异步迭代器；emit 北斗事件；持有三个 SDK hook (钩子)。 |
| `beidou/primitives/` | 七个面向 agent 的工具：`core.py`（纯 Python 实现）+ `mcp.py`（MCP 封装）。 |
| `beidou/skills/coding/` | 第一份 skill 集：orchestrator、product_manager、software_architect、junior_engineer、test_engineer、qa_engineer、deployment_engineer。 |
| `beidou/web/` | 观测 UI 的 Svelte 5 前端打包。 |
| `beidou/gateways/` | 可插拔 human gateway：终端、web、TUI、composite。 |
| `docs/` | **真理之源。** 所有行为边界都在这里规定。 |
| `proto_01_long_tool.py`、`proto_02_token_granularity.py` | 当年决定走 SDK 的两个经验性探针。作为一手资料留着。 |

---

## 核心概念

- **Agent（智能体）** — 一次持久的 SDK 会话，跑在一个 skill 之下。绝不
  自行退出；完成只是一种状态，不是进程结束。Spec：
  [`docs/agent-runtime.md`](docs/agent-runtime.md)。
- **Team（团队）** — 一个 leader 加 N 个成员，由一次 `create_team` 调用
  派生。调用方自动成为 leader（self-lead 不变量）。Spec：
  [`docs/orchestration.md`](docs/orchestration.md)。
- **Workspace（工作区）** — 三层。*项目* 工作区由用户通过 `--workspace`
  指定，整次运行共享；*团队* 工作区按 `create_team` 创建、归这个团队
  独占；*root agent* 因为是无团队的（depth=0），单独有一个 scratch
  目录。Spec：[`docs/architecture.md`](docs/architecture.md)。
- **Primitive（原语）** — 每个 agent 都看得到的七个 MCP 工具：
  `send_message`、`list_peers`、`ask_user`、`report_status`、
  `create_team`、`terminate_child`、`list_pending_reviews`。身份
  (`caller_id`) 在 per-spawn 的 MCP 闭包里绑定，模型无法伪造。Spec：
  [`docs/tool-surface.md`](docs/tool-surface.md)。
- **Skill（技能）** — 一个带 YAML frontmatter（`allowed-tools`、
  `description`、`triggers` 等）和系统提示正文的 `SKILL.md` 文件。
  纯声明式，加新 skill 不需要写 Python。Spec：
  [`docs/skills.md`](docs/skills.md)。

---

## 北斗如何接入 SDK

大致六个接入点，集中在两个文件里（`beidou/sdk_agent.py`、
`beidou/primitives/mcp.py`）。

- **`ClaudeAgentOptions`** 携带每次 spawn 的所有内容：拼好的五段式系统
  提示词、`setting_sources=["user", "project"]`、`skills="all"`、MCP
  server、`allowed_tools` 与 `hooks`。
- **per-spawn 的 in-process MCP server**（`create_sdk_mcp_server`）。
  每个 agent 一份。每个 `@tool` 闭包在 spawn 时绑定 `caller_id` 和
  orchestrator 句柄。这是身份不可伪造能落到地的根本原因——模型从来不
  从自己的工具入参里读 `caller_id`。
- **自定义工具** — 七个北斗 primitive，对外暴露成
  `mcp__beidou__<name>`（见 `beidou/primitives/mcp.py`）。
- **三个 SDK hook**，都在 `beidou/sdk_agent.py`：
  - `on_ask_user_question`（PreToolUse）— 把 SDK 自带的
    `AskUserQuestion` 原样转发给北斗的 human gateway。这条路径与 MCP
    `ask_user` 路径产出同一种 `Question` 对象。
  - `on_review_gate`（PreToolUse）— 当一个 leader 有任意直属子节点处
    于 `completion_pending=True` 时，禁止它调用允许列表之外的工具。
  - `on_report_status`（PostToolUse）— 持有完成 (completion) 交接：
    读取报告 turn 内 agent 最后一段 assistant 文本，缺 `[REVIEW
    REQUIRED]` 信封时合成一个，再把内容投递到 leader 的收件箱（如果
    报告者是 root，则走 user gateway）。该 hook 的超时被覆盖为
    1800 秒，避免人审被 claude-code 默认的 60 秒默默截断。
- **Drain loop** 在 `beidou/sdk_agent.py` 里消费 SDK 的异步消息迭代器
  （streaming-input 模式），把 SDK 消息翻译成北斗事件，turn 之间把
  agent park 到一个 per-agent `asyncio.Queue` 上。再分配、对端消息、
  终止哨兵都从这个队列进来。
- **不拦截单次 LLM 调用。** 北斗只观测；重试、缓存、工具分发都归 SDK。

**迁移成本。** 七个 primitive 和团队图本身是 SDK-agnostic 的（纯 Python，
在 `beidou/primitives/core.py` 与 `beidou/orchestrator.py`）。真正与
SDK 耦合的面是 `beidou/sdk_agent.py`（drain loop、消息形态映射、hook
注册）和 `beidou/primitives/mcp.py`（MCP 封装）。一个 `pi-core` 后端
基本就是替换这两个文件，其它都能直接搬。后续可能作为可切换后端而不是
强制替换提供。

---

## 与 OpenClaw 的差异

（*OpenClaw 是一个个人 AI 助理，预设的 persona 在长生命周期的频道和会话
里反复服务用户。*）

- **以项目为单位。** 每次 `beidou run` 都绑定一个 `--workspace`，并派
  生一张全新的智能体图。没有全局的 agent 注册表；agent 不会比一次任务
  活得更久。
- **agent 是按项目派生的。** Agent 是为当下这个任务从一组角色 skill 里
  现派的，不是从一份预设 persona 名册里挑出来跨次复用的。
- **可观测性是一等公民。** 每一次工具调用、每一个 turn、每一次完成评
  审、每一笔 cost 汇总，都落到
  `~/.beidou/events/{task_id}.jsonl`；Web UI 实时回放。用户能看到每个
  agent 做了什么、为什么做。
- **硬 harness，胜过软提示词。** persistent-agent 契约、`create_team`
  的 self-lead 不变量、leader-only 终止、深度与扇出上限、
  `[REVIEW REQUIRED]` 完成评审信封、PostToolUse 评审 hook、liveness
  watchdog——这些都是用代码强制的。提示词在 turn 之间会漂移；harness
  不会。详见 [`docs/agent-runtime.md`](docs/agent-runtime.md) 和
  [`docs/limits.md`](docs/limits.md)。

---

## 为什么用 Anthropic SDK

三条理由，按重要性排序：

1. 项目启动时，Anthropic 的模型在 agentic coding 这类负载上表现最好。
2. `claude-agent-sdk` 已经把缓存友好、可重试的单 agent 循环和
   in-process MCP server (`create_sdk_mcp_server`) 都做好了。再造一遍
   是浪费；北斗早期那套自己写的循环就是漂移的来源，已经退役（commit
   `5f267c2`）。
3. 北斗只负责做 *编排*。它不持有单次 LLM 调用的循环——那归 SDK。所有
   北斗特有的逻辑（团队图、A2A 路由、可观测性、终止权）都在单 agent
   循环之外，在编排器边界这一层。

如果要做 provider-neutral 的版本，正确路径是 `pi-core`，而不是手写一套
harness、也不是用 `pi-agent`。具体迁移要动什么，见
[北斗如何接入 SDK](#北斗如何接入-sdk)。

---

## 为什么先做 coding 团队

agentic harness 在 coding 这一个域里反馈最密集——测试过没过、代码跑没
跑通、build 成没成——所以每跑一次都能拿到最多关于 harness 哪里好用、
哪里碍事的信号。同时这也是我自己最熟的域，足以判断 harness 是在帮我
还是在挡路。

`beidou/skills/coding/` 下的 skill 集是 skill schema 的第一份具体实例：

- `orchestrator` — 默认的 root skill；做阶段规划、派生团队。
- `product_manager` — 把模糊任务转成具体的 `requirements.md`。
- `software_architect` — 产出 `SPEC.md` 和 `tasks.md`。
- `junior_engineer` — 实现单个任务。
- `test_engineer` — 给单个任务写测试。
- `qa_engineer` — 用 spec 验证最终产物。
- `deployment_engineer` — 接通部署 / 发布路径。

后续接入新域的方式是：在 `beidou/skills/` 下再加一个目录。今天，*北斗
被实际跑过的域只有 coding 这一个*。

---

## 设计取向：先做出 MVP

北斗刻意还不是一个通用、面向插件、事件总线驱动的框架。

- 没有公开的扩展 API。新 skill 通过在 `beidou/skills/` 下加一个
  `SKILL.md` 落地；新 primitive 通过编辑 `beidou/primitives/core.py`
  落地。
- JSONL 事件流是暴露出来的，但还没有公开的订阅 API，事件 schema 也没有
  超出 [`docs/observability.md`](docs/observability.md) 之外的稳定版本
  约定。
- harness/skill 这一对，目前是与 Anthropic SDK 共同设计的；我没有抽象
  出一个跨 harness 的中间层。

这是有意为之。我想先在一个真实工作负载（coding）上把框架跑通、收集
UX 反馈，再去做泛化。在 MVP 还没跑通之前去做泛化，常常会把错误的抽象
固化下来。等 MVP 形态稳定，下一步是把 harness/skill 这一对抽出成一个
稳定契约，让别人能接入自己的实现。

---

## 项目是怎么走到今天的——简史

大约四个阶段，约 50 个 commit。

**Bootstrap。** 第一版是一套纯 Python 的手写 agent 循环：用户收件箱
escalation 链、LLM/工具弹性层（重试、退避、错误归一化），还有一份消费
事件总线的早期 web UI。Question gateway 从一开始就是可插拔的——web、
TUI、composite——因为人在回路一直是这个项目的核心。

**SDK 转向——拐点**（`5f267c2`，*cutover: retire manual agent loop,
rewire CLI to Orchestrator*）。两个 throwaway 的探测脚本
`proto_01_long_tool.py` 和 `proto_02_token_granularity.py` 验证了
`claude-agent-sdk` 没有 per-tool 超时、并且暴露了北斗需要的 usage 和
cost 字段。同期把 spec-driven 的工作流脚手架放进了 `docs/`。手写循环
被删掉，北斗从此不再持有单 agent 循环。这之后，北斗就只是一个编排器。

**可观测性与 UI 成熟期。** JSONL 被宣布为权威；SQLite 从事件存储被
降级为聚合 rollup 缓存。工具 span 配上了配对追踪。Web UI 先在
cursor-based 事件流上重写过一次，再用 Svelte 5 重写过一次，加上可折
叠的工具卡片和 markdown 流。Agent 自动 park 到 per-agent 队列上
（`wait_for_message` 和 `read_messages` 都被删掉——运行时主动投递，
agent 不再轮询）。

**Harness 收紧**（`38e3cd1`，*feat(orchestration): unified
teamless-root model + create_team consensus guardrail*）。三层
project/team workspace 和 `--workspace` flag 落地。Leader 主导的完成评
审落地：`[REVIEW REQUIRED]` 信封约定、读取报告 turn 最后一段 assistant
文本并投到 leader 收件箱（root 则走 user gateway）的 PostToolUse hook、
带评审 ping 与空闲 nudge 的 liveness watchdog、`terminate_child` 必须先
completion-pending 才能调用、结构化 `AskUserQuestion`+answer-as-bubble。
Agent 加了人类可读的 `name` 字段。合成的 `tm_root` 单成员团队被去掉了：
root agent 真的是无团队（depth=0），如果任务太大，root 自己调
`create_team` 升级成 leader。`create_team` 加了 `consensus=True`
保护，拒绝 N>1 个成员共享同一个 `(skill, description)`，从而堵掉
"十个 junior 全员重复实现整个 feature" 的脚坑。

**任务下达与提示词卫生**（`18e81df`，*feat(spawn): propagate user
task as first user message; clean role_description*）。一次真实的
"用 React 写一个计算器" 任务暴露了一个隐患：派生出来的 team 成员从来
没收到过用户的真实请求——orchestrator 的 `create_team` 示例里把
description 写成了元描述（meta-description），worker `SKILL.md` 正文
里从来没引用过 `{role_description}`，agent 的第一条 user-role 消息也
只是 orchestrator 自己写的团队协调字串。修复：orchestrator 在
`run_root` 时把用户任务捕获下来，之后每一次 spawn（包括子团队再派生
出来的孙节点）都把这条原始用户任务作为 agent 的第一条 user-role 消息
投递。`{role_description}` 现在严格只承载角色级 scope（root 的会被替
换成空串）；六个 worker `SKILL.md` 各自加了一个 `## Your role-specific
scope` 段落把这个占位符暴露到系统提示里。`create_team` 的 `task` 参
数依然记录在 `TeamRecord` 上供 orchestrator 内部协调用，但已经不再是
agent 的第一条 user-role 消息了。

---

## 按目标查文档

| 你想... | 看 |
|---|---|
| 弄清楚 orchestrator/SDK 拆分、进程布局、事件流向 | [`docs/architecture.md`](docs/architecture.md) |
| 弄清楚持久 agent 契约、完成评审、watchdog | [`docs/agent-runtime.md`](docs/agent-runtime.md) |
| 弄清楚七个 agent-facing primitive 及其 wire shape | [`docs/tool-surface.md`](docs/tool-surface.md) |
| 加 / 改一个 skill（SKILL.md frontmatter、系统提示拼装） | [`docs/skills.md`](docs/skills.md) |
| 弄清楚团队图、self-lead 不变量、终止级联 | [`docs/orchestration.md`](docs/orchestration.md) |
| 弄清楚 JSONL 事件 schema 与统计粒度 | [`docs/observability.md`](docs/observability.md) |
| 查一个硬边界（扇出、深度、收件箱上限等） | [`docs/limits.md`](docs/limits.md) |
| 改 web UI（Svelte 组件、reducer、面板、构建） | [`docs/web-ui.md`](docs/web-ui.md) |
| 找按改动种类划分的开发者 checklist | [`docs/workflows.md`](docs/workflows.md) |

任何非平凡改动的完整 read-first mapping 在
[`docs/README.md`](docs/README.md)。

---

## 贡献与开发

北斗目前是个人规模的原型；欢迎 PR，但 review 会比较慢。

- 看 [`AGENTS.md`](AGENTS.md) 和 [`CLAUDE.md`](CLAUDE.md)——它们写明了
  这个项目的工作规则。人类和 agent 贡献者用同一套规则。
- 在做任何非平凡改动之前先看 [`docs/README.md`](docs/README.md)。
  在 PR description 里写清你看了哪些 spec。
- Spec 改动（修改 [`docs/limits.md`](docs/limits.md)、改
  [`docs/agent-runtime.md`](docs/agent-runtime.md) 或
  [`docs/orchestration.md`](docs/orchestration.md) 里的任何契约、改
  [`docs/skills.md`](docs/skills.md) 的 SKILL schema、改
  [`docs/observability.md`](docs/observability.md) 的事件 schema）需要
  在写代码之前显式获得 user 批准。
- 行为改动必须与对应的 spec 改动落在同一个 commit（cohesion 规则）。
- Issue 跟踪用 [`bd`](https://github.com/beads-software/bd)——跑
  `bd prime` 看完整工作流。

```bash
# 开发循环
source .venv/bin/activate
pip install -e .
pytest
beidou run --skill orchestrator "<your task>"
```

---

## License

待定。在添加 license 之前，本仓库 **保留所有权利**。

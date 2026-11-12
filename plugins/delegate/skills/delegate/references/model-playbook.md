# Claude Code + delegate：按任务选模型的使用手册

核查日期：2026-11-11。以下流程是本插件的操作建议，模型可用性以账号、
网关、CLI 和目录实测为准。API 标价不能用来计算订阅剩余额度。

0.20：调用前在当前轮分析每个子任务，传 task_profile(kind/scope/uncertainty/risk/validation)，
省略 complexity 或用 auto；不新增分类 agent。清晰的低风险执行用 Terra／low，文件处理
可用 DeepSeek Flash／low，日常 Sol／medium，高风险或复杂 Astra／high。不要按整个项目
一概分配强模型，也不要以“快”替代能力／验收。tests／replay 需要 shell 角色，file-only
DeepSeek 不承担命令执行。自动 effort 不选 max／ultra；显式选择仍保留。

## 阅读背景

[Addy Osmani 的文章](https://claude.dev/blog/which-claude-model-should-you-build-with/)
强调：用完成任务的整体成本比较组合；先有验证，再调 effort；小模型适合
容易核验的辅助工作；模型切换应安排在自然阶段边界。本文将这些原则用于
现有 delegate/am 工作流，不复制价格表，也不把文章跑分当成本机结论。

## 两个分别决定的选择

主模型负责当前会话的判断；worker 的 model/effort 由该次任务单独指定。
主模型大，不代表文件查找也要大；Claude-native worker 使用同一 Claude
额度，Codex/DeepSeek 委派则使用对应账号资源，协调仍有主线程成本。

在 Claude Code 中查看 `/model`、`/effort status`、`/usage`。试用另一个主模型
优先新开 `claude --model sonnet` 或 `claude --model opus`，不改变常用会话的模型配置。
`/model <name>` 可能保存为以后会话的默认；切换前先保存 am 交接摘要。
命令及别名以[官方模型配置](https://code.claude.com/docs/en/model-config)为准。

Fable 为强档候选，不自动加入本机白名单。核查时 CLI 为 2.1.321，满足官方
Fable 5.2 版本门槛，但这不证明当前账号可用或费用路径已确认。部分订阅下
它需要 usage credits，非交互请求可能没有确认提示。先在交互选择器核对，
再决定是否授权配置；不要把新模型可识别误认为已通过真实推理验证。

## 针对日常工作的配方

### 快速定位：只要证据，不要推理长文

直接对 Claude 说：

> 先检索 am 中本项目已有结果；足够就复用。否则只用一个快速只读 worker，
> 查指定目录中的错误。返回路径、行号、首个错误、可复现命令；不要改文件。

适合日志、定义定位、已有报告和可计数提取。小问题主线程直接处理。
delegate 用 simple、operation=read；先查看该 provider 的模型及 effort。
不要把“判断实验口径是否正确”当成机械提取交给小模型。

### 日常修复：明确触发条件与通过标准

> 复现这个错误，修改最小范围，运行指定测试。使用一个 builder；返回修改、
> 测试命令和结果、未解决问题。先不提交，主线程验收后再决定如何集成。

以 standard/write 开始。角色权限与模型分开选择；有可靠测试时，优先比较
同一模型的中等与更高 effort。网络、容量或权限故障不代表模型能力不足。
自动重试仍沿用插件的升档规则；要做 effort 对比，显式指定候选组合。

### 数值、并发与重大迁移：给验证契约

> 先确认原版本、冻结输入、时间窗口、单位和不变量。用强模型处理歧义与
> 生命周期分析；辅助 worker 只负责搜证。比较具体反例和独立验证结果，
> 不以“测试通过”代替必要的回放或实际接口验证。

自动 complex 仍有强档/high 的保守门槛；文章中 medium 起点不直接覆盖它。
要试强模型/medium，使用明确的角色、model、effort，仍需相同验收证据。
不要把 max 当作默认；它可能加大推理和工具调用开销。
参见[官方 effort 说明](https://platform.claude.com/docs/en/build-with-claude/effort)。

### 长训练与回放：程序监督，模型处理结果

> 为作业登记完成回执，使用程序等待。正常进度只更新记录；完成或失败后
> 给我一次结果。已有可靠产物先复用，不重新生成。

使用 delegate_exchange watch/poll/ack。需要长时间执行的 worker 可显式
设置 timeout_seconds=0；它仍受账号及 provider 限制。API worker 没有 shell，
不能假设它能启动训练或运行系统测试；只读角色也不能改变为写角色。

### 多文件批量检查：有边界的并行

> 按不重叠目录分工，每个 worker 返回同一结构的证据清单。限制并发，先
> 用小样本确认收益；不要为每个小文件各开一个 worker。主线程只合并结论。

先用已允许的 provider/role 池，保持父任务与 work_item 对应。相同文件
写入、串行依赖很强或单个上下文能轻松完成的工作，优先一个执行者。

## 原生子 agent 的可选模板

如果只想分离 Claude 主线程中的大日志阅读，可自行创建
`.claude/agents/log-evidence.md`；本次不会自动覆盖用户的 Explore 或全局模型。

```markdown
---
name: log-evidence
description: Read explicitly provided test logs and report bounded failure evidence.
model: haiku
effort: medium
tools: Read, Grep, Glob
---
Only inspect the paths supplied by the caller. Do not edit files or scan home roots.
Return at most five failures with path, test name, first error and evidence lines.
State unreadable inputs and missing evidence. Do not declare the parent task accepted.
```

原生模型/effort、前置上下文及实际模型可能受宿主策略影响；检查实际记录。
[官方 subagent 文档](https://code.claude.com/docs/en/sub-agents)说明了这些边界。
不要为了便宜把所有主线程和原生子任务都用一个环境变量强制成相同模型。

## 复用、切换与小型对照

1. 同一逻辑任务优先原对话 resume，避免丢失正在修复的问题。
2. 大阶段结束再考虑换模型；先记录简短计划、产物、失败证据和下一步。
3. 换模型不保证继承全部内部推理；保存明确结论与证据，不依赖隐藏思考。
4. 只读查询不需要唤醒另一个 session；问答预算默认关闭，必要时一问一答。
5. 对模型对比，准备相同输入与通过标准，分别使用新会话和稳定 case_key。
   区分缓存冷启动与已暖缓存，不比较被其他会话同时影响的账户百分比。
6. 用 delegate_inspect(metrics) 查 worker 结果，再用任务 report/compare 查
   主线程开销、重试和完整性。缺失用量时不宣称节省。

较低 effort 失败后，先确认测试或观测是否能定位问题；有清晰证据再提高
一档或切换模型。接近最高 effort 时，将候选强模型的较低 effort 纳入实测，
不要自动越过账号白名单或使用额外付费模式。advisor、fast mode 和大团队
不在此插件中自动开启；它们需要独立的权限、成本与效果验证。

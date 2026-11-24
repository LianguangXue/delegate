# 按子任务自动选模型与零 Token 协同分流（0.28）

0.28 在 0.24 能力／额度选型基础上，新增 **4×4 全平台互调矩阵**（Claude、Codex、Antigravity/Gemini、DSH/DeepSeek）与**零额外模型 Token 的确定性协同顾问（`advisory`）**。详见 [额度与智能一起编排](ORCHESTRATION.zh-CN.md)。

## 4×4 跨宿主角色分工与零 Token 规则（0.28）

任意主控（`claude`、`codex`、`antigravity`/`agy`、`dsh`）均可通过 MCP 或一行 CLI（`delegate-agent advise` / `delegate-agent ask`）调度四类后端：

| 协同场景（`workflow`） | 零 Token 调度规则 | 推荐角色 / 模型 / effort |
| --- | --- | --- |
| 小改动盈亏线（`estimated_lines<=40`） | `<=40` 行修改、修审查意见、前后端紧耦合联调、或主控本就是该领域最佳模型（如 Claude 做纯前端视觉）直接主控自做，避免交接成本倒挂 | `keep_inline`（0 worker） |
| 大方案独立盲审（`dual-plan`, `blind_context=true`） | 主控自己出一份，同时后台派 `agy-advisor`（若主控已是 Antigravity 则派 `claude-reviewer`/`codex-reviewer`）；**只传用户原话与核实过的代码事实，禁止传主控草稿**，合并前核验 `[speculative]` 项 | `agy-advisor`（只读，`--blind`） |
| 界面文案与交互润色（`copy-ux`） | 主控先实现一版，再请 `agy-advisor` 去 AI 腔润色，主控核对业务事实后采纳 | `agy-advisor`（只读） |
| 接口已定死的独立后端／算法（`backend-module`） | 仅派发接口与验收明确的独立模块；简单用 `gpt-6.1-terra` `low`，常规用 `gpt-6.2-sol`/`gpt-6.1-sol` `medium`/`high`，**`astra` 严格限真 `complex`（`escalate=true`）** | `codex-builder`（隔离 worktree） |
| 合并前门禁审查（`gate-review`, `review_gate=true`） | 优先 `gpt-6.2-sol` 默认 `high`（仅触及钱／账单／鉴权／权限／登录／外发消息／发布配置等敏感路径升 `xhigh`）；严格区分 `BLOCKING`（算错钱、丢数据、越权、错发外部消息、可复现崩溃）与 `ADVISORY`，**最多审 2 轮**（`>=2` 轮未过升级交由用户拍板） | `codex-reviewer`（`--review-gate`） |
| 批量低成本任务（`bulk-cheap`） | 明确机械整理、文档或探索任务默认使用 `deepseek-flash`（除非显式要求或复杂升级，不选 `v4-pro`）或 `claude-explorer`（`haiku`） | `deepseek-*` / `claude-explorer` |

一行命令速查（零 JSON heredoc 开销）：

```sh
delegate-agent advise "重构撮合结算模块" --host claude --workflow backend-module --lines 180
delegate-agent ask agy "独立给出缓存一致性架构方案" --host claude --blind
delegate-agent ask codex-review "合入前门禁审查" --host antigravity --review-gate --files plugins/delegate/scripts/service.py
```

你可以直接描述目标，让 Claude 在当前轮分析每个子任务后调用 delegate。
不需要你逐项指定模型，也不额外启动一个模型来做分类。新调用指引和 SessionStart
hook 都要求主 agent 先评估再分发；插件根据结构化评估执行选择，而不是数关键词、
按提示长度判断难度，或把整个项目的复杂程度复制给每个子任务。

## 当前分工

| 子任务特点 | 初始偏好 | 通常 effort |
| --- | --- | --- |
| 极小查找、一个直接回答 | 主会话完成 | 不启动 worker |
| 范围明确、低风险的执行、机械修改 | Terra；需要 shell 时使用有执行工具的角色 | low |
| 范围明确的文件整理、提供材料后的文档处理、有界只读检查/复现 | DeepSeek Flash／Terra | low |
| 信息缺口较大但低风险的调研、文档/配置核查 | Sol（不升级 Astra） | medium |
| 日常实现、常规评审、一般调试 | Sol（跨模块调试/评审或 replay 验证升至 high） | medium／high |
| 架构、高正确性风险、很高不确定性、系统层工作 | Astra | high |

这些是工作流偏好，不是本机跨模型跑分或速度排名。同档可行候选仍考虑观察到的
开销与额度；优先模型无容量时只考虑允许池内满足要求的候选，不开启手动账号。
新模型仍需目录支持、角色白名单及足够额度。显式指定模型／effort 时保留你的选择。

官方资料将 Terra 用于明确且可核查的工作，Sol 用于常规或成本敏感的工作，Astra
用于要求高的任务：[OpenAI 模型选择](https://developers.openai.com/api/docs/guides/model-selection)、
[Work／Codex 模型说明](https://learn.chatgpt.com/docs/models)。本插件中的 DeepSeek
分工来自用户工作流偏好，并未据此宣称与 Astra 等价或实测更快。

## Claude 在调用时分析什么

`task_profile` 包含以下必需字段，主 agent 在原有分析中填写即可：

- `kind`：lookup／documentation／mechanical／implementation／review／debugging／architecture／research。
- `scope`：bounded／cross-module／system。
- `uncertainty`：low／medium／high，指解决问题需要的推理与不确定性。
- `risk`：low／medium／high，指做错的影响与正确性要求。
- `validation`：inspection／tests／replay。

可选 `reasoning`（low／medium／high，深层权衡/算法推理可设为 `high` 触发 `high` effort 而不强制升高模型档位）、`requires_shell` 和最多 600 字符的 `reason` 解释该评估。如果缺少必要输入，
先澄清或复用已有证据，不要把缺信息的任务直接交给昂贵模型碰运气。

```json
{
  "agent": "auto", "model": "auto", "effort": "auto",
  "operation": "write", "work_item": "focused-fix",
  "task_profile": {
    "kind": "implementation", "scope": "bounded",
    "uncertainty": "medium", "risk": "medium", "validation": "tests",
    "reason": "修改范围已知，需要实现并运行回归"
  },
  "notify": true
}
```

这是加入原有 task／acceptance／cwd／rationale／session_id 等参数的片段；
模型通常会选 Sol／medium。无需再填复杂度，也可写 `complexity="auto"`。
显式复杂度是最低要求，高风险评估不能被一个 `simple` 标签压低。

清晰、低风险、范围明确的 lookup／documentation／mechanical 使用快档；
中等不确定性或风险、跨模块、常规实现／评审／调试／研究至少使用日常档；
例外是 bounded、低风险、低不确定性且 validation=tests 的明确小改动，可用快档执行并测试；
高风险、高不确定性、系统范围或架构工作使用复杂档。具体领域仍由主 agent
理解：例如核对几处文档链接不等于设计评分公式，运行已给定脚本也不等于推导算法。

复杂项目里的机械步骤可用快档，且不会改写主任务 ledger 的原复杂度。省略 profile
时兼容原有显式／继承复杂度，信息不足不会擅自降档。每个 work_item 有自己的评估；
重试会继承该 item 的 profile。显式新 profile 可以更新它，但不得借新 ID 绕过次数限制。

## 工具、额度和失败处理

`validation=tests/replay` 或 `requires_shell=true` 要求能执行命令的角色。未显式选择
`permissions="workspace-shell"` 时，Claude／Anthropic-compatible workers 仅保留文件工具，
因此默认自动路由会排除它们，不会把执行任务转成主 agent 的额外测试工作；显式传入
`permissions="workspace-shell"` 时，主 agent 可在隔离 worktree 内为这些角色开放 `Bash`
与 `bypassPermissions` 并参与路由或直接派发。固定选择了 `read-only`／`workspace-write`
file-only 角色时仍会提前解释能力不符，不悄悄切账号。它不授予 GPU／SSH／联网权限，
相关 requirements 仍需单独声明。

自动 effort 通常为 low／medium／high，最多 xhigh，不会因“自动”选择 max／ultra。
只有实际推理后的验收失败会触发质量升级；初始化、worktree、过载等基础设施／传输
问题本身不会增加 effort。已有推理任务的重试仍保留模型与 effort 下限，避免反复降档。
如果已有显式高 effort 的结果需要续跑，但自动上限无法满足，需明确决定后续选择。

模型目录顺序不再让 Terra 被 Sol 遮住；所有合格候选先计算预计工作量，再做额度和偏好比较；仅展示时限制候选数。真实耗用与验收证据仍用于原有统计，不能由路由规则推导出账单
节省保证。`delegate_control status` 的 `model_policy` 可查看偏好，计划／回执中的
`assessment`／`task_assessment`、`routing_lane` 和候选解释可用于核查选型。

分开的 plan／run 会绑定评估；明确改动 profile 后需新计划。接受结果的评估发生变化时，
必须先重新核验产物，不能直接复用旧结论。off／manual、账户、角色、提交、资源权限
仍独立生效；这项改进不会开启自动委派或更换主模型。

旧 MCP 会保留旧 schema／调用指引。重载或新会话后使用新版；也可通过当前
`delegate-agent call` 和原 session_id 调用。不要为应用升级中断正在运行的任务。

# 按子任务自动选模型（0.20）

你可以直接描述目标，让 Claude 在当前轮分析每个子任务后调用 delegate。
不需要你逐项指定模型，也不额外启动一个模型来做分类。新调用指引和 SessionStart
hook 都要求主 agent 先评估再分发；插件根据结构化评估执行选择，而不是数关键词、
按提示长度判断难度，或把整个项目的复杂程度复制给每个子任务。

## 当前分工

| 子任务特点 | 初始偏好 | 通常 effort |
| --- | --- | --- |
| 极小查找、一个直接回答 | 主会话完成 | 不启动 worker |
| 范围明确、低风险的执行、机械修改 | Terra；需要 shell 时使用有执行工具的角色 | low |
| 范围明确的文件整理、提供材料后的文档处理 | DeepSeek Flash／Terra | low |
| 日常实现、常规评审、一般调试 | Sol | medium |
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

可选 `requires_shell` 和最多 600 字符的 `reason` 解释该评估。如果缺少必要输入，
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
高风险、高不确定性、系统范围或架构工作使用复杂档。具体领域仍由主 agent
理解：例如核对几处文档链接不等于设计评分公式，运行已给定脚本也不等于推导算法。

复杂项目里的机械步骤可用快档，且不会改写主任务 ledger 的原复杂度。省略 profile
时兼容原有显式／继承复杂度，信息不足不会擅自降档。每个 work_item 有自己的评估；
重试会继承该 item 的 profile。显式新 profile 可以更新它，但不得借新 ID 绕过次数限制。

## 工具、额度和失败处理

`validation=tests/replay` 或 `requires_shell=true` 要求能执行命令的角色。当前
Claude／Anthropic-compatible workers 只有文件工具，因此自动路由会排除它们；
不会把执行任务转成主 agent 的额外测试工作。固定选择了 file-only 角色时会提前解释
能力不符，不悄悄切账号。它不授予 GPU／SSH／联网权限，相关 requirements 仍需单独声明。

自动 effort 通常为 low／medium／high，最多 xhigh，不会因“自动”选择 max／ultra。
只有实际推理后的验收失败会触发质量升级；初始化、worktree、过载等基础设施／传输
问题本身不会增加 effort。已有推理任务的重试仍保留模型与 effort 下限，避免反复降档。
如果已有显式高 effort 的结果需要续跑，但自动上限无法满足，需明确决定后续选择。

模型目录顺序不再让 Terra 被 Sol 遮住；同档先应用偏好再限制候选数，目录中靠后的
偏好模型也能被选到。真实耗用与验收证据仍用于原有统计，不能由路由规则推导出账单
节省保证。`delegate_control status` 的 `model_policy` 可查看偏好，计划／回执中的
`assessment`／`task_assessment`、`routing_lane` 和候选解释可用于核查选型。

分开的 plan／run 会绑定评估；明确改动 profile 后需新计划。接受结果的评估发生变化时，
必须先重新核验产物，不能直接复用旧结论。off／manual、账户、角色、提交、资源权限
仍独立生效；这项改进不会开启自动委派或更换主模型。

旧 MCP 会保留旧 schema／调用指引。重载或新会话后使用新版；也可通过当前
`delegate-agent call` 和原 session_id 调用。不要为应用升级中断正在运行的任务。

# 新派发的版本、模型与输入契约（0.26）

这是第一批增强。升级保留现有账户、角色授权、模型偏好和会话开关；不会自动禁用
DeepSeek Pro 或切换为 Sol 优先。报告分层、独立临时区、通知合并和队列等后续项
不包含在本次实现内。

## 版本与能力门禁

新版 Claude PreToolUse hook 在新的 run、loop start/resume/advance/retry 前要求：

1. 同一个 MCP 工具命名空间通过 delegate_control(action="status") 返回能力信息；
2. 服务版本至少0.26.0，并声明 model-deny、sol-first、snapshot-required-files；
3. 该本地服务进程的 boot/start/PID 身份仍然有效。

首次派发被拦时，查询该连接的 status 一次。旧服务仍只能报告旧版本；这种情况下
统一使用当前 delegate-agent CLI 和原 session_id，或等任务结束后重连。无需反复
重试同一个不兼容的工具，也不要重新运行已完成任务。结果、状态、tell、extend、
cancel、verify 仍可调用；安装不会自动重启用户进程。

SessionStart 会清除旧握手。宿主没有提供连接实例 ID，这仍是命名空间＋本地进程
观察，不保证识别“原服务仍活着却将相同工具名重绑到另一个服务”的特殊情况；
发生重新连接后应再查询 status。Codex/DSH 没有此 Claude hook，仍须检查实际
server_runtime.version，或直接使用新版 CLI。CLI doctor 只证明 CLI 版本。

## 模型硬约束（可选配置）

下面是可选配置示例，不会在安装时自动应用：

```json
{
  "name": "delegate_configure",
  "arguments": {
    "section": "model_policy",
    "value": {
      "mode": "sol-first",
      "deny_models": ["*v4-pro*"],
      "quality_escalation": true
    }
  }
}
```

保存为 request.json 后用 `delegate-agent call --input request.json`。
默认 mode=capability-first；可改回该值并以 deny_models=[] 清空全局禁用名单。
provider 或 agent 的 deny_models 是附加约束，不会被全局列表清空所覆盖。
支持大小写敏感的精确模型 ID／glob，自动选择、显式模型、重试都不能绕过禁用名单。

sol-first 保留日常 Sol、简单 Terra／其他已授权后端的分工，额外限制 Astra 升级：
必须在本次 run/plan 明确传 escalate=true，或同一 work_item 的 Sol 成功执行后被
以具体证据评为 needs-work。基础设施失败、传输错误、无证据的评价不构成质量升级。
quality_escalation=false 可只允许显式升级；显式升级也不能越过 deny_models。
单独 plan 后再 run 时，本次 run 仍需带明确的 escalate=true，不能把计划当作执行授权。

高风险／高不确定性仍受现有能力下限约束。若 Sol 不满足该下限且没有其他允许的合格
候选，会返回 replan 或明确拒绝；不会静默降级。sol-first 不自动禁用其他 provider，
若希望排除某个模型，仍需 deny_models。固定角色只在该角色内选择，不扩大账户池。

策略在分发和 worker 启动时检查，并在 Codex turn/start／CLI Popen 前再次检查，
避免工作树准备期间策略变化后仍启动被禁模型。已经开始的模型任务不会自动被终止。
旧不可变进程无法热更新：启用新规则前，让旧自动 loop 暂停并结束其控制进程；
需要这些新规则的旧 loop 后续 resume/advance 将被拒绝，应保留结果并新建当前版本任务。

## 声明输入自动快照

```json
{
  "name": "delegate_run",
  "arguments": {
    "session_id": "SID", "agent": "codex-builder", "model": "auto", "effort": "auto",
    "cwd": "/path/to/repo/component", "operation": "write",
    "required_files": ["src/model.py", "tests/test_model.py"],
    "snapshot_required_files": true,
    "source_inputs_base": "cwd",
    "source_inputs": ["design.md"],
    "task": "按已确认设计完成修改并运行指定测试",
    "acceptance": "指定回归通过，冲突明确报告",
    "rationale": "隔离工作树使用声明的当前草稿"
  }
}
```

以上派发示例适用于已开启 auto 的会话；off/manual 下的一次明确授权使用 one_shot=true。

source_inputs_base 只控制 source_inputs；省略时仍为 repo，保持旧接口兼容。
required_files/output_files 仍按 worker cwd 解析。明确选 cwd 后 design.md 就是
component/design.md，不在两个基准之间猜测同名文件。source_inputs 仍是精确文件
路径，不接受目录、凭证文件、符号链接或逃逸路径。

snapshot_required_files=true 需要非空 required_files。准备工作树时只把其中
属于仓库的已修改／暂存／未跟踪输入纳入快照，HEAD 中已有的未变文件由正常 checkout
提供。其他会话的未声明修改不复制，也不要求清理整个仓库；源 HEAD/index/文件保持原样。
外部数据仍按只读输入访问，不复制进仓库。忽略文件不会被偷偷纳入，实际工作树缺失
声明输入时仍会在模型推理前失败。路径与内容受既有64文件、1MiB/文件、8MiB总量限制。

同一沙箱策略下的 required-file 预检继续执行。它能检查声明输入，不能推断所有动态
导入、运行时依赖或证明测试已经通过。新任务缺必要输入时直接失败；resume 保留原快照，
不会用新的源文件覆盖已工作的 worktree。需要新输入时创建明确的新任务。

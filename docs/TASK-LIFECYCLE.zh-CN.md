# 工作项与完成状态（0.26.2）

## 新任务不会再共用隐式 main

delegate_run 未传 work_item 时，使用任务、验收、上下文、规范化 cwd、读写用途和
声明文件契约生成稳定的 auto- 标识。相同请求会得到相同标识；不同新任务独立计数，
不会仅因当前父任务里存在 accepted 的 main 作业而被要求复用它。

旧 main 记录按实际请求内容匹配，避免把同一历史 main 桶里另一个任务的已验收结果
误当作本次结果。相同旧工作仍受运行中防重、尝试次数和证据新鲜度检查约束。
不会迁移、改写或重跑历史 job。

显式 work_item（包括 main）、绑定 plan 和 resume_job 的原 ID 保持有效。不要把不同
目标都显式命名为 main。同一目标的修正应继续用回执中的 work_item 或 resume_job；
修改 task/context/acceptance/文件契约会改变自动 ID，不能依靠语义猜测或反复改名绕过
尝试限制。希望看板便于识别时，可以继续指定有意义的阶段名。

delegate_plan 本身没有完整任务文本，单独规划时仍应给出明确稳定的 work_item。
自动 run 已有完整请求，能自行生成对应标识。

## 验收文本不能替代文件 checkpoint

verify accepted 记录实际验收证据，不会伪造 evidence_checkpoint。复用同一结果时，
需要显式选择代码／数据／产物文件，通过 delegate_inspect checkpoint 生成指纹，
重新验证后再 verify。checkpoint 会使旧验收变为 pending，避免把当前文件误称为
过去已验证的版本。不同新工作应使用独立工作项，不必为旧无关结果补 checkpoint。

## native 执行结束与用量完整度分开

新 hook 分别记录 SubagentStart 和 SubagentStop。旧版本只在 Stop 时写入的 path
字段保留其停止事件含义；路径现在缺失不意味着子代理还在运行。报告的 native_agents
列出 agent_id、execution_state、completion_source、usage_status 和恢复办法。

已观察到停止但日志缺失时，用量保持 unknown/incomplete，不补成零、不设置虚假的
complete。finish 在必需插件结果全部收取后可直接关闭，不再依赖旧 Stop hook 对
complete 字段的判断。此时冻结父会话统计边界，最终回复未计量也会明确标记，
measurement_complete=false；不能用于完整成本或节省比例比较。

执行结束尚未确认的 agent 仍会阻止 finish，错误会指出具体 ID：

```json
{"name":"delegate_task","arguments":{"session_id":"SID","task_id":"TASK","action":"report"}}
```

先通过宿主的任务状态／完成回执确认结束，再提供真实证据：

```json
{"name":"delegate_task","arguments":{"session_id":"SID","task_id":"TASK","action":"resolve-native","native_agent_id":"AGENT","evidence":"替换为实际终态回执和检查依据"}}
```

以上请求可保存到文件后通过 delegate-agent call --input request.json 执行。
resolve-native 只确认执行生命周期，不验收插件作业、不填造 usage、不结束正在运行的
进程。不要仅凭日志不存在、等待时间长或希望清掉提示就调用它。

正常日志完整的 Claude 任务仍通过 finish → closing → Stop 捕获最后回复。
缺 native 用量而直接关闭的任务保留不完整统计标记；之后日志恢复可补读 native usage，
但不会把未记录的最终回复算成已测量。任务关闭不等于计量完整。

旧会话可通过稳定 CLI 使用此修复，无需重启 session。已经载入的旧 MCP/hook 不会
热替换；如果继续调用旧 MCP，它仍可能返回旧错误。版本检查门禁保持原行为。

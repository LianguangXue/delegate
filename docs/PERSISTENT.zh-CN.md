# 持久任务与 loop（0.22）

`delegate_loop` 为明确授权的长任务建立独立持久记录和控制器。普通 delegate_run
不会因此变成循环，off／manual 也不会自动开启。控制器在代码中检查状态、等待和
发通知；只有分发下一轮时才请求模型，不再用另一个 agent 反复询问进展。

## 两种模式

| 模式 | 一轮结束后 | 适用场景 |
| --- | --- | --- |
| persistent | 等待主 agent 取全结果、核查并接受这一步，再 advance | 复杂实现、研究分阶段、需要逐步纠偏的任务 |
| loop | 在已授权轮数、token 门槛和间隔内继续，可选输入变化才唤醒 | 周期检查、持续跟踪更新、独立重复评估 |

第一种是默认模式。第二种必须明确请求，且需要正数 token_budget。
每个步骤的验收与整个目标的验收分开；不能为了推进而改弱原目标或跳过证据冲突。
控制器保存最初 goal_acceptance，改变下一步 run_spec 不会改写它。

```text
delegate_loop(action="start", session_id=原ID, mode="persistent", max_cycles=6, token_budget=300000,
  run_spec={
    "agent":"codex-builder", "model":"auto", "effort":"auto",
    "cwd":"/path/to/repo", "operation":"write", "timeout_seconds":0,
    "task":"完成一个可核查的阶段并保存增量检查点，报告剩余工作",
    "acceptance":"给出阶段证据；未完成的原目标项目明确报告，不得伪称全部通过",
    "rationale":"主 agent 持续监督复杂任务",
    "task_profile":{
      "kind":"implementation", "scope":"bounded", "uncertainty":"medium",
      "risk":"medium", "validation":"tests"
    }
  })
```

沿用已有 session opt-in；off／manual 下，只有用户确实明确请求该工作流时才使用
one_shot=true。自动 loop 不会把 manual-only 账号转换为自动账号。角色、provider、
资源、文件输入和并发限制仍由现有派发路径重新检查。

## 主 agent 如何监督

- status：当前轮次、控制器身份／心跳、子任务、真实已观测用量和下一步状态。
- inspect：当前子任务的进程／命令、日志、运行时和结果诊断。
- result：取结果；继续所有 next_result_call，完整收取不等于验收。
- verify：按真实证据给当前步骤 accepted／needs-work／blocked。
- advance：仅在该步已完整收取且 accepted 后，推进下一个逻辑 cycle；run_spec 可更新下一步任务、模型／effort／评估。
- retry：失败或 needs-work 后显式重试同一 cycle／work_item，保留原重试次数和升级下限，不能借新 ID 绕开限制。
- tell：向仍在运行的 Codex 子任务发送指导。API／Claude 文件工具 worker 不支持实时 steering，应在下一轮更新说明。
- pause：当前轮允许收尾，不启动新模型轮次。它不是冻结 OS 进程。
- cancel：请求取消本任务拥有的当前子任务，等待清理确认后结束调度，不会终止其他任务。
- recover：控制器消失时接管已有 child／launch journal，不自动重放可能已收费的请求。
- resume：继续已暂停调度；不能替代 persistent 的验收／advance，也不能替代失败后的 retry。
- limits：明确修改 max_cycles、token_budget、interval_seconds，不清空已耗用量。
- finish：主 agent 用 verdict 和 evidence 单独记录整个目标的结论，不以轮数上限或完成文件代替验收。

status 中保留原 session_id、loop ID 和 task_id。MCP 退出不影响独立控制器；恢复时
使用同一 session_id 和 loop ID。首个已解析角色会固定，之后可在该角色／provider
支持范围内调整模型和 effort。换角色、账号或源码 cwd 应建立新的工作流／明确交接。
没有跨会话的静默权限转移。

长单轮仍使用 run_spec.timeout_seconds：0 明确取消总时限；未指定时保留普通任务
默认值。主 agent 可用状态中的 child.id 调 delegate_job runtime／extend 修改支持的
运行中时限。等待很长计算时，Codex 应使用 delegate_wait，不用模型反复短轮询。

## 自动 loop 的额度边界

```text
delegate_loop(action="start", session_id=原ID, mode="loop",
  max_cycles=4, interval_seconds=300, token_budget=300000,
  trigger_file="progress.json", stop_file="workspace:DONE.json",
  run_spec={...明确的单轮任务与验收...})
```

max_cycles 为 1..100，间隔为 30..86400 秒，自动模式 token_budget 必须大于 0。
这些限制是显式授权范围，不自动续期。token_budget 统计所有已尝试 worker 的已观测
tokens（包括失败与缓存），作为**下一次派发门槛**：不是账单金额，也不是单次推理的
硬上限；当前调用可能超出门槛。控制器会对运行中观测到的超限发一次监督提示，下一次
派发前再次检查最新预算。用量缺失时阻止继续，不能把未知当成零。父会话协调另计。

trigger_file 是原 run_spec.cwd 内由外部 producer 发布的精确小文件。缺失或指纹未变
时不调用模型；仅到时间也不够。未指定 trigger_file 则按明确间隔重复有界工作，仍消耗
对应模型额度。不要为了“持续监督”默认启用反复推理；只监控状态不需要新模型轮次。

stop_file 默认也相对原 cwd，需为包含 done:true 的有效 JSON。要让 builder 在隔离
工作树内发布停止回执，显式使用 workspace:DONE.json。该前缀只允许访问本 loop
确认拥有的 worker cwd。回执停止后续调度，不中断正在收尾的 child，不代表目标已通过。
两类文件均不递归扫描，不跟随 symlink，最多64KiB，建议 producer 原子写入。

关闭父会话自动委派会暂停新的轮次，当前 child 仍可收尾。失败不会被定时器不断重试；
需要主 agent 诊断后显式 retry。周期循环可以在 execution completed 后继续独立轮次，
但不会替主 agent 接受结果；需要每步严格验收时选择 persistent。

## 持久化、结果与通知

控制器和 worker 使用冻结代码；保存 launch intent、cycle／attempt、已消耗量、会话／
工作区和事件序列。恢复先修复已有派发 journal 并接管匹配 child，避免崩溃后重复扣量。
已失效 launch 的迟到 worker 在推理前被隔离。重试不多算逻辑 cycle，但仍计全部调用费用。

每轮正常结束仍归档 worker 对话，下一轮显式恢复同一会话；结果和 worktree 保留。
这不让 sandbox 内原生后台进程永久存活：需要它完成时不要提前结束该轮，必须增量
checkpoint。控制器恢复不是 OS 进程快照，也不能重建已经丢失的计算输出。

Claude 的新 hook 可在步骤完成、受阻、异常心跳或观测预算告警时通知主会话；单个事件
不反复唤醒。status 等显式交互确认的是通知事件，不是 child 全文或目标验收。关闭宿主
后不能承诺实时唤醒，事件保留到下次交互。原生主会话可通过宿主后台命令运行返回的
wait_command／wait_stdin，或使用：

```sh
delegate-agent loop-wait <<'JSON'
{"session_id":"原ID","loop_id":"loop ID","after":0}
JSON
```

Codex／DSH 用 loop-wait codex／loop-wait dsh。等待器只返回事件，不发起模型调用，
也不收取或验收结果。所有 actor 的身份及心跳可由 status 查看，控制器日志路径也在回执中。

每个持久任务使用独立 worker ledger，不劫持主会话当前任务。父任务报表列出引用，
不能据此宣称整个目标计量完整或节省了费用。查看 loop status／对应 task_id 获得所有
worker 尝试的用量。自动循环不继承提交授权；需提交时暂停工作流，走明确授权的提交流程。

旧 MCP 不能凭安装动作获得新工具。先核对 server_runtime.version；使用新版会话或
当前 delegate-agent call，并保留原 session_id。启动该功能不会重跑历史任务，也不会
改变普通委派的默认开关。

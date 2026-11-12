# 运行、长计算与故障诊断（0.19.0）

长报告续取和完成通知参见[结果与等待](RESULTS.md)。notify=true 保留必需结果，
持久在线会话有已确认的等待器时允许当前轮暂停；单次 MCP 等待仍有界。

## 明确范围与运行中延时

错误信息直接显示收到的 timeout_seconds 和实际配置范围。范围同时出现在
delegate_control status、delegate_catalog 和 delegate_job runtime 返回中，不必先试错。
0 表示明确取消总时限；有限值为整数 10..config.max_timeout_seconds 秒。
不要把分配总时限、单次 RPC 超时或 worktree 准备超时混为一谈。

正在运行的支持热控制的 worker 无需等到超时后续跑。追加一小时：

```json
{"name":"delegate_job","arguments":{"session_id":"原会话ID","action":"extend","job_id":"任务ID","extend_seconds":3600,"wait_seconds":5}}
```

新截止时间在当前截止时间后追加；如果时钟刚超过原截止但 worker 尚未开始取消，
则从当前时刻追加。追加后的**总时长**仍受配置最大值约束，超出时明确报数值范围。
已经无总时限的任务不需要追加。只有显式设置 timeout_seconds=0 才取消总时限，不会
因为追加触及上限就自动变成无限运行。

也可用 action=runtime 设置 timeout_seconds：这是从原执行开始计算的总时长，并非
从现在起重新计时；与 extend_seconds 二选一。查询无需修改时只用 action=runtime。
返回包括 deadline_at、remaining_seconds、allowed_timeout_seconds、settings_revision、
worker_applied_revision／timeout_seconds 和 change_state：pending／applied／unconfirmed。
wait_seconds（0..50）可在代码中等待新 worker 的读取确认，避免反复让主模型轮询。
applied 只确认 worker 读取了当前时限，不表示任务有语义进展或必定成功。

延时和超时取消使用同一个短锁判定先后；已进入 cancelling、已请求取消、执行已结束
或后端已开始收尾时不能通过延时恢复。早于热控制功能的 worker 也不能靠修改 JSON
获得该能力。旧 worker 若支持动态时限但没有确认协议，会显示 unconfirmed。

旧 MCP 仍在使用旧参数／文案时，可以按下节用当前 delegate-agent call，在原
session_id 下调整支持热控制的既有任务。不需要为了更新主服务重启已有 worker；
但无法热补旧 worker 本身的执行代码。计时控制和确认不会调用另一个模型，任务仍消耗
所选后端的正常额度。

## 后台派发与收尾诊断

原 delegate_run 参数中添加 **background=true** 即可，不必再调用 detach。
它等价于 await_completion=false，并把本次 wait_seconds 视为 0，立即返回任务回执。
旧参数和 delegate_job detach 保持兼容；普通必需任务的默认行为不变。仅对用户明确
要求的后台工作使用。总时限仍独立配置：长任务需要 timeout_seconds=0 或足够的有限时限。
它不会把 sandbox 内的计算迁移为独立作业，不保证 worker 结束后子进程还存活。

active_commands 来自 Codex 工具事件，shutdown_observation.processes 来自 Linux
进程观察。**live_child_count 是清理前原始子进程数，不等于未完成计算数或进程泄漏数。**
0.17.1 通过已采集的命令行、可执行文件和父子关系识别 Codex app-server／code-mode
辅助进程，分别报告 runtime_helper_count、workload_or_unknown_count 和每行 process_kind。
仅有已确认的运行时辅助进程时，不再提示“任务计算尚未结束”；active_commands 不为空、
未知进程或清理后仍活着的进程仍需核查。不能仅凭进程名排除，缺少命令行的旧记录保持
未分类，不能事后还原或认定为误报。进程分类本身也不能证明任务已完成。

新任务在后端清理后按原始 boot/start/PID 身份再次观察，cleanup 区分
observed_processes_stopped、processes_still_alive、unknown；只证明原记录范围，不是
全系统泄漏检查。原始清理前计数及命令保留。辅助进程如果清理后仍活着，同样告警。

新记录包含 PID／PPID、进程名、脱敏 argv／command_line、cwd 及采集状态。
读取命令前后校验 start／boot 身份，最多 256 个确认归属的进程，每条 command_line
最多 2048 字符。结果摘要最多显示 8 条，优先显示任务／未知进程；更多内容通过
delegate_job inspect 查看。

常见认证参数、Bearer、URL userinfo 和已知凭证会脱敏；不可靠的复杂 shell 正文会
省略。这些是本地诊断信息，不应公开原始进程记录。权限不足、进程退出或身份变化时
明确标记无法采集；以前没有采集的 argv 不能事后还原。观察发生在后端清理前，记录中
尚存活的子进程并不意味着它们在清理后仍存活。

## 验收项与证据冲突

所有后端的新任务和续跑提示均要求逐项报告 PASS／FAIL／BLOCKED／NOT_RUN／CONFLICT，
附命令、输入／产物版本和证据。不能为了通过而跳过项目、调整评价规则、容差、输入、测试
或预期值。旧结果也可能错误，不能把“和旧结果一致”直接当作正确性证明。

发生冲突时保留原验收项，说明冲突证据、影响和建议修正；未执行检查不能报 PASS。
主 agent 用 blocked／needs-work 记录未解决冲突，并明确决定如何处理；只有显式修订后
才能按新标准重新验证，不能声称原标准已通过。插件把规则写进所有 worker 提示及主控
使用指引；它不靠关键词自动证明报告真实或代替主 agent 的逐项验收。

## 测试辅助文件的交付检查

成功的 worktree 任务在后端停止、按授权完成本地提交后生成 **delivery_check**。
它重点发现“本地能跑，但下一份干净 checkout 没有辅助脚本”的问题，例如测试静态
导入了未纳入 Git 的 output/helper.py。报告含 test、referenced_by、行号、dependency
与原因：缺失、本地文件不在当前提交中、或依赖工作树外的脚本。

检查不导入项目代码、不运行测试、不复制文件、不 stage／提交，也不扩展批准的提交
路径。它是 advisory，执行结果与验收仍分开；发现通过运行警告和结果字段交给主 agent。
需要提交 helper 时，仍须批准对应路径。已跟踪文件的正常待交付修改仍应审核 diff。

静态范围有界：最多 64 个测试、128 个文件、1024 次路径探测，30 秒软预算；文件系统
阻塞与进程清理可能使实际时间更长。Git 使用索引元数据；未跟踪测试只查看最多 8 个
已知目录的单层各 128 项，拒绝枚举工作树外的测试目录，不递归扫描 output/data 等。
单文件最多 64 KiB。普通／相对导入、显式 sys.path 和部分标准库 literal loader 路径
可以检查；动态路径、自定义 import hook、exec、生成步骤、外部服务及非 Python 依赖
不保证覆盖。

warning 表示发现需处理的候选依赖；incomplete 表示预算、权限或解析不足；passed
只表示所检查范围内未发现问题，**不代表重新运行测试或完整复现通过**。摘要显示前
5 条发现，完整有界报告保存在任务记录和 inspect 中，截断和限制均会标记。

## 升级后旧会话继续使用

`/reload-plugins` 不保证替换已经连接的 MCP 进程。Claude 官方说明：没有交互式终端的
会话中，MCP 连接变更要到下一个 session 才生效；见
[reload 文档](https://code.claude.com/docs/en/plugins/cli-reference#sessions-without-an-interactive-terminal)。
不应把反复重试当成升级办法，也不应关闭代码指纹校验。

旧会话可以使用安装器维护的固定 CLI，**沿用原 session_id**：

```sh
delegate-agent call <<'JSON'
{"name":"delegate_control","arguments":{"session_id":"原会话的真实ID","action":"status"}}
JSON
```

随后把相同 `name/arguments` 协议用于 delegate_plan、delegate_run、delegate_job 等操作。
CLI 读取同一个任务索引，使用当前代码重新检查 mode、角色、quota、work_item 和授权；
不会复制成另一份任务，也不会自动重跑旧任务。不要通过换 work_item 或提高 max_attempts
绕过失败记录。Codex／DSH 原生入口分别使用 `delegate-agent call codex|dsh`，保留原逻辑
session_id。未安装 launcher 的 Claude 用户可以执行**当前安装版本**中的
`python3 <plugin>/scripts/delegate.py call`。永久切换 MCP 版本仍需在宿主允许时重连／开新会话。

## GPFS worktree 创建与恢复

`worktree_timeout_seconds` 默认 **600 秒**，作用于准备工作区的每个 Git 命令；
配置范围为 10..86400。已有 config 没有这个字段时也使用 600 秒。可以明确请求主 agent：

```text
把 delegate_configure 的 limits.worktree_timeout_seconds 设置为 1800。
```

它独立于模型 timeout_seconds。准备阶段更新 heartbeat、phase=preparing-worktree 和
`workspace_setup`，Git 子进程组按 start/boot 身份登记，支持取消。超时会先停止整个
所属 Git 进程组，再检查 Git registry、common-dir、detached HEAD、base commit、index lock、
工作树状态与原子目录身份；确认完整且干净才继续使用，不凭“目录存在”判定成功。
支持本机 Git 2.31，不依赖新版 add --reason 或 list -z。

失败时记录路径和恢复阶段，只释放此次新建工作树的已知 initializing 锁。干净的失败目录
用普通 worktree remove 回收；修改、未跟踪或 ignored 产物保留，不使用强制删除／全局 prune。
未知锁、路径变更或进程拥有权未确认时保留现场并注明原因。SIGKILL 等无法运行 finally 的
情形不保证自动解锁；父 agent 应先 inspect，不得盲目重派冲突写任务。

## 模型尝试次数与基础设施失败

`max_attempts` 限制实际／可能执行过的模型尝试。新任务从 inference_state=not_started 开始；
Codex 在发出 turn/start 之前标记 attempted，CLI 在启动可能推理的进程前标记 attempted。
丢失响应、usage 缺失、零 token 的已发送请求仍计数，不把未知消耗当零。

明确未进入推理的失败不占模型次数，也不会触发提高 effort／模型档位，或被当作模型质量
失败样本。旧记录只对精确的 runtime identity 拒绝和 preflight worktree-add timeout 进行
兼容识别；不能仅因没有 usage 就免计。原始任务记录保留，不必删历史或重置整个任务。
摘要和账本列出 counts_toward_model_attempt_limit、inference_state、failure_kind。
父 agent 为处理基础设施问题消耗的 token 仍保留，整体额度消耗并不因此归零。

## 固定运行代码

MCP／CLI 入口在加载服务模块前，把同目录 Python 源码保存成按内容哈希命名的
只读快照：`<state>/runtimes/<sha256>/`。服务与其 worker 使用同一份快照；任务记录
包含 `runtime_image.path/digest`。worker 在准备工作目录、调用模型前校验哈希。
无指纹的旧父服务若调用新 worker，会直接失败并提示重启；不会创建 worktree、
调用模型或因复用的 thread ID 触发归档。该限制不终止已经运行中的旧 worker。
源码读取期间变化、语法不完整或快照被改写会被明确拒绝。开发目录后续修改不改变
已经启动的新服务。它保证进程内模块一致，不代替发布审查或对操作系统同用户的隔离。

Codex／DSH 安装器把 managed launcher 指向快照。已有适配器只更新 launcher：

```sh
python3 scripts/install-hosts.py codex --refresh-launcher
```

这也更新 DSH 共用的 `delegate-agent`，不改 MCP/provider/账号设置。旧 launcher
会备份。Claude 安装升级仍按快速安装文档操作。**旧服务必须重载／重启才应用修复**；
不中断当前任务。旧 cache 和历史数据不会自动删除，快照也不随任务完成删除。

## 模型与角色

`delegate_catalog(provider_name="codex")` 返回 `agents`，其中包含确切 name、provider、
workspace、automatic、enabled、模型／effort 限制和说明；另列 configured_providers
与 timeout_seconds 范围。provider `codex` 和角色 `codex-builder`／`codex-reviewer`
是不同字段。模型目录缓存不会缓存角色的启用状态。不要因启动错误偷偷换账号重试。

## 长计算与低额度等待

- `delegate_run(timeout_seconds=0)` 明确关闭总时限，仍支持取消、心跳和父 agent 检查。
  正数的允许范围为 `10..config.max_timeout_seconds`，错误信息列出实际上限。
- `delegate_job(action="runtime", session_id=..., job_id=..., timeout_seconds=0)`
  可移除支持热控制的**正在运行**任务限制。正数从原执行开始计算，并非从修改时间计算。
- 有限时任务在截止前最多 60 秒（短任务约总时长的五分之一）发送一次保存检查点的
  Codex steering，写入 `timeout_warning`。这是提前提醒，不是保证能保存结果；接近截止
  才启动、服务断连或阻塞的工具可能来不及处理。到期仍会中断并清理任务进程组。
- Codex worker 获得动态工具 `delegate_wait(path="completion.json")`，或
  `delegate_wait(pid=<inspect 中确认的子进程 PID>)`。只指定一种条件，
  `timeout_seconds=0` 等到条件、任务结束或预警；正数为单次等待秒数，最大 86400。
  等待代码每两秒探测，不调用模型；父控制循环继续处理心跳、检查、取消和 steering。
  进程可用 host PID 或唯一匹配的 namespace PID；不接受无归属的 PID。
- 文件必须在任务 workspace/source 目录内；只检查普通文件存在，不读取内容。
  **文件存在和进程退出都不是成功证明**。推荐计算脚本增量保存检查点，仅成功后
  原子写入 completion receipt，再由 worker 校验产物。复用已有文件前须确认属于本次运行。
- 必需的计算尚未结束时，worker 不应交付最终答案。后台子进程不保证跨 sandbox／
  app-server 退出存活。本版本不自动把任意命令迁移为外部 detached 作业，不自动重跑
  失败的实验。确需独立长作业时，应由父 agent 在用户授权的项目调度器中启动并收集。

动态工具依赖 Codex app-server 的 experimentalApi、dynamicTools、item/tool/call 协议。
仅新建的 0.13.0 Codex thread 及其后续 resume 带有此工具；旧 thread resume 保留原工具集，
brief 会注明不可用，不能靠升级给旧 thread 热添加工具。
升级后先做小任务验证；协议不支持会报告失败，不降级为高频推理轮询。
参考 [官方 app-server 文档](https://developers.openai.com/codex/app-server)。

## 检查与启动诊断

`delegate_job(action="inspect", session_id=..., job_id=...)` 返回：

- 经 backend start/boot 身份确认的子进程：host PID、namespace PIDs、名称、状态；
  最多 256 个。它不执行 `nsenter`，不提供远程 shell，也不能证明脱离进程组的所有进程已被发现。
- Codex 命令事件中的 active_commands；完成事件可能缺失，故需结合进程与实际产物判断。
- 已知 stderr/output 日志的 canonical path、大小、代码指纹、wait 状态及退出前观测。

Codex stderr 在内存环形缓冲中保留最多 64 KiB，脱敏后写入
`<state>/stderr-<job>.txt`（0600）。环境凭证、当前 Codex auth 中已知 token、JSON 转义
和常见 Bearer／key 形式会脱敏；任意用户内容不保证能自动识别，不应主动把凭证写入命令。
断连错误包含阶段、exit_code 与末尾诊断。stdout JSON-RPC 与 stderr 分开读取，避免死锁。
inspect 只返回日志路径，不把大量日志反复灌入主 agent 上下文。

`shutdown_observation` 保留停止后端前仍存在的子进程／命令，结果摘要给出警告。它不是
checkpoint，也不保证任务恢复。启动失败的根因要根据新的 stderr 与退出码判断；此前
丢弃的 stderr 无法事后还原，多版本并存／编辑时间相关也不等于已证明存在误杀。

## 项目环境约束

Codex workspace-write 默认使用 networkAccess=false、approval=never；显式授予 network／host
profile 时以 execution_environment 中的实际权限为准。Claude／API worker
只有限定文件工具，无 shell。worker brief 明示这些边界，并为 Codex 子环境提供
`NUMBA_CACHE_DIR=/tmp/delegate-numba-<uid>`，避免依赖用户 home 缓存可写。
只读 sandbox 的写权限仍以实际后端为准。

特定项目需把约束写到角色 instructions 或本次 context，例如：

```text
本项目的默认 sandbox 无法访问所需内部数据库；replay 使用项目认可的离线参数，
报告相应指标未验证。不要尝试 SSH。缓存工具仅执行检查模式，不改缓存。
长 replay 必须周期性保存 checkpoint，并用本次 run ID 的 completion receipt。
```

这些内容不作为其他项目的全局参数，也不通过禁用 sandbox 绕过限制。
新版本不保证“完美完成”或实际账单下降；应比较同一验收任务的总 usage、重试、
缓存与父 agent 开销。等待工具只消除其等待阶段的模型调用。

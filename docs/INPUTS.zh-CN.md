# 先核查输入、输出和实际版本（0.21）

安装新版不等于当前会话连接的 MCP 服务已经更新。首次真正分发前，主 agent
应通过 **MCP** 调一次 delegate_control status，查看 server_runtime.version。
CLI 的版本只说明该 CLI 的代码，不能证明旧 MCP 已更换。若旧连接仍保留旧版，
沿用同一个 session_id 使用当前 delegate-agent call，或在宿主允许时重载／新开会话。
不要为升级重跑已经验收的任务，也不要终止仍在运行的实验。

新回执将 job_runtime_version（实际 worker 镜像）和 server_runtime_version
（本次结果由哪个代码版本服务）分别显示。读取旧 job 不能证明现在的 MCP 仍旧；
但新分发若明确使用了旧镜像，新 hook 会给一次升级提示。

## 先读 brief，再声明最低输入契约

```text
delegate_run(...,
  required_files=["BRIEF.md", "/path/to/frozen/data.csv"],
  output_files=[],
  task_profile={..., "requires_shell":true},
  operation="read")
```

主 agent 应先读 brief，分析是否需要 Python／shell、哪些输入必须可读、报告是返回
文本还是写文件。不要仅把一个外部 brief 路径交给 file-only 模型，让它花额度后才发现
全部输入在限制目录外。`requires_shell=true` 或 tests／replay 验证会排除 file-only 角色。

required_files 和 output_files 都是精确路径，最多各 32 个、总计各 8192 个路径字符，
相对路径按 worker 的 cwd 解释。required_files 是最低必需输入，不是文件访问白名单；
它不会自动发现 brief 中所有隐式依赖，也不会复制或扩大权限。明确填写的列表是权威
声明，包括 []。只有省略该参数时，才保守检查 task／context／acceptance 中现存的
绝对文件引用；这种补救启发式不理解“历史路径”“不要读取”等语义，误判时应显式声明。

分发前会检查输入存在、类型和 file-tool 的 cwd 边界，自动规划先排除不兼容角色，
固定角色则直接解释原因。file-tool worker 启动前再次检查本机可读性；Codex 则在
相同 sandboxPolicy 中运行固定 stdlib 文件探针，先于 account／thread／model turn。
只读一字节验证可读，不返回或保存输入内容；缺文件、目录／特殊文件或受限读取失败
不启动模型。检查是当时的可读性，不是冻结数据、全部依赖、项目 CLI 额外权限的证明。

## 只读分析与报告文件是不同要求

只读 reviewer 可以返回 Markdown 报告文本，不能被要求写 report.md。
自动规划时 output_files 非空需要 write 操作；固定角色必须是可写 worktree，
所有输出都限制在实际 worker cwd 内。
输出预检拒绝越界、目录、特殊文件及明显无效的父路径；write_access_verified=false
表示没有试写文件，也未证明实际 CLI／沙箱写入必定成功。它不授予原仓库或外部目录权限。

分析目标是“项目代码只读、只在临时目录保存报告”时，可先让有执行工具的 reviewer
返回报告文本，再由主 agent 按用户授权保存。无需为了这个目标强行创建源码 builder。
长文本通过 next_result_call 续取，不能接受第一段预览；不要为了获得全文重新跑模型。

## 脏仓库与草稿

Builder 在隔离 Git worktree 中工作。源仓库有未提交文件时，使用显式 source_inputs
选择所需草稿，避免 stash／丢弃／提交用户改动。参见[草稿快照](DRAFTS.md)。

```text
delegate_run(agent="deepseek-company-builder", ...,
  operation="write", cwd="/path/to/repo",
  source_inputs=["README.md", "doc/BRIEF.md"],
  required_files=["README.md", "doc/BRIEF.md"],
  output_files=["report.md"])
```

对 file-only builder，required_files 和任务中的输入引用应使用将来工作目录内的
相对路径。原仓库的绝对路径即使已经快照，也不位于新 worktree 的 restricted cwd 内，
因此会在分发前明确拒绝并提示相对路径。插件不静默改写任务或验收中的路径／规则。
source_inputs 相对 Git 根目录，required_files／output_files 相对 worker cwd，
当 cwd 是子目录时需要区分。未选中的草稿不自动复制，大数据／忽略目录不递归搬运。

准备工作区失败会保留“没有启动模型”的分类和恢复建议：选择 source_inputs，或使用
只读 reviewer 返回文本。不会自动提交、stash、删除改动，也不会自动重试付费任务。

## 旧会话的当前 CLI

```sh
delegate-agent call <<'JSON'
{"name":"delegate_control","arguments":{"session_id":"原会话ID","action":"status"}}
JSON
```

同样的 name／arguments 协议可调用 run／plan／job result；始终沿用原 session_id。
Codex／DSH 原生父会话使用 call codex／call dsh 和其原逻辑 ID。旧结果可由新 CLI
分页补取，保留原结果、归档和验收。看到 next_result_call 时继续直到没有下一页，
并核对 result_delivery_complete；以前的完整收取记录不等于你当前上下文已读完每页。

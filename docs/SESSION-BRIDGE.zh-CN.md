# 旧会话直接调用新版本（0.26.1）

旧 Claude Code 会话可以继续使用原 session_id、任务和上下文，通过稳定 CLI 调用
新版 delegate；无需重启整个 session，也无需重新派发已经完成的任务。

给旧会话一次以下指令：

> 本会话后续所有 delegate 操作统一使用 delegate-agent CLI，不再调用旧 delegate MCP。
> 先检查 delegate-agent --version。保留当前 delegate session_id、job_id 和 work_item。
> 通用操作用 delegate-agent call 的 name/arguments JSON；新参数需要时查询 schema。
> 结果优先导出完整文件再验收，长任务使用 managed wait。不要重新运行已有任务。

```sh
delegate-agent --version
delegate-agent doctor
delegate-agent call <<'JSON'
{"name":"delegate_job","arguments":{"session_id":"原SID","job_id":"原JOB","action":"status"}}
JSON
```

run、result、verify、tell、extend、loop 等工具均可通过同一 JSON envelope 调用。
`delegate-agent result JOB --session-id SID --output /tmp/report.md` 可导出完整报告。
Codex／DSH 父会话分别使用 `call codex`／`call dsh`，便利命令带对应 `--host`；
保留 delegate_host 已分配的逻辑 SID，不与其他会话混用。

## 为什么后续升级不必重启旧会话

托管入口先从当前 CLAUDE_CONFIG_DIR（缺省 ~/.claude）的 installed_plugins.json
选择唯一的用户级 delegate 安装，核对版本和 manifest，再执行该版本。没有用户级
安装时使用安装器记录的原生宿主 frozen fallback。不猜测 project/local 作用域覆盖。
损坏、歧义或低于0.26的用户级安装会报错，避免悄悄退回旧规则。

每次 CLI 调用重新解析当前安装，因此完成正常插件安装／激活后，下次调用使用新版本。
只有 git pull 源码并不等于激活新版插件；native-only 用户更新后刷新托管入口：

```sh
python3 scripts/install-hosts.py codex --refresh-launcher
```

DSH 用户可将 codex 替换成 dsh；刷新模式只更新已有托管入口。
入口首次安装此 resolver 后，用户级插件的后续激活无需再次刷新入口。

解析和版本选择只发生在调用起点。在途命令、等待器和已派发 worker 继续使用启动时
的冻结代码；新调用使用已激活代码。升级不改变账户、权限、SID、验收状态或原任务内容。

## 边界

已经运行的旧 MCP 进程、其工具 schema 和已加载 hook 不能被这条 CLI 路径热替换。
因此旧会话需要上述一次性使用指令；这不是自动向其他会话发送消息。CLI 通用 JSON
参数绕过旧 MCP schema 的限制，但仍执行新引擎的权限、模型和输入检查。
若仍选择直接调用 MCP，需要按宿主支持方式重连该 MCP；使用 CLI 不要求重连。

本实现不发送模型请求来检查升级，也不轮询所有会话。版本选择只读两个小元数据文件，
实际引擎仍执行原有冻结运行时校验。

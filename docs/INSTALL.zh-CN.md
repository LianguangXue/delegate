# 快速安装：Claude Code、Codex、DeepSeek Harness

当前实现：delegate 0.17.1；可选 am 0.4.0+。运行环境：Linux、Python 3.12+。
只安装你需要的入口即可，不必装齐所有模型。Python 核心无需第三方包。
本文不包含或要求上传任何 API Key。

GPU／SSH／任务命令联网需求应在派发时通过 requirements 声明；network／host 权限
需要角色明确允许，host 会取消 Codex OS 文件系统隔离，不是默认设置。预检在模型推理前
执行，不代表已取得 GPU 租约或 SSH 登录授权。[执行环境与预检说明](EXECUTION.md)。

0.17.1 区分运行时辅助进程与未完成任务，增加清理后观察，并固化验收冲突报告规则。
固定运行代码、启动诊断及 Codex 计算等待工具从 0.13 起提供。已有用户请先阅读
[运行与排障](RUNTIME.zh-CN.md)。旧 MCP 进程不会热更新；让它们完成现有任务，
再重载插件／重启会话。不要为升级终止仍在运行的实验。

## 1. 先准备可用的后端

- 用 Codex worker：安装 Codex CLI，并在终端完成 **ChatGPT 登录**。本插件不允许
  以 OPENAI_API_KEY 作为 Codex worker 的付费回退。先确认普通 Codex 推理正常。
- 用 Claude worker：安装 Claude Code 并完成终端认证，确认自己的模型／effort可用。
- 用 DeepSeek worker：仍需 Claude CLI（它是 API worker 执行器），再配置独立的
  Anthropic-compatible provider。普通 OpenAI-compatible endpoint 不能直接替代它。
- 用 DSH 作为主控：先按 [DeepSeek Harness 官方说明](https://github.com/deepseek-ai/deepseek-harness)
  安装并初始化一个能正常运行的 profile。本项目不安装 DSH，也不自动开户或登录。

`--version`、模型目录、HTTP 200 都不等于推理可用。先选一个已验证的后端，
用下文的小任务验收后，再逐个加入其他账号。模型 ID 应从实际目录选择。

## 2A. Claude Code 入口（最简单）

终端执行：

```sh
claude plugin marketplace add LianguangXue/delegate
claude plugin install delegate@local-delegation --scope user
```

重启 Claude Code，新会话中执行：

```text
/delegate status
/delegate on
/delegate models codex
```

然后说：

> 使用一个只读 Codex reviewer，从刚才的目录选择支持的模型和 effort。任务只需
> 返回一个固定标记，不改文件。取回结果并检查，再记录验收。

不要直接照抄别人的模型名。若只准备用 Claude worker，让主 agent 使用已验证的
`claude-explorer`，不要为了完成测试临时换账号或启用不认识的 provider。

新安装默认关闭；`on` 仅开启当前会话。想让新会话自动可用，明确对主 agent 说：

> 将 delegate 的新会话默认模式设为 auto，保留现有角色和额度限制。

对应工具是 `delegate_configure(section="defaults", value={"default_mode":"auto"})`。
`/delegate off` 仍可关闭；已保存为 off/manual 的旧会话不随全局默认改变。

## 2B. Codex 入口

先克隆完整仓库，安装入口和技能：

```sh
mkdir -p ~/code
cd ~/code
git clone https://github.com/LianguangXue/delegate.git
cd delegate
python3 scripts/install-hosts.py codex
codex mcp get delegate --json
```

Python 3.12 的命令如果不是 `python3`，请使用本机对应的解释器。
脚本使用 `CODEX_HOME`（未设置则为 `~/.codex`），为配置保存私有备份；只新增
名为 delegate 的 MCP entry 和技能链接。认证及其他 MCP 配置不变。

重新启动 Codex，然后说：

> 使用 delegate：先用 delegate_host 注册当前会话，保留返回的 session_id。
> 在这个会话开启 delegate，只使用我已验证的 Claude explorer。派发一个固定标记
> 的只读任务，取回结果并验收。不要切换账号。

Codex／DSH 没有 Claude 的 `/delegate` 命令解析入口，使用自然语言让主 agent
调用 MCP 工具即可。每个会话单独调用一次 `delegate_host`；重连时保留并复用
原 native_session_id，不要在多个父会话中共享同一个逻辑 ID。

## 2C. DeepSeek Harness（DSH）入口

在上述 clone 的仓库中执行：

```sh
python3 scripts/install-hosts.py dsh --dsh-profile web
```

profile 名称可换成自己**已初始化**的 profile。脚本使用 `DSH_HOME`（未设置则
`~/.dsh`），向该 profile 的 `cordis.patch.yml` 添加带标记的 MCP entry，并安装
用户技能。它需要可用的 `@deepseek-ai/dsh-mcp-client`；已实测的本地 Harness 为
0.2.1-rc.3。后续 Harness 版本若改变配置 schema，应按官方说明核对，不能仅凭
文件已保存就认定工具已加载。

重载／重启 profile 后开新会话，等待 `mcp__delegate__...` 工具发现。可以说：

> 使用 delegate_host 注册本次会话，在当前会话开启 delegate。用已配置的 Codex
> reviewer 完成一个固定标记任务，取回并验收。若失败，报告原因，不修改账号配置。

DSH 主账号与 worker 账号分开。无法确认当前 DSH 账号对应哪个 provider 时，
`parent_provider` 留空，额度显示未知。脚本给 MCP 显式传入原生 Codex／Claude
配置目录路径，避免隔离 HOME 指向空账号；不复制密钥，不关闭沙箱。

`claude-switch harness-run` 等隔离启动器可能使用另一个 DSH_HOME/profile；
它们不会自动继承 web profile 的 MCP entry，需给那个环境添加相同 overlay。
运行工具后的实际返回才是验收。不要在故障时用禁用沙箱、改 HOME 或借用其他
账号来掩盖配置问题。

## 3. 可选：安装 am

在同一个父目录另开一个 checkout：

```sh
cd ~/code
git clone https://github.com/LianguangXue/am.git
cd am
./install.sh
am --version
```

按照 am 自己的文档初始化项目 store。**已有 store 不要重新初始化或加 --force。**
随后建立一个明确任务：

```sh
cd /path/to/project
am init --project my-project --link   # 仅首次；已有 store 跳过
am start example-task
```

让主 agent 使用 `delegate_handoff` 关联这个 am 任务。推荐提供 delegate_task_id，
必要时再提供 work_item，避免把不同目标混在一个会话级交接里。
接手者读取 `am brief --task example-task`，而不是复制全部对话。

看板默认只监听本机：

```sh
am serve
```

默认地址 `http://127.0.0.1:37778/`。远端机器用你自己的安全访问方式；不要直接
照搬别人的内网地址，也不要在未评估访问权限时暴露共享任务数据。

## 4. 可选：DeepSeek／其他 API worker

让主 agent 用 `delegate_configure` 添加 provider 和角色。简化结构示例：

```json
{
  "section": "providers",
  "name": "my-api",
  "value": {
    "kind": "anthropic", "enabled": true, "command": "claude",
    "base_url": "https://YOUR-PROVIDER.example/anthropic",
    "api_key_env": "MY_PROVIDER_API_KEY",
    "models": ["VERIFIED-MODEL-ID"], "efforts": ["low", "medium", "high"]
  }
}
```

以上地址和模型都是占位符，不能直接调用。再添加 provider=`my-api` 的只读角色，
实际验证推理后才考虑 `automatic=true` 和 builder。密钥只在本机终端／密钥管理中
设置，不能发进聊天、提交 Git 或填写成普通配置里的字面量。

主控能够读取环境变量，不代表 MCP 子进程也能读取。Codex 的该 MCP entry 可用
`env_vars = ["MY_PROVIDER_API_KEY"]` 显式传递已设置的变量；DSH 在 MCP entry 的
`env` 中引用 `process.env.MY_PROVIDER_API_KEY`，不要写字面量。原有 Bridge 用户
也可通过私有 snapshot 引用接入，见[账号说明](../plugins/delegate/skills/delegate/references/accounts.md)。
官方和公司账号应保持独立；metadata 失败不代表推理失败，也不能自动换账号。

## 5. 首次验收与常见问题

| 现象 | 检查顺序 |
| --- | --- |
| 看不到工具 | 重启主控，核对 `codex mcp get delegate`／DSH 实际组合配置，确认技能可读 |
| 默认不派发 | 新安装默认 off；明确开启当前会话或配置新会话默认 auto |
| 账号／模型错误 | 在原 CLI 单独验证；检查 provider/角色白名单，勿用目录返回代替推理验收 |
| DSH 中 Codex 启动断开 | 检查 MCP 显式 CODEX_HOME、目录读写和认证；不要关闭沙箱 |
| 接收不到完成提醒 | Codex／DSH 无 Claude hook；用 result wait 或宿主支持的后台 waiter |
| 不可提交 | 需要 builder、干净隔离工作树，以及每次明确 allow_commit/message/paths |
| 超时 | 长任务明确 timeout_seconds=0；旧版运行中的 worker 不能被热升级 |
| am 无关联摘要 | 先绑定任务；本机 delegate 概览与已导入 am 交接是两种数据 |

提交 hash、任务完成、读到结果和验收通过是不同状态。模型仍会消耗自己的
额度／API 资源；总成本还包含主控协调，原生父用量暂未完整计量。

## 6. 升级与撤销

- Claude 入口：`claude plugin update delegate@local-delegation`，重启主控；若本地
  marketplace 未刷新，先通过 Claude 的 marketplace 更新功能刷新仓库。
- Codex／DSH：在保留的 clone 中 `git pull --ff-only` 后重启／重载宿主。launcher 和
  skill 链接依赖这个路径，不能随意删除或搬走。
- 安装器是首次安装入口，已有同名 server/overlay 会拒绝覆盖。更换路径前先核对
  当前条目，再移除本项目创建的条目；不要重复追加配置。
- Codex 可用 `codex mcp remove delegate` 移除 server；技能只删除确认属于本项目的
  符号链接。DSH 只移除标记为 delegate-host-mcp 的配置项。
- 不要直接拿旧整份备份覆盖正在使用的账号配置；期间可能已有其他合法更新。
- 撤销入口不会删除任务、对话归档和工作树；清理这些内容要另行确认范围。

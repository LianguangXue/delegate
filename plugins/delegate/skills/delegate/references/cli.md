# 当前 CLI、脏仓库与完整结果（0.23）

当已连接 MCP 太旧时，可以继续使用同一个 session_id，通过当前 CLI 调用新功能。
不用把任务复制到 scratch repo，也不用更换 work_item 绕开已有任务状态。

## 先确认帮助和能力

```sh
delegate-agent --help
delegate-agent --version
delegate-agent doctor
delegate-agent schema delegate_run
delegate-agent call --help
```

无参数显示帮助。空／错误 JSON 返回简短错误和用法，不输出 Python stack trace。
doctor 不探测模型或账号；它只报告当前 CLI，**不能证明已连接 MCP 的版本**。
MCP 的实际版本需在那个连接中调用 delegate_control status 查看 server_runtime.version。
新安装不会替换旧连接。混版本时，应在该会话中所有 run／result／verify 都走当前 CLI，
或重载／新开新版 MCP 会话。不要一半用新 CLI、一半继续用旧工具回显长报告。

## JSON 输入格式

外层必须是 name 和 arguments：

```sh
delegate-agent call <<'JSON'
{"name":"delegate_job","arguments":{"session_id":"SID","action":"status","job_id":"JOB"}}
JSON
```

也可把同样 JSON 写入 request.json，再执行：

```sh
delegate-agent call --input request.json
```

任务文本使用 JSON 文件或带引号 heredoc，避免拼接 shell 字符串。不要把 Key 放进请求。
Codex／DSH 原生主会话使用 call codex／call dsh 和原逻辑 session_id；便利命令则使用
--host codex／--host dsh。普通 Claude 会话无需 --host。

## 脏仓库的 builder

以下是 request.json 示例，替换实际目录、任务、验收和 SID；已有 auto 授权时使用：

```json
{
  "name":"delegate_run",
  "arguments":{
    "session_id":"SID",
    "agent":"codex-builder", "model":"auto", "effort":"auto",
    "cwd":"/path/to/repo", "operation":"write",
    "source_inputs":["README.md","doc/PLAN.md"],
    "required_files":["README.md","doc/PLAN.md"],
    "task":"同步这两份草稿，保留其他 session 的改动",
    "acceptance":"规则与引用一致；矛盾项明确报告，不弱化验收",
    "rationale":"选定草稿进入隔离 worktree 后修改",
    "notify":true
  }
}
```

off／manual 下只有用户明确授权一次任务时，才额外使用 one_shot=true。
source_inputs 是 Git 根目录相对的精确文件，最多64个，单个1MiB、共8MiB。
它把选定的暂存／未暂存／未跟踪草稿或已跟踪删除带入 worktree，不改变源目录的
HEAD、index、文件或其他 session 的改动。required_files 则相对实际 worker cwd。
使用子目录 cwd 时注意两种相对根不同。更多见仓库 docs/DRAFTS.md。

如果任务只是分析并生成报告，用 reviewer 返回文本，再导出完整报告，不必创建源码
builder。确实修改源码时才明确选择所需草稿；不要为了分发而自动 stash、commit、
丢弃文件或复制整个 Research／data 目录。输出仍需主 agent 审核整合。
旧 MCP 0.17.x 不支持 source_inputs；通过当前 CLI 调用才会使用新实现。

## 完整报告：一条命令，不读私有 job JSON

```sh
delegate-agent result JOB --session-id SID --output /tmp/review.md
```

完整终态文本写入 UTF-8 文件，返回路径、字符数、SHA-256 和收取状态，不回显整份报告。
同内容文件可重复使用；不同内容已有文件不覆盖，除非显式 --force。发布／flush 失败
不标记完整收取。导出意味着产物交到父会话可访问的位置，不意味着模型已读完或验收通过。
仍须查看文件和证据后再 verify。

```sh
delegate-agent verify JOB --session-id SID --verdict accepted --evidence "已检查全文和对应产物"
```

长证据可以用 --evidence-file。verify 默认只回简短确认，不再附带报告正文。
显式需要完整 stdout 时用 result … --all；宿主显示仍可能截断，长报告优先 --output。

父任务仍需继续时，可以批量记录已检查的子任务验收，不必 finish 父任务：

```sh
delegate-agent call <<'JSON'
{"name":"delegate_task","arguments":{"session_id":"SID","action":"update","task_id":"TASK","accepted_jobs":["JOB"],"evidence":"已检查完整报告与验证证据"}}
JSON
```

不传分类字段，它们在分发后已冻结。批量验收只接受同一 task/session 的成功终态任务；
先导出或完整收取报告，验收本身不补记未读正文。重试保留已有的详细验收证据。

## MCP 的按需结果视图

- result_view="auto"：首次返回正文页；相同文本已经返回过时只回简短 receipt，提供续取／重读入口。
- result_view="page"：显式读取正文，包括重读第一页；使用 next_result_call 中的指纹和 offset。
- result_view="summary"：只看元数据，不新增正文交付／收取标记。
- result_view="file"：全文发布为私有 Markdown 产物，返回路径和哈希，不受正文分页截断影响。

file 视图由父会话请求发布报告副本，不赋予只读 worker 额外写权限，也不改写源码。

新 MCP 的普通调用：

```text
delegate_job(session_id=SID, action="result", job_id=JOB, result_view="file")
```

选择分页时，执行所有非空 next_result_call；不要自己计算字节 offset。
full_result_call 可直接获得全文文件；reread_result_call 用于重新查看正文。
没有实际收到前一页时应显式重读，不把 receipt 当作正文。

更新 hook 后，支持输出替换的 Claude 宿主还能压缩旧 MCP 的 verify／已确认重复结果。
它不修改原报告、验收或首次新版结果页，且不把 hook 版本伪装成旧 MCP 的版本。
只更新安装目录无法更新已载入的旧 hook；当前 CLI 是不依赖该兼容功能的替代路径。

等待／通知、收取和验收是不同操作。不要用 result 反复查询来等待长任务；用
delegate-agent wait 或任务完成通知，然后再导出或读取结果。MCP 客户端必须显式使用
mcp／host-mcp，裸 delegate-agent 是用户 CLI，裸 Python entry 的历史 stdio 接口仍兼容。

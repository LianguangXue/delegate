# CLI 与混版本迁移

完整指南随插件打包，避免安装后找不到输入格式：

[当前 CLI、脏仓库与完整结果](../plugins/delegate/skills/delegate/references/cli.md)。

最短路径：

```sh
delegate-agent --help
delegate-agent doctor
delegate-agent result JOB --session-id SID --output /tmp/review.md
delegate-agent verify JOB --session-id SID --verdict accepted --evidence "已检查全文和证据"
```

通用 JSON 请求使用 delegate-agent call 的 name／arguments envelope，可从 stdin 或
--input 文件输入。原 session_id 保持不变；旧 MCP 未更换时所有操作统一走当前 CLI。

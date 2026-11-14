# GC 范围与混版本运行（0.25.1）

先通过当前 CLI 查看清单：

```sh
delegate-agent --version
delegate-agent gc --retention-days 14
```

默认只盘点；`delegate_control(action="gc")` 也只盘点。只有明确要求永久删除时，
才使用 CLI 的 `--apply`。更新安装不自动执行 GC，不应为清理而终止正在运行的任务。

0.25.1 保留根目录及 locks/ 下的协调锁文件。空闲 flock 只是点时状态；删除文件
会让等待者与新调用者锁住不同 inode。新调用同时协调两个路径，与0.24及以前、0.25.0
的不可变运行时兼容。已在运行的两个旧版本之间无法靠更新磁盘代码补上互斥；
它们的任务结束后应重启连接，让新的调用进入双路径协调。

冻结 runtimes 不再自动删除。暂停、已完成但可扩展的 loop，以及旧 MCP 后续启动的
worker 都可能依赖它们；仅凭一天以上的 mtime 或当前没有 job 引用不能证明可删。
`counts.runtimes_retained` 表示保守保留的旧候选，不计入 reclaimable_bytes；
`counts.locks`、`counts.runtimes` 为0。所有保留的 loop 还会保护其子任务记录，
保证原会话恢复和后续阶段仍有依据。

GC 仍可按原保留期清理符合条件的终态任务、关联诊断文件、过期计划和无待提交登记的
旧 WAL。活动任务、必需而未收取的结果、未关闭父任务及有比较依据的任务继续保留。
0.25.0 已加载进程中的旧 GC 不会热更新；清理操作统一走 `delegate-agent --version`
确认为0.25.1或更新版本的 CLI。

通知索引使用 pending_jobs_seen 记录已检查的 job ID。旧服务向 jobs 追加新任务时，
新 hook 会增量补入 pending_jobs；正常稳定状态不用反复读取已经收取的历史 job。
升级不删除历史结果，也不把通知、全文收取和验收混为一谈。

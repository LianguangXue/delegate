# Complete results and completion notifications (0.23)

For long reports, request `result_view="file"` or use
`delegate-agent result JOB --session-id SID --output REPORT.md`. This publishes the
entire plain Markdown report and returns only its path/hash/count; no private job-JSON
parsing is needed. Export is collection of an artifact, not proof the parent read or
accepted it. See the [bundled CLI guide](../plugins/delegate/skills/delegate/references/cli.md).

Verify never echoes report text. Default `result_view="auto"` returns new text once,
then compact receipts with continuation and reread handles when text was already
returned. `page` explicitly rereads; `summary` is metadata-only and does not collect.

To record already-inspected successful children without closing their parent task, use
`delegate_task(action="update", task_id=..., accepted_jobs=[...], evidence=...)`.
Classification fields omitted from an update stay unchanged; changing classification
after dispatch is rejected. The entire batch must belong to the same task/session and
be successfully completed. Existing accepted evidence is preserved on retries.
This records reviews, not delivery of unseen text. `finish` still closes the parent.

The 0.23.2 collection hook also recognizes exact full legacy `verify` responses,
including failure diagnostics. It leaves a first full legacy body visible before
acknowledging it; an already-delivered body's later echo can be compacted. A verdict,
metadata-only receipt or truncated preview never substitutes for the report itself.

## Read every finding without opening private job JSON

`delegate_job(action="result", session_id=..., job_id=...)` no longer silently cuts
ordinary reports at 8,000 characters. It requests up to 24,000 characters by default,
within a 32,000-byte UTF-8 JSON budget including metadata and escaping. Longer reports
have explicit pages, not a sliced JSON document.

If `next_result_call` is present, issue that exact tool request. It includes the next
character offset and the SHA-256 of the full text. Continue until
`result_delivery_complete=true`; merely reaching the last page out of order does not
establish complete delivery. Pages preserve every character, including Unicode and
newlines. A changed fingerprint requires restarting at offset zero.

Optional `result_limit` accepts 1..32000 requested characters. The byte budget can
reduce that count; use the returned offset rather than calculating one. `detail=true`
includes more metadata when it fits but does not bypass text pagination. Large
metadata is explicitly omitted/shortened, with diagnostics retained in the job record.
Hosts can impose smaller output limits; choose a smaller page size if needed.

`result_truncated=true` now explicitly means there is a following text page. Results
are marked collected only when all ranges of the current terminal text have been
returned or the complete report artifact has been published by an explicit request.
A preview or background notification does not acknowledge the missing tail.
Collection is still separate from checking evidence and accepting work.

## Let a required job finish without model polling

For a new long task, use the normal dispatch arguments plus:

```text
delegate_run(..., notify=true)
```

This returns immediately and keeps `await_completion=true`. It differs from
`background=true`, which explicitly opts out of required collection. To arm the same
behavior for an existing job:

```text
delegate_job(session_id=..., action="watch", job_id=...)
```

In Claude Code, the PostToolUse asyncRewake hook monitors the owning job. Its actual
process identity and heartbeat are registered. In a recognized interactive/persistent
streaming session, Stop can pause the current turn while that watcher is alive; the
task remains open and the result remains required. Completion emits the stable notice
that asks the main agent to collect and review all pages. Uncollected notices can replay;
their event ID does not mean the output has been accepted.

The receipt's `completion` field exposes `watcher_active`, `stop_may_defer`, the waiting
command and stdin payload. A receipt may precede hook startup, so `watcher_active=false`
does not claim notification is already armed. A flag alone never suppresses Stop.

If the host does not start the hook, or dispatch used the CLI instead of an MCP call,
run the returned waiter through the host's **background-command facility**:

```sh
delegate-agent wait <<'JSON'
{"session_id":"the-owning-session","job_id":"the-job-id","background":true}
JSON
```

Codex/DSH native parents use `delegate-agent wait codex` / `wait dsh` with their same
logical session ID. The script can wait beyond 50 seconds in code, heartbeats while
waiting, and returns a completion notice containing `next_result_call`. It does not
fetch/collect the report. A plain shell `&` is not a host notification integration.

Once the notice arrives, call its `next_result_call` and collect remaining pages.
`action="status"` inspects a job without collecting its output. Do not spawn another
model merely to poll, or repeatedly run 50-second result calls for a multi-minute task.

The 50-second MCP wait remains bounded to avoid occupying a fragile long-lived tool
request. For a synchronous/headless host, run the managed waiter in the foreground
(omit `background`), wait for its one completion response, then collect results.
One-shot `claude -p` and unknown parent modes keep the Stop guard; a foreground waiter
can still wait in code without repeated model inference.

## Delivery boundaries

Stop continues to request collection for terminal jobs and when a watcher is missing,
stale, dead or has a recycled PID. Even after the reminder's loop guard is exhausted,
an undelivered required job does not make its task complete. A completed review remains
pending acceptance until the parent checks its evidence.

Automatic wake depends on a running, supporting host. Closing the session cannot wake
it later; completion notices remain durable for the next interaction/session. No
delegate job is killed or automatically rerun by notification bookkeeping. Existing
MCP servers and hooks retain their loaded versions: reload/start a new session, or use
the current fixed CLI with the same session ID. Old partial-result markers are repaired
by the new collection hook when it observes the truncated response; old hooks cannot
gain this behavior retroactively.

Claude documents [asyncRewake notifications and headless teardown limits](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background).

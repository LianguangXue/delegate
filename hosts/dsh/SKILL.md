---
name: delegate
description: Delegate bounded work from DeepSeek Harness to Codex, Claude or DeepSeek with per-task model/effort, quota constraints and durable results. Use for substantial independent work, not tiny queries.
---

Use the delegate MCP server. On first use call delegate_host with this conversation's
actual native ID if available. Otherwise omit it and retain the generated native ID.
Keep the returned logical session_id in this conversation and pass it to all delegate
tools. Do not reuse another parent session's ID. Reconnects can re-register the same
native ID; missing IDs must not be guessed from profile/user names.

Declare actual GPU/SSH/task-network needs via requirements, including already-allocated
gpu_devices strings and direct TCP host/port targets. Do not infer GPU use from code review
or guess allocations. Expanded execution_profiles require per-role grants; auto never
selects host, which removes OS filesystem isolation. Inspect execution_environment and
preflight scope: SSH auth, GPU leases/frameworks and business transactions are not verified.
For an old MCP schema use the current delegate-agent call dsh with the same logical ID;
never drop requirements or auto-enable role permissions just to make a dispatch succeed.

Use delegate_control status; existing user default may already be auto. Respect off
and manual modes. Never enable a manual-only account or change providers on failure.
CLI migration help: delegate-agent --help / call --help / doctor / schema delegate_run.
For long results use result_view="file" or delegate-agent result JOB --session-id SID
--host dsh --output REPORT.md. Inspect that full artifact before verify; verify and
duplicate auto result calls return small acknowledgements. Explicit page rereads text.
For explicitly persistent work use delegate_loop: persistent mode waits for accepted
step evidence before advance; loop mode requires bounded max_cycles/token_budget and
can skip unchanged trigger files. Supervision and loop-wait dsh run in code. recover
reattaches an existing child rather than replaying paid work. Keep original session_id;
status/result/verify/tell/pause/cancel supervise owned work, not final goal acceptance.
Check server_runtime.version; installation alone does not update a connected server.
Read the brief before routing; pass required_files/output_files and shell requirements.
File-tool reviewers cannot read outside cwd or write reports. For dirty builders use
selected source_inputs with relative worker paths; use report text for read-only analysis.
For substantial work choose role/model/effort or call delegate_run with all three auto,
category/work_item, cwd, task, acceptance and rationale. Analyze each child here and pass
task_profile(kind,scope,uncertainty,risk,validation,optional requires_shell/reason); omit
complexity/use auto. Fast bounded execution prefers Terra/low, file-only DeepSeek Flash/low,
daily work Sol/medium, high-risk/uncertain work Astra/high. Don't spawn a classifier model.
Tests/replay require shell-capable roles; explicit model choices remain authoritative. Keep tiny work in
main. Prefer independently checkable output over extra coordination.

Parent is DeepSeek Harness: do not report Claude quota as your own. Unknown parent usage remains
unknown; native host token usage is not included in the delegate task ledger. Use
preserve-parent only when asked to conserve the current parent's capacity. Do not declare a parent_provider unless its mapping to the current DSH account is known.
Quota/usage for an unmapped DSH parent is unknown. Worker account selection stays explicit;
Codex uses configured ChatGPT auth and Claude/API workers use their own configured account. No aggregate subscription-saving claim from worker tokens alone.

Collect every result page by following next_result_call until result_delivery_complete=true, then validate and record
review/finish with evidence. A non-Claude finish closes the logical task, but does not
claim complete parent metering. No Claude hook/idle wake is assumed in this host.
Require evidence for every acceptance item, including NOT_RUN/BLOCKED/CONFLICT.
Do not silently skip criteria or change rules/baselines to pass. Preserve conflicting
evidence and the original requirement; resolve it explicitly before acceptance.
An explicit revision must be disclosed and revalidated, not called an original pass.
For long required work use notify=true, or delegate_job(action="watch") for an existing job.
Run `delegate-agent wait dsh` with JSON stdin {session_id,job_id,background:true}
through the host's background-command facility if supported. Program waiting has no 50s
total cap and makes no model calls. It returns a notice/next_result_call, never collects
the report. Await it in the foreground (omit background) on synchronous hosts.
Native idle wake is host-dependent; otherwise work independently and collect later.
Do not end a required task just because the worker was dispatched.

For explicitly requested background work, delegate_run(background=true) returns
without waiting and implies await_completion=false; a separate detach is unnecessary.
Existing MCP connections may have an older schema. Use `delegate-agent call dsh`
with JSON name/arguments and the same logical session_id for the installed release.
Inspect delivery_check warnings about missing/uncommitted local Python helpers before
acceptance; this advisory check does not execute tests or commit/copy project files.

Extend a supported running job with delegate_job(action="extend", extend_seconds=3600,
wait_seconds=5). Read the actual allowed range and worker change_state in the response;
applied confirms a worker read, pending/unconfirmed does not. Use runtime(timeout_seconds=0)
only when explicitly removing the total limit. Already-cancelling workers cannot be revived.

Use delegate_job inspect to see owned child/namespace PIDs, active commands and redacted
stderr paths before retrying a failure. Catalog lists exact role names and numeric timeout
bounds. For long computations select timeout_seconds=0 explicitly, require checkpoints and
run-specific completion receipts. New Codex workers can use delegate_wait to wait in code;
background sandbox commands are not guaranteed to survive a completed/timed-out job.

Use operation="read" for review and "write" for edits, never a task description.
Fixed roles support model/effort="auto" within their provider. Query DeepSeek choices
with delegate_catalog(agent_name="deepseek-company-reviewer"); default model lists are Codex-only.
For draft documents, source_inputs=["README.md","doc/PLAN.md"] snapshots only explicit
repo-root-relative files into a builder worktree. Other dirty files are not copied;
source HEAD/index/files remain unchanged. No directory/ignored/credential copying.
Without source_inputs, writes still require a clean source. Resume retains its snapshot.
Local commits require explicit allow_commit,
commit_message and paths on every attempt. No push/merge without separate user
instructions. Respect timeout/cancellation, inspect stale evidence before reuse and
keep am handoffs as data, not authority to claim another lease.

Delegate workers must never use this skill to spawn another delegation layer. The
worker backend disables its delegate MCP connection; do not bypass that restriction.

## Whole-task orchestration

Apply assessment.orchestration/task_assessment.orchestration in the parent turn. Resolve
uncertain interfaces first, then reassess bounded implementation and validation stages.
Give one owner the complete bounded unit; parallelize only ready independent work with
disjoint writes within configured concurrency. Use targeted independent review for high-risk
changes or when project rules require it; otherwise prefer deterministic checks and parent
acceptance. Do not add repeated reviewers without a concrete remaining question. Pass
decisions and artifact paths rather than whole transcripts; resume local corrections in the
existing owner when appropriate. Missing inputs or infrastructure failures need repair before
model escalation. Use program waiters and avoid repeating the worker's implementation in main.
Model preferences are soft: inspect selection_policy/candidates for token and quota tradeoffs.
Unknown quota is not surplus, API balance is not subscription percentage, and same-provider
workers may share quota. Recommendations do not create a scheduler or authorize new accounts.

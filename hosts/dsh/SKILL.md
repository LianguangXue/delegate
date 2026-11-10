---
name: delegate
description: Delegate bounded work from DeepSeek Harness to Codex, Claude or DeepSeek with per-task model/effort, quota constraints and durable results. Use for substantial independent work, not tiny queries.
---

Use the delegate MCP server. On first use call delegate_host with this conversation's
actual native ID if available. Otherwise omit it and retain the generated native ID.
Keep the returned logical session_id in this conversation and pass it to all delegate
tools. Do not reuse another parent session's ID. Reconnects can re-register the same
native ID; missing IDs must not be guessed from profile/user names.

Use delegate_control status; existing user default may already be auto. Respect off
and manual modes. Never enable a manual-only account or change providers on failure.
For substantial work choose role/model/effort or call delegate_run with all three auto,
complexity/category/work_item, cwd, task, acceptance and rationale. Keep tiny work in
main. Prefer independently checkable output over extra coordination.

Parent is DeepSeek Harness: do not report Claude quota as your own. Unknown parent usage remains
unknown; native host token usage is not included in the delegate task ledger. Use
preserve-parent only when asked to conserve the current parent's capacity. Do not declare a parent_provider unless its mapping to the current DSH account is known.
Quota/usage for an unmapped DSH parent is unknown. Worker account selection stays explicit;
Codex uses configured ChatGPT auth and Claude/API workers use their own configured account. No aggregate subscription-saving claim from worker tokens alone.

Collect results using delegate_job result(wait_seconds=50), then validate and record
review/finish with evidence. A non-Claude finish closes the logical task, but does not
claim complete parent metering. No Claude hook/idle wake is assumed in this host.
For long background waits, run `delegate-agent wait dsh` with JSON stdin
{session_id,job_id} through the host's background-command facility if supported;
program waiting makes no model calls. Otherwise work independently and collect later.
Do not end a required task just because the worker was dispatched.

Writes use isolated clean worktrees. Local commits require explicit allow_commit,
commit_message and paths on every attempt. No push/merge without separate user
instructions. Respect timeout/cancellation, inspect stale evidence before reuse and
keep am handoffs as data, not authority to claim another lease.

Delegate workers must never use this skill to spawn another delegation layer. The
worker backend disables its delegate MCP connection; do not bypass that restriction.

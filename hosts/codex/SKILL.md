---
name: delegate
description: Delegate bounded work from Codex to Claude, Antigravity/Gemini, DeepSeek or Codex with zero-token advisory routing, per-task model/effort, quota constraints and durable results. Use for substantial independent work, not tiny queries.
---

Use the delegate MCP server. On first use call delegate_host with this conversation's
actual native ID if available. Otherwise omit it and retain the generated native ID.
Keep the returned logical session_id in this conversation and pass it to all delegate
tools. Do not reuse another parent session's ID. Reconnects can re-register the same
native ID; missing IDs must not be guessed from profile/user names.

## 4×4 Cross-Host Matrix & Zero-Token Dispatch Rules (0.28.0)

Fast CLI one-liners across all 4 hosts (`claude`, `codex`, `antigravity`/`agy`, `dsh`):
- `delegate-agent advise "task" --host codex [--workflow dual-plan|copy-ux|frontend|gate-review|bulk-cheap] [--lines 30]`
- `delegate-agent ask <agy|claude-build|claude-review|deepseek|codex-review> "task" --host codex [--blind] [--review-gate]`

Apply zero-token Khazix-style routing before spawning a worker:
- **Inline gate (`estimated_lines<=40`)**: keep <=40-line edits, review fixes, and backend/algo tasks where parent Codex is already the specialist inline.
- **Blind second opinion (`workflow="dual-plan"`, `blind_context=true` / `--blind`)**: on major architecture plans, ask `agy-advisor` (`antigravity` read-only) with ONLY raw user intent + verified code facts (no parent draft); verify `[speculative]` items against code.
- **UX/copy & visual frontend (`workflow="copy-ux"` / `"frontend"`)**: use `agy-advisor` to humanize stiff UI copy/interactions, and `claude-builder` (`sonnet`) for visual frontend craft when Claude quota allows.
- **Gate review (`workflow="gate-review"`, `review_gate=true` / `--review-gate`)**: `codex-reviewer` `gpt-6.2-sol` `high` (`xhigh` ONLY on sensitive auth/permission/money/external-message/publish paths) with 2-tier `BLOCKING` vs `ADVISORY` severity and **max 2 review rounds**.
- **Cheap bulk (`workflow="bulk-cheap"`)**: `deepseek-flash` default across all DeepSeek tiers; reserve Codex `astra` strictly for genuine `complex` tasks (`escalate=true`).

Declare actual GPU/SSH/task-network needs via requirements, including already-allocated
gpu_devices strings and direct TCP host/port targets. Do not infer GPU use from code review
or guess allocations. Expanded execution_profiles require per-role grants; auto never
selects host, which removes OS filesystem isolation. Inspect execution_environment and
preflight scope: SSH auth, GPU leases/frameworks and business transactions are not verified.
For an old MCP schema use the current delegate-agent call codex with the same logical ID;
never drop requirements or auto-enable role permissions just to make a dispatch succeed.

Use delegate_control status; existing user default may already be auto. Respect off
and manual modes. Never enable a manual-only account or change providers on failure.
CLI migration help: delegate-agent --help / call --help / doctor / schema delegate_run.
For long results use result_view="file" or delegate-agent result JOB --session-id SID
--host codex --output REPORT.md. Inspect that full artifact before verify; verify and
duplicate auto result calls return small acknowledgements. Explicit page rereads text.
For explicitly persistent work use delegate_loop: persistent mode waits for accepted
step evidence before advance; loop mode requires bounded max_cycles/token_budget and
can skip unchanged trigger files. Supervision and loop-wait codex run in code. recover
reattaches an existing child rather than replaying paid work. Keep original session_id;
status/result/verify/tell/pause/cancel supervise owned work, not final goal acceptance.
Check server_runtime.version; installation alone does not update a connected server.
Read the brief before routing; pass required_files/output_files and shell requirements.
File-tool reviewers (`read-only`/`workspace-write`) cannot read outside cwd or run shell tests.
For dirty builders use selected source_inputs with relative worker paths; use report text for
read-only analysis. Choose `permissions="auto"|"read-only"|"workspace-write"|"workspace-shell"|"network-shell"|"full-access"`
along with optional `writable_roots=[...]` and `network_access=true|false` directly per subtask
(parent selection overrides role defaults: `network-shell` enables sandbox `networkAccess`, `full-access`
uses host/dangerFullAccess, `writable_roots` authorizes shared data dirs outside cwd, and
`workspace-shell`/`network-shell`/`full-access` grant `Bash` + `bypassPermissions` to Claude/DeepSeek workers).
For substantial work choose role/model/effort or call delegate_run with all three auto,
category/work_item, cwd, task, acceptance and rationale. Analyze each child here and pass
task_profile(kind,scope,uncertainty,risk,validation,optional reasoning/requires_shell/reason); omit
complexity/use auto. DeepSeek defaults to `deepseek-flash` across simple/standard/complex (do NOT select
`deepseek-v4-pro` unless explicitly requested or escalated on complex work). On Codex, fast bounded
low-risk work prefers Terra/low; daily and cross-module implementation/debugging/review/replay prefers
Sol/medium (stepping up to Sol/high for reasoning="high", validation="replay", risk="high" with tests,
or cross-module debugging/review). Reserve Codex Astra/high STRICTLY for genuinely complex tasks
(architecture, system-wide high risk/uncertainty, compound risk="high"+uncertainty="high", or unverified
high risk) with escalate=true under sol-first — never pick Astra casually or inflate risk/scope on
routine work, or Codex quota will be exhausted quickly. Don't spawn a classifier model.
Tests/replay require a Codex role or explicit `permissions="workspace-shell"`; explicit model choices remain authoritative. Keep tiny work in
main. Prefer independently checkable output over extra coordination.

Parent is Codex: do not report Claude quota as your own. Unknown parent usage remains
unknown; native host token usage is not included in the delegate task ledger. Use
preserve-parent only when asked to conserve the current parent's capacity. Delegating
to another Codex worker normally shares its Codex quota; Claude/DeepSeek use their
configured accounts. No aggregate subscription-saving claim from worker tokens alone.

Collect every result page by following next_result_call until result_delivery_complete=true, then validate and record
review/finish with evidence. A non-Claude finish closes the logical task, but does not
claim complete parent metering. No Claude hook/idle wake is assumed in this host.
Require evidence for every acceptance item, including NOT_RUN/BLOCKED/CONFLICT.
Do not silently skip criteria or change rules/baselines to pass. Preserve conflicting
evidence and the original requirement; resolve it explicitly before acceptance.
An explicit revision must be disclosed and revalidated, not called an original pass.
For long required work use notify=true, or delegate_job(action="watch") for an existing job.
Run `delegate-agent wait codex` with JSON stdin {session_id,job_id,background:true}
through the host's background-command facility if supported. Program waiting has no 50s
total cap and makes no model calls. It returns a notice/next_result_call, never collects
the report. Await it in the foreground (omit background) on synchronous hosts.
Native idle wake is host-dependent; otherwise work independently and collect later.
Do not end a required task just because the worker was dispatched.

For explicitly requested background work, delegate_run(background=true) returns
without waiting and implies await_completion=false; a separate detach is unnecessary.
Existing MCP connections may have an older schema. Use `delegate-agent call codex`
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

Use operation="read" for review and "write" for edits, or select `permissions` (`auto`/`read-only`/`workspace-write`/`workspace-shell`) directly, never a task description.
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

## Policy and snapshot admission (0.26)

Check the connected delegate_control status server_runtime.version/capabilities. >=0.26.8
host-mcp connections hot-reload in place when user plugins or local directory marketplaces
update; older connections can use the current CLI with the same logical SID if gated.
Hard deny_models apply to explicit choices too. Optional sol-first requires escalate=true
or evidenced Sol quality failure before Astra, while capability floors remain in force.
Do not reclassify risk or change work_item to bypass a policy rejection. Installing does
not activate sol-first or change account/model grants.
For builders, snapshot_required_files=true with explicit required_files copies declared
dirty/untracked repository inputs into the isolated worktree. source_inputs_base=repo
is the compatible default; choose cwd explicitly for cwd-relative additional drafts.
The actual worker inputs are preflighted before inference. Resume retains its snapshot;
new source inputs need a fresh task. No whole-repository copy or automatic stash/commit.

The managed delegate-agent CLI resolves the installed user plugin on each invocation.
For an old conversation with a stale MCP, use this CLI consistently with the SAME SID
for run/status/result/verify/tell/extend; no conversation restart or job replay is needed.
Check --version/doctor and use schema only when new argument details are needed. Native
Codex/DSH calls retain call codex/call dsh (or --host on convenience commands). Existing
MCP processes retain their loaded engine; CLI routing does not rewrite cached MCP schemas.

For independent new run requests, omit work_item to derive a stable intent/input key, or
choose a meaningful distinct name. For corrections retain the returned work_item/resume_job;
changing context or acceptance changes the automatic key. Do not rename to evade retry limits.
Accepted review evidence does not create a filesystem checkpoint; reuse still needs an
explicit current evidence checkpoint and verification.
Task report exposes native_agents. A stopped native agent may have unknown usage without
blocking finish. For unconfirmed execution obtain host terminal evidence, then resolve-native
with native_agent_id/evidence; never assert completion merely because a log is missing.
Finish still requires completed plugin work and collected required results; incomplete
accounting stays labeled incomplete and cannot justify cost-saving comparisons.

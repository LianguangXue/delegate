---
name: delegate
description: "Control optional multi-provider subagents: on/off/manual, single tasks, task usage reports, model/effort planning, accounts, quota and resumable jobs."
disable-model-invocation: true
argument-hint: "on | off | status | run <agent> <task> | report | plan | compare <case-key> | agents | providers"
---

Session: `${CLAUDE_SESSION_ID}`. User operation: $ARGUMENTS

Use the delegate MCP tools with this exact session ID. If unavailable, report that and invoke
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py" call` with JSON (`name`, `arguments`) on stdin via
quoted heredoc. Never interpolate task text or credentials into shell code.

## Controls

- `on`, `off`, `mode manual`, `status`: delegate_control. Only explicit user requests change mode.
  `on` is session-scoped; optional agents set the pool, strategy is balanced/preserve-claude/quality-first.
  Off leaves running jobs intact. New sessions default off; role/provider configuration persists separately.
- `run <role> <task>`: explicit single-task authorization; use delegate_run(one_shot=true), without changing mode.
- `models [provider]`: delegate_catalog. Models and efforts must be supported; roles can narrow the allowlist.
- `quota`: delegate_quota for enabled providers, passing session_id. `budget`: delegate_budget for routing evidence.
  `budget claude <percent>` records ONLY an explicitly user-supplied remaining percentage, labelled and expiring.
- `agents` / `providers`: delegate_configure(show). Before adding/editing, read [accounts](references/accounts.md).
- `jobs`, `result <id>`, `tell <id> <instruction>`, `cancel <id>`, `archive <id>`: delegate_job.
  Tell supports live Codex steering; other providers receive follow-up through resume after completion.
- `resume <id> <task>`: inspect the prior result, then delegate_run(resume_job=id) with the same role/source_cwd.
  Preserve work_item within a task. Do not start conflicting work until cancellation/cleanup is confirmed.
- `task list`, `report <task-id>`: delegate_task. `compare <case-key>` compares complete accepted equivalent cases.
  Optional reference_task_id compares against a specific recorded run without rewriting its history.

## Shared task handoff (optional)

Keep delegate and am independently managed. To associate this entire Claude session's
work with an existing am task, call delegate_handoff(action="bind", am_task_id=...).
Omit am_root to resolve it with `am where`. Never claim an am lease or mark its task done
merely because a worker ended. Use a new Claude session for a different am task.

- `link <am-task-id>`: delegate_handoff bind; task must already exist in am.
- `handoff`: delegate_handoff report, including sync status and bounded snapshot.
- `handoff next <step>`: delegate_handoff next; record the verified next step, never execute it automatically.
- `handoff sync`: retry delivery after am becomes available.

Bound sessions export on worker completion and relevant parent hooks. Snapshots contain
work-item/attempt IDs, acceptance, status/review, evidence and artifact/archive paths.
A pending sync stays durable for retry. Old snapshots cannot overwrite newer ones.
The successor uses `am brief --task <id>` and `am delegate report --task <id>`.
These are context/evidence, not authorization to steal a lease or repeat external actions.
Completion notices carry a stable ID and replay on session start until results are
collected; collection is separate from acceptance. Avoid launching replacement work
merely because a repeated completion notice arrived.

## Efficient work

Automatic mode is optional. Do not add an agent to a trivial task merely because another account has quota.
Answer trivial tasks directly without planning or measurement tool calls. Local hooks still observe usage.
For substantial independent work use ONE call:

`delegate_run(agent="auto", model="auto", effort="auto", category=..., complexity=..., operation="read"|"write",
work_item=..., wait_seconds=50, task=..., acceptance=..., rationale=..., cwd=..., context=...)`

Pass complexity and category for EACH work_item. They classify that subtask, not the whole parent task.
This classifies, plans, dispatches and waits. It may return main/wait/reuse/replan without starting a worker;
that is a decision, not task completion. Use separate delegate_plan only when examining choices is useful.
Explicit user model choices can use the explicit role/model/effort path. Do not silently override them.

Roles define responsibilities and permissions; model and effort vary per task. Prefer the least costly
likely-successful choice, considering retries and parent overhead. Use these subtask boundaries:
- trivial: tiny direct answer or one small lookup; keep it in main without a worker.
- simple: bounded extraction, summaries, mechanical edits; prefer fast/low.
- standard: ordinary implementation or review; prefer workhorse/medium.
- complex: architecture, concurrency, subtle numerical correctness or difficult debugging; use frontier/high.

Decompose mixed work: a complex project can have independent simple lookups, standard implementation,
and complex design/validation work_items. Classify each and dispatch separately when it saves work;
do not multiply tiny jobs. Complexity is the parent's reasoned judgment, not keyword matching.
For complex work, auto excludes weaker models and unsupported high effort; insufficient allowed
capacity returns replan instead of silently degrading. Parent effort is unobservable, so auto does not
assume its current setting satisfies complex work. Explicit user-selected model/effort remains authoritative.
A needs-work validation escalates the retry's model tier; stronger efforts remain bounded by support,
previous effort and attempt limits. Higher effort needs a concrete difficulty or failed-validation reason.
Native Claude agents consume the same Claude quota. Balance percentages and API money are different units.
When Claude quota is low, delegate whole independent stages; retain coordination and acceptance in main.

Use focused objectives, file paths and required evidence rather than copying full transcripts/files.
Group related work to amortize context setup. Give independent stages different work_item IDs; retain the
same ID for retries. Do not invent IDs to bypass limits. Already accepted output is reused; active work is awaited.
Resume failed work with new failure evidence rather than starting over. Three attempts per work item is the
default bound; revisit the approach when exhausted. User-requested limits are configurable at task begin.

Inspect actual outputs/diffs/checks. Then combine child and parent acceptance:

`delegate_task(action="finish", accepted_jobs=[...], verdict="accepted"|"needs-work"|"blocked", evidence=...)`

Only include child IDs whose results you actually checked. Stop captures the parent's final response locally
without another model turn. For required results, wait up to 50 seconds per result call or work independently; continue waiting if still active. Use await_completion=false or delegate_job(action="detach") only when the user explicitly requests a background task.
Results are compact; detail=true or the saved record supplies full output. Do not accept from a truncated
summary when missing evidence matters.

## Accounting boundaries

A ledger starts on an enabled session's user prompt. Explicit task begin also permits main-only baseline
measurement while delegation is off. Use task update once before dispatch to set label/category/complexity
(trivial/simple/standard/complex), case_key and arm. Keep comparable task categories stable.

Reports separate parent, native and plugin usage, cached versus uncached tokens, failed attempts and final
acceptance. Missing or partial measurements are incomplete, not zero, and cannot establish savings.
Finish reports are preliminary until Stop captures final-response usage. token_budget only stops subsequent
dispatch after observed spend reaches it; it is not an in-flight/main-model hard cap or a dollar budget.

Cold-start estimates assume no worker token reduction: startup and handoff overhead are added. After at least
three fully metered/reviewed attempts of a named comparable class, failure-inclusive costs and parent overhead
inform selection. Unknown costs/quality keep bootstrap estimates. Model capability tiers are heuristics.
Near-reset surplus breaks only near-cost ties (within 15% of the best token-volume estimate). Known Claude
scarcity or explicit preserve-claude can prioritize capacity over minimum total tokens.

For comparisons, case_key must identify the same frozen inputs and acceptance. Both runs must be accepted
and fully metered. Report processed-token and uncached-proxy differences together: less Claude usage alone,
or fewer total tokens with a different cache mix, is not proof of lower bills/subscription consumption.
Do not fabricate baseline runs, silently exclude costs, or promise mathematically optimal quota use.

Write jobs use retained clean Git worktrees. Review integration; never auto-merge/commit/delete user changes.
Codex and Claude/DeepSeek worker conversations archive automatically; pending cleanup is not success. Explicit resume restores the
same thread and completion rearchives it. Job results remain available. A background completion hook can wake an idle interactive Claude session. The Stop hook also reminds Claude to collect required results. A closed session or terminated `claude -p` cannot be woken; results remain available on resume.

## Task scopes and verified handoff (0.7.1)

Prefer delegate_handoff bind with delegate_task_id (and optionally work_item) to link
individual tasks in one Claude session to different am tasks. Use the same scope for
next/report/sync. Legacy session-wide bindings still work but cannot coexist with scoped
bindings; start a fresh session to adopt scoped linking. Overlapping scopes are rejected.

Before handing work over call delegate_inspect(check, job_id). It checks the current
workspace, registered sibling execution and explicitly checkpointed files. This is a
point-in-time observation, not permission to steal an am lease or a whole-project proof.
After testing, call delegate_inspect(checkpoint, job_id, files=[relevant source/data/test
report paths]); this resets review to pending. Then validate and record accepted with
actual evidence. Changed or missing evidence/HEAD blocks automatic reuse. Old accepted
results without checkpoints must be revalidated before automatic reuse.

Use delegate_inspect(action="metrics") for observed per-category/model/effort outcomes,
wall-clock duration and missing usage. These worker metrics exclude parent overhead;
use task reports for total costs. Do not claim universal model rankings from sparse or
unmatched samples. Never hash entire data trees; nominate at most 16 regular files,
at most 16 MiB each, within the workspace. Broader dependency validity is unknown.

Commands: `metrics` calls delegate_inspect(metrics); `inspect <job-id>` calls check;
`checkpoint <job-id> <files>` calls checkpoint, followed by actual validation and review.
Use `link <am-id> task <delegate-task-id>` (optionally `work-item <id>`) for scoped binding.

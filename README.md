# Delegate — Claude Code, Codex and DeepSeek Harness

A shared delegation engine for Claude Code, Codex and DeepSeek Harness, with
per-task model/effort, durable results and configured Claude/Codex/API workers.
Version 0.24.0. Activation is configurable; new installations default off.

Quota-aware orchestration now compares all eligible models before soft family preferences.
Bounded expected-work tradeoffs preserve scarce quotas, and each assessment includes
parent-applied staging/review guidance without another classifier call. See
[quota and intelligence orchestration](docs/ORCHESTRATION.zh-CN.md).

The CLI now has help/version/doctor/schema and direct result/verify commands. Export
full reports with `delegate-agent result JOB --session-id SID --output REPORT.md`:
no private job-JSON parsing or report-body echo. Verify and repeated result calls return
compact receipts; explicit page/file views control rereads and full artifacts.
See the [bundled CLI guide](plugins/delegate/skills/delegate/references/cli.md).

Explicit `delegate_loop` supports durable parent-gated tasks and bounded automatic
loops with file-change triggers, per-loop token admission budgets, pause/cancel,
same-conversation continuation and crash recovery without blind replay. Supervision
runs in code; results and goal acceptance stay separate. See
[persistent tasks and loops](docs/PERSISTENT.zh-CN.md).

Declare `required_files` / `output_files` before dispatch: unreadable briefs, restricted
file-tool paths and read-only/file-output conflicts fail before inference. Codex checks
declared inputs under its selected sandbox. Receipts distinguish job versus serving
runtime versions; old connections need the current CLI or reload. See
[input/output contracts and migration](docs/INPUTS.zh-CN.md).

Parents assess each subtask once with `task_profile` (scope, uncertainty, correctness
risk and validation), then delegate derives difficulty/model/effort: fast Terra or
DeepSeek Flash, everyday Sol, demanding Astra. Tests/replay require executable tools;
automatic reasoning never selects max/ultra. No extra classifier model is called.
See [automatic routing](docs/ROUTING.zh-CN.md).

Long required tasks can use `notify=true` (or `delegate_job(action="watch")`) to wait
in code and notify on completion. A verified live waiter lets a persistent Claude
session pause without dropping the required result. Long reports expose exact result
pages through `next_result_call`; collection requires the complete text, not a preview.
See [results and waiting](docs/RESULTS.md).

Concrete roles support automatic model/effort selection without switching providers.
Catalog discovery lists all roles; query `agent_name` for its provider's models.
`operation` explicitly selects read/write. Builders can receive selected uncommitted
drafts with `source_inputs` while preserving the source checkout and index.
See [draft files and dispatch examples](docs/DRAFTS.md).

Shutdown reports distinguish confirmed runtime helpers from task/unknown children and
recheck recorded identities after cleanup. Raw child counts alone do not mean unfinished
work. Every worker must report acceptance conflicts and unperformed checks explicitly;
criteria cannot be silently skipped or weakened to match a faulty baseline.

Declare GPU/SSH/command-network needs before dispatch using `requirements` and a
role-permitted `execution_profile`. Launch preflight runs under the selected policy
before inference; default sandboxing stays unchanged and host access is explicit.
See [execution requirements and limits](docs/EXECUTION.md).

Running supported jobs can be extended directly with
`delegate_job(action="extend", extend_seconds=3600, wait_seconds=5)`. Errors and runtime
status show actual timeout bounds; worker acknowledgement distinguishes a written change
from an applied one. No timeout/resume cycle or additional model is required.

Use `delegate_run(background=true)` for explicitly requested background work, without
a second detach call. Results now include redacted pre-shutdown child command lines
and an advisory static check for tests depending on uncommitted/ignored local helpers.

Runtime reliability: frozen server/worker images, redacted startup stderr, process/log
inspection, finite-deadline checkpoint warnings and Codex no-inference condition waits.
See [runtime and troubleshooting](docs/RUNTIME.zh-CN.md). Existing MCP servers need restart.

Workspace preparation now defaults to 600 seconds per Git command with cancellation,
owned-process cleanup and checkout recovery. Proven pre-inference failures do not consume
the model-attempt limit or trigger escalation. An existing session can use the current
`delegate-agent call` CLI when its host defers MCP reload, retaining the same session ID.

## Quick start / 快速开始

[中文完整安装手册](docs/INSTALL.zh-CN.md) · [Supported surfaces and limitations](docs/CAPABILITIES.md) · [Release notes](docs/CHANGELOG.md)

Choose one parent host. Authenticate and verify at least one worker backend first.
Codex/DSH users clone this repository and run `python3 scripts/install-hosts.py codex`
or `dsh --dsh-profile web` after initializing that profile. The full guide explains
account setup, activation, first-task verification, optional am and safe upgrades.

## Install

```sh
claude plugin marketplace add LianguangXue/delegate
claude plugin install delegate@local-delegation --scope user
```

Restart Claude Code after upgrades so its MCP server and hooks use the same version.
New sessions inherit configured `default_mode` (built-in fallback: off).
Explicit session on/off/manual overrides that preference. Role definitions persist. Native Claude agents remain available independently.

```text
/delegate on
/delegate status
/delegate agents
/delegate models codex
/delegate quota
/delegate run codex-reviewer Review the current changes and report evidence
/delegate jobs
/delegate result <job-id>
/delegate resume <job-id> Address the failed check
/delegate off
```

The full skill command is `/delegate:delegate` when the short alias is ambiguous.
Explicit `run` is one-shot authorization and does not turn on automatic delegation.

## Model selection and usage

Each work item has its own complexity, model, effort and acceptance requirements.
Tiny work stays in main; simple work prefers fast/low; standard work prefers
workhorse/medium; complex automatic work requires frontier/high. Unsupported
combinations, quota reserves and role allowlists constrain choices. Failed
validation raises the retry model floor. Explicit model choices remain supported.
Model-family tiers are heuristics, not cross-provider quality benchmarks.

Task ledgers account for parent/child usage, cache counts and retries. Missing usage
stays unknown. Token volume, API money and subscription quota are different units;
no universal cost-saving or optimality claim is made. Soft task budgets gate new
dispatches; they do not cap an in-flight generation. Default limits are two concurrent
jobs, twenty attempts per session and a 900-second job timeout.

## Execution and history

Codex workers use ChatGPT authentication and its quota; API-key fallback is disabled.
Claude-native workers consume Claude quota; API providers use their configured
credentials. Parent coordination still consumes parent capacity.

Write roles use isolated worktrees retained for review. The source must be clean unless
explicit `source_inputs` selects draft files to snapshot; the source itself stays untouched.
Nothing automatically merges or publishes changes. Local worktree commits can be explicitly authorized per attempt. Claude/API workers use restricted
file tools; parent-owned tests may be needed when shell access is required.

Completed Codex and Claude/API conversations are archived recoverably. Explicit
resume restores the exact conversation and completion archives it again. Job results
and worktrees remain available. Completion hooks use asyncRewake where supported,
and Stop guards request missing results. Pending notices replay on session start
until collection. Collection is distinct from acceptance. A closed host cannot be
woken; reopen it and inspect the task records. Explicit background work can detach.

## Providers and credentials

Use `/delegate providers` and `/delegate agents` to inspect or configure accounts and
roles. Providers can reference environment variables or existing Claude Switch Bridge
account snapshots. No keys should be placed in prompts or ordinary plugin config.
Accounts remain separate; failures do not silently substitute another account.
Metadata failure does not prove inference failure. Configure allowlists and verify
inference independently. See the [account reference](plugins/delegate/skills/delegate/references/accounts.md).

State lives in `~/.claude/delegate`; `DELEGATE_STATE_DIR` selects an isolated test
store. Configuring a provider does not activate delegation or switch the main account.

## Optional am integration

[am](https://github.com/LianguangXue/am) remains a separately installed, independently
usable shared-memory CLI. am 0.2.0 adds the versioned handoff import protocol.

Create an am task with `am start <task-id>`, then in Claude:

```text
/delegate link <task-id>
/delegate handoff next <verified next step>
/delegate handoff
```

The binding covers this delegate session's job history. Use a new session for a
different am task. A successor reads `am brief --task <task-id>` or
`am delegate report --task <task-id>`.

Delegate owns execution and its retryable outbox; am owns goals, leases and evidence.
Snapshots are bounded, versioned and idempotent, with omitted counts and original
record paths. Import does not claim leases, execute next steps or mark tasks done.
No daemon or automatic external-event subscriptions are enabled.

## Development

Python 3.12+, Linux process identity support, Git for worktrees, authenticated backend
CLIs, and a Claude Code host supporting plugin hooks/MCP are required. The Python
runtime has no third-party package dependencies.

```sh
python3 -m unittest discover -s tests -q
python3 -m compileall -q plugins/delegate/scripts tests
claude plugin validate plugins/delegate
```

Optional am integration tests use a sibling `am` checkout, or `AM_TEST_BINARY` pointing
to its executable. They are explicitly skipped when am is unavailable. Tests use
throwaway state and no model inference. Host UI wake behavior and provider inference
require separate live validation. Local machine validation reports are not published.

## Task-scoped handoff and evidence checks (0.7.1)

`delegate_handoff` now accepts `delegate_task_id` and optional `work_item`. A single
Claude session can link different task scopes to different am tasks. Legacy whole-
session links remain supported but cannot coexist with task links. Overlapping scopes
are refused. Scoped next steps, snapshot versions and source IDs stay separate.

`delegate_inspect` supports `check`, `checkpoint` and `metrics`. Check reports current
workspace/evidence and registered sibling liveness blockers, without taking leases.
Checkpoint fingerprints explicit bounded regular files and Git HEAD; it resets review
to pending, so validate before marking accepted. Automatic reuse requires an unchanged
checkpoint. Unknown/changed dependencies need revalidation. This is a point-in-time,
named-evidence check, not a transactional write lock across the whole workspace.

Metrics report actual worker attempts, reviewed acceptance, failures, durations and
usage gaps by task category/model/effort. They do not measure causal model superiority
or billed savings; parent overhead is in the task ledger. am 0.3.0 task cards show
running, uncollected, awaiting-review, accepted, needs-work and conflicting summaries.

## Codex startup reliability (0.7.2)

Worker startup no longer depends on the cosmetic thread/name/set RPC. Thread source
metadata still records ownership. Thread start/resume and turn start allow up to 120
seconds within the task deadline; initialize/account/restore RPCs share that deadline,
and cancellation interrupts RPC waiting. Expired requests are not sent and mutating
requests are never automatically replayed. Cleanup may take additional bounded time.

Oversized inherited instruction files are referenced for mandatory bounded file-tool
reading instead of rejecting the job; duplicate canonical instruction paths are read
once. Inability to read required instructions must be reported as blocked. A provider
serverOverloaded response records a separate 60-second per-model cooldown, consulted
by dispatch and planning even after account quota refresh. It is not quota exhaustion
and does not automatically switch accounts or replay partially executed work.

## Quota-first collaboration (0.8.0; am 0.4.0)

`delegate_exchange` adapts the local `am work` protocol. Search/get saved artifacts
before requesting new inference; publish bounded summaries and versioned evidence
references instead of full conversations. This tool invokes no models. Explicit
completion manifests support background program waits with host-native completion
notifications; stage receipts support dependency/freshness checks without rerunning
side effects. Neither registry schedules jobs or takes leases.

Questions default off. Explicit configure enables one new question per am task,
with one recipient, <=2000-character question/reply and no forwarding. Messages
stay queued until explicitly read/answered or delivered through an authorized host
channel. Enqueueing never automatically resumes a long conversation or starts an
agent. Sender identity is bound to the MCP session. Limits apply to this protocol's
requests, not arbitrary native messages or in-flight model usage. See the bundled
skill and [am](https://github.com/LianguangXue/am) documentation for payload schemas.

This release adds mechanisms for reuse and measurement, not a claim of measured
subscription savings. Keep per-provider usage, quality, parent overhead and failed
attempts in comparisons. Restart Claude to load the new MCP tool.

## Optional local commits (0.9.0)

A builder can return a local commit by setting allow_commit=true, commit_message and
commit_paths on delegate_run. Permission defaults false and must be explicit on each
resume. Only isolated write worktrees are supported. The model edits/checks files;
the runtime commits only approved literal paths after successful backend termination.
Claude/API workers retain their file-only tool restrictions. Read-only roles reject
commit authorization. Receipt git_commit contains the hash, actual files and worktree;
parent acceptance is still required.

Example tool fields: allow_commit=true, commit_message="Fix cache invalidation",
commit_paths=["src/cache.py", "tests/test_cache.py"]. No source checkout commit,
push, merge, amend or reset is requested by the runtime. Dirty starting worktrees,
unexpected staging/HEAD changes and escaping paths fail safely. Git subprocess
identities participate in normal crash recovery. User Git hooks and signing settings
are respected; failures preserve work for inspection instead of discarding it.

## Optional unbounded runtime (0.10.0)

Explicit timeout_seconds=0 disables the job's total wall-clock limit. Ordinary tasks
retain the existing finite default. Per-control RPC timeouts, user cancellation,
provider/account limits and cleanup protections remain in effect. No active legacy
worker is silently migrated or terminated by an upgrade.

`delegate_job` action `runtime` reports elapsed time, process identities, heartbeat
and separate progress age/phase. On supported active workers, pass timeout_seconds
again to change the total limit from the original start (0 removes it). Cancellation
or archival already underway cannot be reversed. Old workers reject live changes.
Worker monitoring is code-only; lack of intermediate output is not proof of a stall.
Unbounded runtime is not an in-flight token or monetary cap.

Host completion observation has a 7-day timeout, independent of job execution.
After the watcher expires, a runtime/result query can establish another one; an
existing watcher is deduplicated, not renewed in place. Closed hosts cannot receive
wakes, while durable job records/notices remain available. Restart Claude to load
the new long-lived MCP implementation. Defaults and unrelated running jobs remain
unchanged until explicitly configured.

## Default availability (0.11.0)

On explicit user request, `delegate_configure(section="defaults",
value={"default_mode":"auto"})` makes fresh sessions directly use configured
automatic roles without a separate on command. `off` and `manual` are also valid
new-session defaults. Existing saved session modes remain unchanged. `/delegate off`
always disables automatic dispatch for that session without cancelling its workers.
The skill supports automatic model invocation so Claude can load its workflow itself.
Tiny tasks still stay in main, and automatic mode grants neither commit permission,
manual-only accounts, cross-session question budget nor remote publication authority.

## Model and workflow recipes

See the [Chinese Claude Code + delegate playbook](plugins/delegate/skills/delegate/references/model-playbook.md)
for evidence-first model/effort selection, a native read-only helper template, scoped
fixes, numerical/concurrency reviews, long jobs, batch work and matched evaluation.
It references the 2026-11-09 claude.dev article plus official model/effort/subagent
docs. It does not change your main model, enable usage credits or widen allowlists.

## Codex and DeepSeek Harness parent hosts (0.12.0)

Run `python3 scripts/install-hosts.py codex|dsh|all` for a first-time local install.
The installer adds a Codex stdio MCP entry, host-specific skills and/or a DSH MCP row
in the selected existing web profile (`--dsh-profile NAME`). It backs up existing
configuration, checks Codex non-delegate settings are unchanged and never stores keys.
Existing entries with the same names are refused for manual inspection. It creates
`~/.local/bin/delegate-agent` pointing to this checkout, so keep the checkout available.
Codex tools allow 180 seconds; normal result waits remain at most 50 seconds. The DSH
MCP entry explicitly references native Codex/Claude configuration homes so isolated
Harness launches do not accidentally search an empty HOME. No sandbox is disabled.
Other isolated profiles need the same MCP row/explicit homes in their own overlay.

Restart Codex or reload/restart the DSH profile, then open a new conversation. Say
“use delegate to assign a bounded task to another model”. The host calls delegate_host
once, retains its returned logical session_id, then uses the existing delegate tools.
Supply the actual native session ID if available; otherwise retain the generated ID.
Using the same ID on reconnection recovers ownership. IDs separate logical host/task
namespaces, not security against another process using the same OS account.

Codex can delegate to configured Claude/DeepSeek/Codex roles; DSH can delegate to
configured Codex/Claude/API roles. All model/effort allowlists, reserves, manual-only
accounts and explicit commit requirements remain enforced. `preserve-parent` excludes
the declared parent provider when conserving its capacity. Unknown DSH account mapping
must stay unknown, not borrow Claude quota. Native-parent token metering is unavailable;
task finish closes the logical task but measurement_complete remains false. Worker
usage is reported separately and does not establish whole-task savings.

Delegate workers cannot recursively use the host adapter. Codex app-server workers
mark their environment and disable the delegate MCP entry only when that table exists.
Claude/API workers retain strict MCP restrictions. Other user MCP servers are unchanged.

These hosts have no Claude Stop/asyncRewake hooks. Collect results with delegate_job;
for code-only background waiting use `delegate-agent wait codex` or `wait dsh` and JSON
stdin `{ "session_id": "...", "job_id": "...", "background": true }` through a host background-command
facility. The waiter returns a completion notice, not a collected report. A host-native completion notification is only available if that host supports
it. Closed or unsupported idle hosts are not promised an automatic wake. Results,
conversation resume, archival, commits and am handoffs remain durable and inspectable.

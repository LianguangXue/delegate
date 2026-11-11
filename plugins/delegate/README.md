# Delegate

Version 0.17.1 separates confirmed runtime helpers from task/unknown processes in
shutdown reports and rechecks recorded identities after cleanup. Raw child counts
alone do not mean unfinished computation. Fresh and resumed workers must report
every acceptance item with evidence, including conflicts and checks not performed;
they must not silently skip requirements or weaken rules to match a faulty baseline.

Optional local delegation for Claude Code. New sessions inherit configured default_mode (built-in fallback off). Run
`/delegate:delegate on` to permit this session to choose configured workers, and
`/delegate:delegate off` to stop new automatic delegation. The short alias `/delegate`
also works when unclaimed. `run`, `agents`, `providers`, `models`, `quota`, `jobs`,
`result`, `tell`, `cancel`, and `resume` are handled by the same command.

Each worker task has a selected model and reasoning effort, a rationale, and explicit
acceptance conditions. Codex uses ChatGPT login and its own quota; main Claude still
uses quota for coordination. DeepSeek and other Anthropic-compatible backends must
be configured and enabled explicitly. New provider configuration does not switch on
delegation. Keys are provided through environment-variable references.

Codex write tasks use clean detached Git worktrees, retained for inspection. Claude
and Anthropic-compatible CLI workers use restricted file tools and have no shell.
Jobs remain pending verification until the parent validates their result. An async completion hook can wake an open idle Claude session; a Stop hook reminds
Claude to collect required results. Closed sessions and exited `claude -p` processes
cannot be woken. Explicit background jobs may be detached.
Resumed sessions retain their explicit mode. A user-requested default_mode=auto
makes new sessions automatically available, while explicit session off is preserved.

Version 0.2 adds session-scoped quota collection and pressure-based routing: low
Claude quota favors delegating complete stages to enabled external agents. Unknown
quota remains labelled unknown and conservatively preserves the main session.
`/delegate budget` displays the evidence. Finished Codex threads are automatically
archived out of ordinary session lists; explicit resume restores them and completion
archives them again. Job results and project modifications remain preserved.

State is stored in `~/.claude/delegate`. Read the bundled skill for configuration and
operation details. Source, tests and full instructions are maintained locally at
[LianguangXue/delegate](https://github.com/LianguangXue/delegate).

Version 0.13 freezes runtime sources per server/worker, preserves redacted startup
stderr and adds `delegate_job inspect` for owned processes, commands and log paths.
Use explicit timeout_seconds=0 for long tasks; finite deadlines warn before cleanup.
New Codex threads provide delegate_wait for completion files/owned processes without
repeated model polling. Keep checkpoints: sandbox children may stop when the job ends.
Restart old MCP servers to apply fixes, allowing existing experiments to finish first.
See the repository's docs/RUNTIME.zh-CN.md for limits and upgrade instructions.

Version 0.14 separates workspace infrastructure failures from model attempts. Git
preparation defaults to 600 seconds, with owned process cleanup and durable recovery
receipts. Same-session current CLI fallback works when a host defers MCP reload.
Failed setup does not raise model/effort retries or erase the work item's classification.

Version 0.15 adds background=true at dispatch, redacted child argv/command lines in
shutdown results, and advisory checks for hidden local test-helper dependencies.
Checks are static and bounded; they do not run tests or stage/copy project files.
Install with `claude plugin marketplace add LianguangXue/delegate` then
`claude plugin install delegate@local-delegation --scope user`.

Version 0.3 adds independent DeepSeek official/company accounts referenced from
Claude Switch Bridge. Private snapshots and pinned endpoints are checked at runtime;
keys are not copied into plugin configuration. Metadata 401 responses do not disable
inference; unavailable balances stay unknown and discovery uses the configured
allowlist. Actual inference must be checked separately. API worker histories are
isolated by provider and account/model/effort choices remain explicit.

Version 0.4 adds parent/child task accounting, conservative model/effort planning,
quota-surplus tie breaking, work-item reuse and bounded retries. Auto dispatch and
wait can be combined; verified child/parent acceptance can be combined. Use `task
list`, `report <task-id>`, `plan`, and `compare <case-key>` through `/delegate`.
Missing usage stays incomplete. Savings comparisons separate processed tokens from
uncached proxies; neither proves subscription savings. Default-off remains unchanged.

Version 0.4.5 extends recoverable conversation archival to Claude/DeepSeek workers,
including their companion session directories. Explicit resume restores and then
rearchives them. Job results and worktrees are retained; cleanup conflicts stay visible
as pending rather than deleting records.

Version 0.5.0 routes each work_item independently: simple fast/low, standard
workhorse/medium, complex frontier/high. Parent classification is preserved, failed
validation raises the retry model floor, and complex work never silently falls back
to weaker models or unsupported high effort. Model tiers are catalog/family heuristics.
Restart Claude to ensure its MCP server uses the updated routing and tool schemas.

Version 0.9.0 supports explicit per-attempt local commits: allow_commit=true with
commit_message/commit_paths, on a clean isolated builder worktree. Runtime performs
the scoped commit and returns its hash, without granting API workers shell tools.
No push/merge is invoked, and a commit is not parent acceptance.

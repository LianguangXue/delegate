# Release notes

This publication bundles the changes since the earlier 0.12.0 snapshot. Version
numbers below describe implementation milestones, not separately published Git tags.

## delegate 0.17.1
- Distinguish captured app-server/code-mode helpers from task/unknown children;
  keep raw pre-teardown counts and commands, prioritize unknown processes in summaries.
- Recheck recorded process identities after teardown. Live helpers still warn;
  missing legacy commands are unclassified, never retrospectively declared harmless.
- Every backend's fresh/resume prompt requires per-item acceptance evidence and
  explicit conflicts; no silent skips, rule changes or assumed-correct baselines.
- Validation: 255 tests (`python3 -m unittest discover -s tests -q`), syntax checks,
  native Codex initialization/process observation and simulated dispatch/cleanup.
  Real GPU workloads and SSH authentication are not established by these checks.

## delegate 0.17.0
- Declare GPU/SSH/task-network requirements and explicit device/TCP targets up front.
- Per-role sandboxed/network/host grants; default unchanged, no automatic host escalation,
  no host permission for read-only roles or command-resource claims by file-tool workers.
- Same-policy non-inference launch preflight, structured coverage/outcomes, allocation
  visibility checks, managed-mode checks and launch-time grant revalidation.
- Capability-aware planning excludes unverified main execution; blocked checks do not
  spend model attempts. Cleanup preserves runtime-validation and owned-artifact boundaries.

## delegate 0.16.0
- Timeout errors include the received value and actual configured numeric range;
  control/catalog/runtime expose finite bounds and explicit zero/unbounded mode.
- Extend queued/running supported jobs with action=extend + extend_seconds, without
  waiting for timeout/resume. Optional wait_seconds waits for worker acknowledgement.
- Revision/value acknowledgements distinguish written settings from a worker read.
  Legacy workers report unconfirmed rather than a fabricated success acknowledgement.
- Timeout cancellation is serialized with extensions; RPCs re-read crossed dynamic
  deadlines while preserving their separate fixed per-request timeouts.

## delegate 0.15.0
- Verified child observations include bounded redacted argv/command_line, cwd and
  capture status. Results include up to eight pre-shutdown child commands even
  without Codex tool-command events; inspect retains the full bounded snapshot.
- Advisory static Python delivery checks after successful execution/authorized
  commit identify missing, uncommitted or external local helpers. No project imports,
  test execution, copying/staging or expanded commit authorization.
- delegate_run(background=true) directly returns a non-required background receipt;
  await_completion=false and a later detach remain compatible.

## delegate 0.14.1
- Preserve a work item's declared complexity/category across pre-inference failures,
  while only real model attempts raise retry model/effort floors.

## delegate 0.14.0
- Workspace Git commands default to 600 seconds; configurable worktree_timeout_seconds
  (10..86400). Owned process groups, heartbeats, cancellation and setup receipts.
- Timed-out checkouts are reused only after identity/HEAD/cleanliness validation.
  Known initialization locks are released after checkout stops; clean failed trees are
  removed without force, changed/untracked/ignored artifacts remain with recovery paths.
- Model-attempt limits and retry escalation exclude proven pre-inference failures,
  including narrowly identified legacy runtime/worktree errors. Unknown or sent model
  requests still count; infrastructure coordination remains in parent cost accounting.
- Current fixed CLI can serve an existing Claude session after deferred MCP reload;
  upgrade errors/hooks give a one-time recovery hint. No automatic task replay.

## delegate 0.13.1
- Refuse jobs from legacy parents missing a frozen runtime identity, before workspace,
  backend, commit or archival side effects. Existing already-running workers remain intact.

## delegate 0.13.0
- Content-addressed read-only runtime images: entrypoints freeze before service imports;
  workers verify the same digest before preparing a workspace. Managed native-host
  launchers point at an image, not a development checkout. Restart old MCP servers.
- Bounded redacted Codex stderr per job, initialization phase/exit-code/tail on disconnect.
- Catalog includes exact agent names, roles and configured timeout bounds; numeric
  validation errors explain the allowed range, including explicit unbounded mode.
- `delegate_job inspect`: owned host/namespace process IDs, active command events,
  log paths, fingerprint and pre-shutdown observation. Canonical state/worktree paths.
- Codex dynamic `delegate_wait` waits for a workspace receipt or verified child exit
  in code, while parent cancellation/steering/heartbeat stay live. No inference during
  the wait itself; tools require experimental app-server support. No automatic retry.
- Finite deadlines warn/steer before shutdown; active work is reported, not detached
  or falsely represented as durable success. Parent can remove limits with timeout=0.
- Worker brief states sandbox, offline/cache, checkpoint and waiting constraints.
  Project-specific flags belong in role/context; no global database/SSH enablement.

## delegate 0.12.0
- Codex and DSH parent adapters, host-specific skills, installer and logical session IDs.
- Parent-aware quota planning/preserve-parent, explicit unknown native-parent usage.
- Non-Claude task finalization without depending on Claude Stop hooks.
- Worker recursion blocked; isolated HOME native backend paths explicitly referenced.

## delegate 0.11.x
- User-configurable default activation; saved off/manual stays authoritative.
- Model/effort workflow playbook; Fable recognition without automatically allowing it.

## delegate 0.10.0
- Explicit timeout_seconds=0, live runtime changes, heartbeats/progress inspection.
- Bounded control RPCs remain; total execution and host notification window separated.

## delegate 0.9.0
- Explicit per-attempt, scoped local commits in clean detached worktrees.
- No runtime push/merge; Git identity, cancellation and crash recovery checks.

## delegate 0.8.0 / am 0.4.0
- Local artifact lookup/publication, bounded question queues, producer receipts and
  stage evidence via am work and delegate_exchange; no automatic model execution.

## Reliability fixes included
- Cosmetic Codex naming no longer blocks task startup.
- Shared/dynamic execution deadlines; cancellation and transient-model cooldowns.
- Large inherited instruction files referenced for mandatory bounded reading.
- Codex/Claude/API conversation archive/resume remains recoverable; worktrees retained.

See installation and capability docs for current limits, account prerequisites and
verification scope. Local smoke tests are not performance or billed-savings guarantees.

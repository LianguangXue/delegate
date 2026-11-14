# Release notes

This publication bundles the changes since the earlier 0.12.0 snapshot. Version
numbers below describe implementation milestones, not separately published Git tags.

## delegate 0.25.1
- Coordinate root and locks/ lock files together so immutable older runtimes remain
  mutually exclusive with new callers. Partial acquisition is released on failure.
- GC retains coordination lock inodes and frozen runtimes. An idle flock or an image's
  age cannot prove no live/queued/older process will use it. Retained loops protect
  resume children independently of execution status; no real-store migration is needed.
- Pending notifications discover legacy registrations through the authoritative jobs
  index while avoiding repeated reconciliation of already-settled history. Concurrent
  registrations remain pending when a hook prunes its processed snapshot.
- See [GC scope and mixed versions](GC.zh-CN.md). Existing processes keep their code;
  use the new CLI for GC and restart old MCP connections when their work permits.
- Validation: 389 tests, independent review and installed CLI/hook fixtures covering
  both lock paths, retained runtimes and legacy notifications. No real-store GC or model calls.

## delegate 0.25.0
- Harden runtime image publishing (`runtime-image.json` version stamp, read-only permissions
  before atomic rename, `-B` launcher/bootstrap flags) and align `codex_rpc` `clientInfo`
  with `runtime_image.VERSION`.
- Eliminate WAL `dispatch-<jid>.json` growth, move per-entity lock files under `locks/`,
  maintain `pending_jobs` on session records for bounded hook scans, and add dry-run-first
  store garbage collection (`delegate-agent gc`, `delegate_control action="gc"`).
- Separate `inference_identity` from git/archive `helper_identity` and let `cancel_orphan`
  cleanly recover dead orphaned records.
- Enforce `st.key()` across file-backed helpers, redact Claude CLI diagnostics alongside
  Codex stderr, bound `AGENTS.md`/`CLAUDE.md` ancestor walks against symlinks, cap
  `persistent_tasks.wait()`, fix Feb 29 quota reset parsing, and honor configured
  `model_tiers` and `is_default` provider hints.

## delegate 0.24.0
- Keep eligible models through expected-work estimation; family preferences no longer
  erase quota/cost alternatives or hide empirical candidates beyond the first four rows.
- Within the existing 15% expected-token band, preserve known scarce quota when a healthy
  alternative exists; unknown quota remains unknown. Reserve checks cover all known
  percentage providers; limiting-window pressure includes Claude spend limits.
- Scoped low-risk, low-uncertainty implementation with explicit tests can use the fast
  lane; missing tests or higher risk retain the workhorse/frontier floor.
- Assessments include parent-applied staged execution, risk-based review and compact
  handoff/wait guidance. No classifier calls, automatic workflow launches or new grants.
- Validation: 373 regression tests plus final targeted planner checks and independent
  review. Installed CLI replay covers six task/quota scenarios with network and model
  subprocess calls blocked. These establish routing behavior, not measured billing savings.

## delegate 0.23.2
- Task update accepts inspected successful children with accepted_jobs/evidence, without
  changing the parent's classification or closing it. The entire batch is validated
  before reviews are written; retries preserve existing accepted evidence.
- Legacy verify responses containing exact full terminal text or failure diagnostics
  now record delivery. First uncollected legacy bodies remain visible; subsequent echoes
  can be compacted. Verdict-only receipts and partial text never establish collection.
- MCP initialization reports the canonical frozen-runtime version rather than a separate
  hardcoded string. Installed versions still do not replace already-connected processes.
- Validation: 361 tests, syntax checks and installed CLI/hook simulations. Batched child
  reviews preserve task classification/state; full legacy verification is collected and
  quiet across repeated tools, while partial reports remain guarded. No provider inference.

## delegate 0.23.0
- User CLI help/version/doctor/schema, stdin/file envelopes and direct result/verify
  commands; empty/invalid input reports a concise error instead of JSON tracebacks.
- Verify/archive acknowledgements and repeated auto result requests omit report text,
  with explicit page/summary/file views and continuation/reread/full-artifact handles.
- Full report export is exact UTF-8, atomic/no-clobber and acknowledged after publication;
  failed/empty-path exports do not collect. Existing matching files can be reused.
- A supported PostToolUse hook can compact legacy MCP echo without hiding first modern
  pages or mislabeling the old server as the hook's version. CLI works independently.
- Validation: 347 tests and independent review; installed CLI help/schema/error handling,
  simulated dirty-source builder with unchanged files/index/HEAD, and an exact 264,146-byte
  twelve-finding UTF-8 export. Repeated-result and verify receipts were 1,214/1,329 bytes.
  No real provider inference or live IDE output-replacement test for this milestone.

## delegate 0.22.0
- Explicit durable parent-gated tasks and bounded loop controllers with same-conversation
  continuation, pause/cancel, live child inspection/steering and reviewed step advance.
- Optional file-change triggers and scoped done receipts keep waiting model-free;
  cycle limits, soft token admission budgets and unknown-usage stops bound repetition.
- Frozen controller/runtime, serialized control, journal repair/adoption, stable launch
  keys and late-worker fencing prevent blind inference replay after controller crashes.
- Separate worker ledger, original session ownership, per-cycle retry limits and explicit
  goal evidence preserve accounting/permission/acceptance boundaries. No automatic commits.
- Validation: 335 tests plus real-process Codex/API simulators covering controller
  loss/recovery, exact conversation continuation, archival and reviewed advancement.
  A bounded automatic loop skipped unchanged triggers. No real provider inference.

## delegate 0.21.0
- Required input/output file contracts reject missing/special inputs, file-tool scope
  violations and read-only report-writing conflicts before inference. Explicit lists
  override conservative legacy absolute-path discovery; no permission expansion.
- Codex checks file readability with fixed same-policy command/exec before model turns;
  file-tool builders require relative snapshot references rather than source absolutes.
- Job/serving versions are separately visible, old new-dispatch images warn once, and
  dirty-source failures explain source_inputs or read-only report-text recovery.
- Validation: 312 tests, independent contract review, installed MCP rejection before
  an API job starts, and a simulated Codex file-preflight denial with no account/thread
  or model turn. No real provider inference or rerun of accepted analysis.

## delegate 0.20.0
- Parent-reasoned task_profile derives per-item difficulty/tool requirements; no
  classifier inference or keyword/length-based task scoring. Mixed tasks can use
  different lanes without relabeling their parent ledger.
- Fast Terra/DeepSeek Flash, everyday Sol and demanding Astra preferences apply
  before candidate truncation, respecting capabilities, roles, quotas and catalog support.
- Tests/replay filter file-tool-only providers before discovery. Profile-bearing plans
  bind assessments; retry inherits the item profile and changed accepted inputs replan.
- Automatic effort is capped at xhigh, never max/ultra; failed acceptance can escalate,
  transport/infrastructure failures alone cannot. Explicit user choices remain supported.
- Validation: 299 tests, independent review and installed-CLI replay of local model
  catalogs across fast execution/daily implementation/architecture/file-only cases.
  No classifier/provider inference or live latency benchmark was run.

## delegate 0.19.0
- Complete ordinary review text instead of an 8k preview; larger results have exact
  fingerprinted pages and ready-to-use next_result_call. UTF-8/escaping-aware strict
  JSON budgets prevent transport slicing; collected only after all terminal text ranges.
- notify=true / action=watch retain required collection while leased, process-verified
  completion waiters can let recognized persistent sessions idle. Dead/stale/unknown
  and one-shot modes retain Stop checks; terminal results still need collection.
- Managed CLI wait has no 50-second total cap, returns a completion notice without
  collecting unseen output, and integrates with host background-task facilities.
- Repaired partial-result collection markers from older MCP servers; required tasks
  remain open even if the Stop reminder reaches its loop cap before full delivery.
- Validation: 284 tests, independent review and a 55-second model-free program wait
  under a simulated persistent host. An 86,546-character twelve-finding report was
  reconstructed exactly through four pages. Real IDE wake-up was not exercised.

## delegate 0.18.0
- Fixed-role model/effort auto selects within that role/provider, retaining explicit
  choices, complexity/retry floors and catalog order without changing session mode.
- Catalog exposes global roles and per-provider allowlists; agent_name resolves the
  selected role's provider instead of silently showing only Codex model choices.
- Operation schema enumerates read/write, old read-only labels normalize safely,
  invalid values explain the allowed set before task registration.
- Optional selected source_inputs snapshots carry staged/unstaged/untracked drafts
  and tracked deletions into isolated builder worktrees without modifying source
  files, HEAD or index. Bounded/no-follow file capture, hashes and scoped commits.
- Validation: 268 tests, syntax/annotation review, installed simulated builder with
  exact source/index preservation, and one real company DeepSeek Flash/low Read
  review with result collection and recoverable archival. Official inference and
  a full production review workload were not exercised for this change.

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

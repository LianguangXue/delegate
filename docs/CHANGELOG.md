# Release notes

This publication bundles the changes since the earlier 0.7.1 snapshot. Version
numbers below describe implementation milestones, not separately published Git tags.

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

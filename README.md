# Delegate for Claude Code

An opt-in Claude Code plugin for delegating bounded tasks to Codex, Claude CLI,
and configured Anthropic-compatible providers, including DeepSeek. Version 0.6.0.

## Install

```sh
claude plugin marketplace add LianguangXue/delegate
claude plugin install delegate@local-delegation --scope user
```

Restart Claude Code after upgrades so its MCP server and hooks use the same version.
New sessions start with delegation off. Role definitions persist; activation is
session-scoped. Native Claude agents remain available independently.

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

Write roles require a clean Git source and isolated worktrees, retained for review.
Nothing automatically merges or publishes changes. Claude/API workers use restricted
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

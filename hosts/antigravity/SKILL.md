---
name: delegate
description: Delegate bounded work from Antigravity to Codex, DeepSeek, Claude Code, or Antigravity subagents with zero-token advisory routing, parent-controlled sandbox permissions, and durable results. Activate when dispatching substantial coding, review, architecture second opinions, or verification work.
---

Use the delegate MCP server (`delegate_*` tools). On first use in a conversation, call
`delegate_host` with this conversation's exact ID from `<user_information>` (or omit to pick up
`$ANTIGRAVITY_CONVERSATION_ID`):

`delegate_host(native_session_id="<Conversation ID>")`

Keep the returned `session_id` (`antigravity-...`) for every delegate tool call in this
conversation. Never share a `session_id` across different conversations. Reconnects can
re-register the same `native_session_id`.

Fast CLI one-liners (also usable as fallback if MCP tools are not loaded in an older session):
- Zero-token dispatch check: `delegate-agent advise "task" --host antigravity [--workflow gate-review] [--files a.py] [--lines 30]`
- One-line worker ask: `delegate-agent ask <codex|codex-review|codex-build|claude-review|claude-build|deepseek|agy> "task" --host antigravity [--blind] [--review-gate]`
- Raw JSON-RPC fallback: `delegate-agent call antigravity <<'JSON'` with `{"name":"...","arguments":{...}}`.

## Zero-Token Dispatch Rules & 4×4 Cross-Platform Matrix

Any host (`claude`, `codex`, `antigravity`/`agy`, `dsh`) can orchestrate all 4 worker backends:
- **Inline break-even gate (`estimated_lines<=40`)**: keep <=40-line edits, quick review fixes, UX/copy tweaks, and tightly coupled UI<->API changes inline in Antigravity so handoff cost never exceeds the work.
- **Blind architecture second opinion (`workflow="dual-plan"`, `blind_context=true` / `--blind`)**: since parent is already Antigravity/Gemini, pair big architecture plans with `claude-reviewer` or `codex-reviewer` passing ONLY raw user intent + verified facts (never your draft); verify differences against code.
- **Frontend visual polish (`workflow="frontend"`)**: delegate high-craft UI/visual implementation to `claude-builder` (`sonnet`) when Claude quota permits, while polishing natural copy in Antigravity.
- **Frozen-interface backend/algo (`workflow="backend-module"`)**: dispatch self-contained backend, algorithm, data, or debugging modules with frozen interfaces to `codex-builder` (`gpt-6.1-terra` `low` simple; `gpt-6.2-sol`/`gpt-6.1-sol` `medium`/`high` standard; `astra` strictly complex-only with `escalate=true`).
- **Pre-merge gate review (`workflow="gate-review"`, `review_gate=true` / `--review-gate`)**: run `codex-reviewer` on `gpt-6.2-sol` (`gpt-6.1-sol` fallback) at `effort="high"` (`effort="xhigh"` ONLY on sensitive auth/login/token/permission/money/external-message/publish paths). Enforce 2-tier `BLOCKING` vs `ADVISORY` severity and **stop after max 2 review rounds** (escalate remaining disputes to the user).
- **Cheap bulk work (`workflow="bulk-cheap"`)**: DeepSeek roles default to `deepseek-flash` across all tiers (avoid `deepseek-v4-pro` unless explicitly requested); `claude-explorer` uses `haiku`.
- **Antigravity subagents** (`agy-advisor` read-only, `agy-builder` worktree): opt-in (`automatic=false`) roles backed by `agy -p`.

## Available Subagent Roles (Codex, DeepSeek, Claude Code)

Inspect live roles/models anytime via `delegate_catalog(agent_name=...)` or
`delegate_control(action="status", session_id=SID)`:

- **Codex** (`codex-reviewer` read-only, `codex-builder` isolated worktree + shell):
  - `simple` (clear, bounded, low-risk tasks, including clear test runs): `gpt-6.1-terra` (`low`).
  - `standard` (routine/cross-module code, debugging, reviews, `replay` validation): `gpt-6.1-sol`
    (`medium` or `high` effort).
  - **STRICT QUOTA GUARD — `astra` (`complex` only)**: reserve `gpt-*-astra` strictly for
    genuinely `complex` tasks (`kind="architecture"`, compound `risk="high"` + `uncertainty="high"`,
    `scope="system"` with high risk/uncertainty, or `risk="high"` without automated test/replay
    validation), passing `escalate=true` under `sol-first`. Never pick `astra` casually or
    inflate `task_profile` on routine cross-module bugs, reviews, or test runs.
- **DeepSeek** (`deepseek-company-reviewer`/`builder`, `deepseek-official-reviewer`/`builder`):
  - **Defaults to `deepseek-flash` across all complexity tiers** (`low`/`medium`/`high`).
    Do NOT request `deepseek-v4-pro` unless the user explicitly asks or `escalate=true` is used
    on genuinely `complex` DeepSeek escalation.
  - DeepSeek/Claude roles default to file tools unless you pass `permissions="workspace-shell"`
    (or `"network-shell"` / `"full-access"`).
- **Claude Code** (`claude-explorer` `haiku` read-only, `claude-reviewer` `sonnet`/`opus` read-only,
  `claude-builder` `sonnet`/`opus` worktree):
  - Check `delegate_quota(provider_name="claude", session_id=SID)` first when choosing Claude
    Code subagents; prefer `claude-explorer` (`haiku`) for quick bounded codebase exploration and
    `claude-reviewer`/`claude-builder` (`sonnet` default, `opus` only when needed) for Claude Code
    tasks. Never fabricate quota percentages.

## Per-Subtask Dispatch & Parent-Controlled Permissions

For substantial independent work, reason once in this turn and pass `task_profile` (`kind`,
`scope`, `uncertainty`, `risk`, `validation`, optional `reasoning`/`requires_shell`/`reason`),
with `complexity` omitted or `"auto"`:

`delegate_run(session_id=SID, agent="auto", model="auto", effort="auto", task_profile=..., permissions=..., writable_roots=..., network_access=..., notify=true, task=..., acceptance=..., rationale=..., cwd=...)`

Or pass an explicit role (`agent="codex-builder"`, `"deepseek-company-builder"`, `"claude-builder"`,
etc.) with `model="auto"` / `effort="auto"` (or explicit model/effort).

Parent controls subagent sandbox and tool scope per subtask (overriding role defaults):
- `permissions`: `"auto"` | `"read-only"` | `"workspace-write"` (edit files in isolated worktree,
  no shell) | `"workspace-shell"` (worktree + shell/Bash) | `"network-shell"` (worktree + shell +
  `networkAccess=true` for GitLab/HTTPS) | `"full-access"` (`dangerFullAccess` / unrestricted host).
- `writable_roots`: `["/path/to/shared-data"]` to authorize shared directories outside `cwd`.
- `network_access`: `true` or `false` explicit sandbox network override (works with `read-only` too).
- Dirty Git repositories need `source_inputs=[...]` or `snapshot_required_files=true` with explicit
  `required_files` for builders.

## Non-Polling Background Wait & Result Collection

- Pass `notify=true` (keep `await_completion=true`) for multi-minute work so `delegate_run` returns
  a receipt immediately.
- Run `delegate-agent wait antigravity` in `run_command` with `WaitMsBeforeAsync=500` (passing
  `{"session_id":"...","job_id":"...","background":true}` on stdin) and wait for the background
  task wakeup — **never poll in a loop**.
- Retrieve full output with `delegate_job(action="result", result_view="auto"|"file", ...)`
  following `next_result_call` until `result_delivery_complete=true`, or export via
  `delegate-agent result JOB --session-id SID --host antigravity --output REPORT.md`.
- Record acceptance with evidence via `delegate_job(action="verify", ...)` and close the ledger
  with `delegate_task(action="finish", accepted_jobs=[...], verdict=..., evidence=...)`.
- For `am` integration, bind this conversation's active `am` task using
  `delegate_handoff(action="bind", session_id=SID, am_task_id="...")`.
- Workers must never recursively invoke `delegate` (`DELEGATE_WORKER=1` is enforced).

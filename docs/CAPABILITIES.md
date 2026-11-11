# Supported surfaces and acceptance boundaries

Delegate implementation: 0.17.1; optional am integration: 0.4.0+. Linux, Python 3.12+.

| Capability | Claude Code parent | Codex parent | DSH parent |
| --- | --- | --- | --- |
| Configured Claude/Codex/Anthropic-compatible workers | Yes | Yes | Yes |
| Model/effort, role, account and quota gates | Yes | Yes | Yes |
| Parent identity | Claude session ID | delegate_host logical ID | delegate_host logical ID |
| Task/result/review, am handoff, archive/resume | Yes | Yes | Yes |
| Scoped local commit on explicit builder authorization | Yes | Yes | Yes |
| Optional unbounded total runtime and runtime inspection | Yes | Yes | Yes |
| Claude Stop/asyncRewake completion hooks | Yes, host dependent | No | No |
| Native parent token accounting in delegate | Transcript-based, partial inputs remain unknown | Unavailable | Unavailable |

Verified local paths include Claude -> company DeepSeek, Codex -> company DeepSeek,
and DSH -> Codex. They establish bounded smoke workflows, not every model/effort,
account or future host version. Native host idle/closed-session wake is not guaranteed.
Use result collection or a supported host background-command waiter. Observed child
tokens exclude unmetered parent cost and are not equivalent quota or money units.

New installs are off until explicitly enabled; a configured default_mode=auto enables
fresh sessions but does not override saved off/manual. Read-only roles, manual-only
accounts, commits and cross-session question budgets retain independent restrictions.
API workers need an Anthropic-compatible interface and Claude CLI; arbitrary
OpenAI-compatible endpoints require another adapter. Workers cannot recursively
use delegate to spawn another layer. Host IDs prevent accidental namespace mixing,
not malicious same-OS-user access.

am imports versioned bounded snapshots and stores project-scoped artifact cards.
It does not claim leases, execute checkpoint payloads, schedule jobs or mark tasks
complete merely from worker output. Receipt observers trust producer receipts;
questions default disabled and queueing never starts a model. Evidence fingerprints
cover named regular files/manifests, not all project dependencies or remote effects.

No credentials, local job/conversation records or machine-specific validation logs
are shipped with the repository. Users configure and verify their own backends.
See [installation](INSTALL.zh-CN.md) and the bundled [model playbook](../plugins/delegate/skills/delegate/references/model-playbook.md).

0.17.0 adds declared GPU/SSH/task-network requirements and per-role execution profiles.
Supported Codex workers check required capabilities under the selected policy before
inference; incompatible file-tool providers are rejected before dispatch. Ordinary
tasks retain their existing sandbox. Host access requires explicit grants and disables
the Codex OS sandbox; it never overrides an outer container or grants a GPU lease.
See [execution requirements](EXECUTION.md) for configuration, examples and check limits.

0.13.0 runtime additions: frozen server/worker source fingerprints, redacted bounded
Codex stderr/exit diagnostics, owned process/command/log inspection, canonical state paths,
and finite-deadline checkpoint steering. New Codex threads expose a dynamic condition-wait
tool: no inference while that tool is pending, with cancellation/heartbeat handled in code.
Legacy threads cannot acquire new tools by resume; existing MCP servers need restart.
These controls do not detach sandbox computation, restore lost results, guarantee checkpoints,
or establish aggregate subscription savings. See [runtime details](RUNTIME.zh-CN.md).

0.14.0 separates workspace preparation from model attempts. Workspace Git commands
default to 600 seconds (configurable), remain cancellable, and leave exact recovery
receipts after timeout. Clean verified completed checkouts can be reused; cleanup
preserves changed/untracked/ignored artifacts and foreign locks. Proven pre-inference
failures do not use model retry slots or quality samples; unknown inference still counts,
and parent coordination cost remains included. Current CLI can serve the same session
when its host defers MCP reload; no automatic replay or unsafe hot replacement occurs.

0.15 adds direct background dispatch, redacted owned-child command lines in shutdown
results, and bounded advisory Python test-dependency checks. These checks never execute
project code or tests, copy files or broaden commit scope. A static passed result is
not proof that a clean checkout or external dependencies were executed successfully.
Old records cannot recover argv that was never captured.

0.16 exposes numeric timeout bounds, additive in-flight extensions and worker-read
acknowledgements. Dynamic deadline updates do not extend fixed per-RPC timeouts or
revive an interrupted backend. Legacy live-control workers can remain unconfirmed;
pre-control workers reject unsafe hot changes instead of pretending to apply them.

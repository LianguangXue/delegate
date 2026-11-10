# Supported surfaces and acceptance boundaries

Current versions: delegate 0.12.0; optional am 0.4.0. Linux, Python 3.12+.

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

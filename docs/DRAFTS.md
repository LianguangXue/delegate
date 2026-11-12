# Draft files, role discovery and model selection (0.18)

## Discover the selected role's models

`delegate_catalog()` defaults to Codex **model discovery**, but its `agents` list
contains all configured roles with provider mappings. `selected_provider_agents`
contains only roles for the selected model catalog. `configured_providers` includes
configured model/effort allowlists; these are not proof of live inference availability.

To query DeepSeek directly, use either:

```text
delegate_catalog(agent_name="deepseek-company-reviewer")
delegate_catalog(provider_name="deepseek-company")
```

Company and official accounts stay distinct. A disabled generic `deepseek` provider
does not disable a separately configured `deepseek-company` provider. Role names are
configuration, not built-in aliases: use the exact names returned by the catalog.

## Auto model/effort within a fixed role

A concrete agent now supports `model="auto"`, `effort="auto"`, or either separately:

```text
delegate_run(agent="deepseek-company-reviewer", model="auto", effort="auto",
  category="review", complexity="standard", operation="read",
  work_item="rubric-review", ...)
```

Selection remains inside that role/provider's supported model/effort lists. It uses
task complexity, catalog order and model-family heuristics, respecting retry floors.
It does not select another account, enable a disabled role or change session mode.
An off/manual session still needs an explicit one-shot request. Explicit choices stay
authoritative. Complex auto needs a frontier-family model and high reasoning; absent
capacity returns an error instead of silently downgrading. These are capability
heuristics, not provider benchmarks or guaranteed cheapest billing.

`operation` has two canonical values, exposed as a tool-schema enum:

- `read`: review/analyze without edits.
- `write`: edits in an isolated worktree.

It is not a task description. Old `read-only`, `review`, and `read-only review`
values normalize to `read` on the server/CLI. Other invalid values get an explicit
allowed-values error before planning/task registration. Token-estimate errors are
reported separately.

## Let builders work on uncommitted documents

Without extra inputs, builders still require a clean source and work from its HEAD.
For draft documentation or code, select exact **repository-root-relative files**:

```text
delegate_run(agent="codex-builder", model="auto", effort="auto",
  category="documentation", complexity="standard", operation="write",
  source_inputs=["README.md", "doc/DESIGN.md", "doc/PLAN.md", "doc/PROGRESS.md"],
  task="Synchronize these draft documents and report the changes.",
  acceptance="Keep the stated rules consistent; report conflicts without weakening them.", ...)
```

The worker captures the selected current contents/deletions at **launch**, creates a
normal detached worktree at the recorded HEAD, then overlays those files. Staged and
unstaged contents resolve to the current on-disk version. Selected untracked drafts
are included. Other dirty files stay at HEAD; this is not an entire-source snapshot.
Paths remain relative to the repository root even when `cwd` is a subdirectory.

The source files, HEAD, index and stash remain untouched. No automatic merge or
write-back occurs. Parents can review the builder's synchronization diff rather than
doing the document edits themselves; integration remains a reviewed parent action.
Use `source_snapshot` in receipts/results to inspect selected paths, hashes, modes,
deletions and baseline commit. Compare delivered edits against those inputs as well
as HEAD, since a HEAD diff also contains inherited draft edits.

Limits: 64 exact files, 1 MiB per file, 8 MiB total. Directories are not traversed;
ignored files, symlinks, special files and known credential paths are rejected.
Path-based credential filters cannot establish that arbitrary source text has no
secret: select only the intended source/documents. Missing paths are errors unless
they are deletions of selected files tracked at the recorded HEAD. Source merge
conflicts and edits detected during capture block setup before inference.

Resume continues the existing snapshot/worktree and does not recopy newer source
drafts. Start a fresh task for a different input set. Read-only reviewers already
see current source files and do not need `source_inputs`.

Local commits still require per-attempt `allow_commit`, message and explicit paths.
Only a verified inherited-input baseline may begin dirty. Approved commit paths can
include inherited input edits; other paths remain uncommitted. No implicit push or
merge permission is added.

Existing MCP connections retain their old schemas. Reload/start a session on this
release, or use the current `delegate-agent call` with the **same session_id**.
Do not invent a new work item to bypass failed attempts or drop requested inputs
just to get an older schema to accept the call.

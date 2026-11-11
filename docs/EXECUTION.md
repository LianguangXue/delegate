# Execution requirements and launch preflight (0.17)

Declare the resources the **task's commands** need before dispatch. This is separate
from model selection, model API connectivity and filesystem workspace selection.
Reviewing GPU code or reading an existing result does not itself need a GPU.

Add these fields to an otherwise normal `delegate_run` request:

```json
{
  "requirements": {
    "gpu": true,
    "gpu_devices": ["0"],
    "ssh": false,
    "network": false
  },
  "execution_profile": "sandboxed"
}
```

Device `0` is only a format example. Use an existing, authorized allocation; never
guess devices or reuse a reservation that has been released. The plugin does not
reserve GPUs or interpret a declaration as a scheduler lease. Local `gpu` means the
Codex execution host, not a GPU on a separate SSH destination.

For SSH/network workloads, declare explicit direct TCP endpoints, without credentials:

```json
{
  "requirements": {
    "gpu": false,
    "ssh": true,
    "ssh_targets": [{"host": "compute.internal", "port": 22}],
    "network": true,
    "network_targets": [{"host": "db.internal", "port": 5432}]
  },
  "execution_profile": "network"
}
```

SSH implies command-network access. At least one SSH target is required for SSH;
network needs network targets or SSH targets. These are **probe endpoints**, not an
enforced firewall allowlist. For proxies/jump hosts, declare the directly reachable
relay; the probe does not evaluate HTTP proxies, SSH aliases, ProxyJump or application
transactions. It never logs in, changes known_hosts, executes remote commands or
accepts passwords/private keys in the declaration.

## Profiles and grants

| Profile | Local filesystem | Command network | Selection |
| --- | --- | --- | --- |
| sandboxed | Existing read-only/worktree sandbox | Disabled | Default |
| network | Same filesystem sandbox | Enabled | Per-role explicit grant; auto may select it for declared network |
| host | Codex OS sandbox disabled; current execution host only | Enabled | Explicit per-job selection and role grant; worktree roles only |

Role `workspace=worktree` still creates an isolated Git checkout, but **host is not
filesystem isolation**. It does not escape an outer VM/container, mount devices or
override managed requirements. A read-only role cannot receive host access.
`auto` never selects host. Requirements alone never grant permissions or trigger
automatic escalation after failure.

Roles default to `execution_profiles=["sandboxed"]`. Only configure expanded grants
when the user requests them. A compute-role configuration could include:

```json
{
  "provider": "codex",
  "enabled": true,
  "automatic": false,
  "workspace": "worktree",
  "instructions": "Use only the current approved GPU allocation. Do not override device visibility.",
  "execution_profiles": ["sandboxed", "network", "host"],
  "gpu_devices": ["0"]
}
```

This is an example for `delegate_configure(section="agents", name=..., value=...)`,
not an installation default. Replace the allocation and deliberately choose which
profiles to grant. Optional role `gpu_devices` restricts accepted declarations;
CUDA_VISIBLE_DEVICES is still a visibility hint, **not a device cgroup**. The worker
must not override it. Visible CUDA indices are renumbered from zero.

Claude/Anthropic-compatible workers currently expose file tools only. They reject
GPU/SSH/command-network requirements before model discovery or dispatch; requesting
these resources does not silently switch accounts or enable another provider.

## What happens at launch

1. Validate declarations and role/profile compatibility before catalog/quota calls or
   worker registration. Automatic planning filters incompatible roles and never assumes
   an unverified main agent has the requested resources. Tiny resource checks return
   replan for authorized non-model inspection rather than automatically spending a
   worker inference call.
2. Prepare the normal workspace. A returned receipt may show preflight `pending`;
   it is not a claim that resources are already usable.
3. Recheck current grants. Under the **same selected Codex sandbox policy**, run a
   fixed isolated stdlib probe with `command/exec`, before account/thread/model-turn
   operations. Respect managed allowed sandbox modes. No project imports or arbitrary
   user preflight commands are executed.
4. Persist `execution_environment.preflight`. A blocked check starts no model turn;
   it is infrastructure failure, not a model retry/quality failure. A newly prepared,
   clean owned worktree may be removed without force; uncertain/changed artifacts and
   resumed worktrees remain preserved. An invalid runtime image cannot trigger cleanup.

Ordinary undeclared CPU/file tasks retain the existing sandbox and do not run hardware
or network probes. `declared=false` and `not_requested` mean availability is unknown,
not verified. Resume preserves the original profile/requirements; changing them needs
a fresh explicit task/handoff. Role revocation before launch blocks expanded access.

## Check coverage

- GPU: verify the selected visibility reaches the command environment, then load the
  CUDA driver, call cuInit and enumerate visible devices. No contexts, kernels or VRAM
  allocations are requested. A successful check does not verify a framework, free VRAM,
  a scheduler lease or the workload itself. A missing mask blocks before CUDA init.
- Network: direct TCP connection to each declared probe endpoint, with bounded timeouts.
  TLS, HTTP, database credentials and business operations are not checked.
- SSH: executable presence and the SSH endpoints' TCP checks. Authentication, SSH
  configuration, forwarding and host-key validation are explicitly not checked.

Receipts, results and inspect expose the requested resources, selected profile,
filesystem/network scope and check outcomes. Preflight consumes local process/I/O
resources and can touch explicitly declared GPU drivers/endpoints, but makes no model
inference request. Job startup timeouts and fixed RPC timeouts still apply. The driver
probe does not authorize using a reserved device.

Old MCP schemas may not accept the new fields. Use the installed `delegate-agent call`
with the same session ID (`call codex` / `call dsh` for native parents), or reload/start
a session on the current release. Never drop declarations or silently broaden grants
just to get a request through.

References: [OpenAI command execution](https://developers.openai.com/codex/app-server),
[sandbox permissions](https://learn.chatgpt.com/docs/permissions),
[NVIDIA driver initialization](https://docs.nvidia.com/cuda/cuda-driver-api/cuda_driver_api/group__CUDA__INITIALIZE.html),
[driver context separation](https://docs.nvidia.com/cuda/cuda-programming-guide/03-advanced/driver-api.html).

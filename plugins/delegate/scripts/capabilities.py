"""Declared execution needs, explicit sandbox grants and pre-inference checks."""
from pathlib import Path
from typing import Any
import copy
import json
import os
import re
import subprocess
import sys
import time
import storage as st
import diagnostics

PROFILES = {"sandboxed", "network", "host"}
PERMISSIONS = {
  "read-only", "workspace-write", "workspace-shell", "network-shell", "full-access",
}
PERMISSION_CHOICES = [
  "auto", "read-only", "workspace-write", "workspace-shell", "network-shell", "full-access",
]
_PERMISSION_ALIASES = {
  "auto": "auto",
  "read": "read-only",
  "readonly": "read-only",
  "read-only": "read-only",
  "plan": "read-only",
  "dontask": "read-only",
  "write": "workspace-write",
  "edit": "workspace-write",
  "acceptedits": "workspace-write",
  "accept-edits": "workspace-write",
  "file-write": "workspace-write",
  "workspace-write": "workspace-write",
  "shell": "workspace-shell",
  "bash": "workspace-shell",
  "bypass": "workspace-shell",
  "bypasspermissions": "workspace-shell",
  "bypass-permissions": "workspace-shell",
  "workspace-shell": "workspace-shell",
  "network": "network-shell",
  "net": "network-shell",
  "network-shell": "network-shell",
  "workspace-network": "network-shell",
  "gitlab": "network-shell",
  "full": "full-access",
  "full-access": "full-access",
  "host": "full-access",
  "danger-full-access": "full-access",
  "unrestricted": "full-access",
}
_PERMISSION_ERR = (
  "permissions must be auto/read-only/workspace-write/"
  "workspace-shell/network-shell/full-access"
)


def normalize_permissions(permissions: str | None = "auto") -> str:
  """Normalize and validate an optional subagent permission tier.

  Args:
    permissions: Requested tier name, alias, ``auto``, or ``None``.

  Returns:
    Canonical permission string in ``PERMISSION_CHOICES``.

  Raises:
    ValueError: If ``permissions`` is not a recognized tier or alias.
  """
  if permissions is None or permissions == "":
    return "auto"
  if not isinstance(permissions, str):
    raise ValueError(_PERMISSION_ERR)
  normalized = _PERMISSION_ALIASES.get(permissions.strip().lower())
  if normalized is None:
    raise ValueError(_PERMISSION_ERR)
  return normalized


def normalize_writable_roots(
  writable_roots: list[str] | None,
  base_cwd: str = "",
) -> list[str]:
  """Validate and canonicalize extra writable directories outside ``cwd``.

  Args:
    writable_roots: Optional list of existing directory paths.
    base_cwd: Base directory used to resolve relative entries.

  Returns:
    Deduplicated list of resolved absolute directory paths.

  Raises:
    ValueError: If entries are invalid, non-directories, or filesystem root.
  """
  if writable_roots is None:
    return []
  if not isinstance(writable_roots, list) or len(writable_roots) > 16:
    raise ValueError("writable_roots must be a list of at most 16 directory paths")
  base = Path(base_cwd).resolve() if base_cwd else None
  resolved_list: list[str] = []
  for raw in writable_roots:
    if (not isinstance(raw, str) or not raw.strip() or len(raw) > 4096
        or any(c in raw for c in ("\0", "\n", "\r"))):
      raise ValueError("writable_roots contains an invalid directory path")
    candidate = Path(raw).expanduser()
    if base and not candidate.is_absolute():
      candidate = base / candidate
    resolved = Path(os.path.abspath(candidate))
    if not resolved.exists():
      raise ValueError("writable_roots directory does not exist: " + str(resolved))
    if not resolved.is_dir():
      raise ValueError("writable_roots must point to an existing directory: " + str(resolved))
    resolved = resolved.resolve()
    if resolved == Path("/"):
      raise ValueError("writable_roots cannot be '/'; use permissions='full-access' instead")
    resolved_list.append(str(resolved))
  return list(dict.fromkeys(resolved_list))


def normalize_network_access(network_access: bool | None) -> bool | None:
  """Validate optional explicit network access override.

  Args:
    network_access: Explicit boolean flag or ``None`` to follow permissions/profile.

  Returns:
    Normalized boolean or ``None``.

  Raises:
    ValueError: If ``network_access`` is not bool or None.
  """
  if network_access is None:
    return None
  if type(network_access) is not bool:
    raise ValueError("network_access must be boolean or null")
  return network_access


def is_unrestricted_fs(execution: dict[str, Any] | None) -> bool:
  """Return True only when execution allows host-wide filesystem writes outside ``cwd``.

  Args:
    execution: Resolved execution environment dictionary.

  Returns:
    True for ``full-access`` permissions or ``host`` profile, else False.
  """
  if not isinstance(execution, dict):
    return False
  return (
    execution.get("permissions") == "full-access"
    or execution.get("profile") in {"host", "full-access"}
  )


def resolve_permissions(
  role: dict[str, Any],
  provider: dict[str, Any],
  permissions: str | None = "auto",
  writable_roots: list[str] | None = None,
  base_cwd: str = "",
) -> tuple[str, str]:
  """Resolve effective subagent permissions and workspace mode.

  Args:
    role: Configured agent role dictionary.
    provider: Configured provider dictionary.
    permissions: Parent-requested permission tier, or ``auto`` to use role default.
    writable_roots: Optional extra writable directories requested by parent.
    base_cwd: Optional base directory for resolving relative ``writable_roots``.

  Returns:
    Tuple of ``(effective_permissions, effective_workspace)``.

  Raises:
    ValueError: If ``permissions``, ``role["permissions"]``, or ``writable_roots`` conflicts.
  """
  roots = normalize_writable_roots(writable_roots, base_cwd=base_cwd)
  norm = normalize_permissions(permissions)
  is_codex = provider.get("kind", "codex") == "codex"
  if norm == "auto":
    if role.get("permissions") is not None:
      eff = normalize_permissions(role["permissions"])
      if eff not in PERMISSIONS:
        raise ValueError(_PERMISSION_ERR)
    elif role.get("workspace") == "read-only":
      eff = "read-only"
    else:
      eff = "workspace-shell" if is_codex else "workspace-write"
    if eff == "read-only" and roots:
      eff = "workspace-shell" if is_codex else "workspace-write"
  else:
    eff = norm
  if eff == "read-only" and roots:
    raise ValueError("writable_roots cannot be combined with permissions='read-only'")
  workspace = "read-only" if eff == "read-only" else "worktree"
  return eff, workspace


def effective_role(
  role: dict[str, Any],
  provider: dict[str, Any],
  permissions: str | None = "auto",
  writable_roots: list[str] | None = None,
  base_cwd: str = "",
) -> dict[str, Any]:
  """Return a copy of ``role`` with parent-resolved workspace and permissions applied.

  Args:
    role: Configured agent role dictionary.
    provider: Configured provider dictionary.
    permissions: Parent-requested permission tier or ``auto``.
    writable_roots: Optional extra writable directories requested by parent.
    base_cwd: Optional base directory for resolving relative ``writable_roots``.

  Returns:
    Role dictionary with ``workspace`` and ``permissions`` resolved.

  Raises:
    ValueError: If ``permissions``, ``role["permissions"]``, or ``writable_roots`` is invalid.
  """
  eff, workspace = resolve_permissions(
    role, provider, permissions, writable_roots=writable_roots, base_cwd=base_cwd,
  )
  return {**copy.deepcopy(role), "workspace": workspace, "permissions": eff}


def input_schema() -> dict[str, Any]:
  """Describe declarations precisely so callers do not guess field types or credentials."""
  target = {"type": "object", "properties": {"host": {"type": "string", "maxLength": 253},
    "port": {"type": "integer", "minimum": 1, "maximum": 65535}}, "required": ["host", "port"],
    "additionalProperties": False}
  return {"anyOf": [{"type": "null"}, {"type": "object", "properties": {
    "gpu": {"type": "boolean", "description": "Local execution-host CUDA access, not merely reviewing GPU code"},
    "ssh": {"type": "boolean"}, "network": {"type": "boolean", "description": "Task-command network, not provider inference traffic"},
    "gpu_devices": {"type": "array", "maxItems": 16, "items": {"type": "string"},
                    "description": "Explicit already-allocated indices/UUIDs, e.g. string 0; not a resource lease"},
    "ssh_targets": {"type": "array", "maxItems": 4, "items": target},
    "network_targets": {"type": "array", "maxItems": 4, "items": target}}, "additionalProperties": False}]}


def validate_profile(execution_profile: str) -> None:
  """Reject invalid profile names before any provider discovery or resource probe."""
  if not isinstance(execution_profile, str) or execution_profile not in PROFILES | {"auto"}:
    raise ValueError("execution_profile must be auto/sandboxed/network/host")


def normalize(requirements: dict[str, Any] | None) -> dict[str, Any]:
  """Validate a bounded declaration; targets contain no users, passwords or URLs."""
  data = requirements if requirements is not None else {}
  if not isinstance(data, dict) or set(data) - {"gpu", "ssh", "network", "gpu_devices", "ssh_targets", "network_targets"}:
    raise ValueError("requirements supports gpu/ssh/network booleans, gpu_devices and ssh_targets/network_targets")
  result: dict[str, Any] = {}
  for name in ("gpu", "ssh", "network"):
    value = data.get(name, False)
    if type(value) is not bool:
      raise ValueError("requirements." + name + " must be boolean")
    result[name] = value
  devices = data.get("gpu_devices", [])
  if (not isinstance(devices, list) or len(devices) > 16 or any(not isinstance(d, str) or len(d) > 128 or
      not re.fullmatch(r"(?:[0-9]{1,4}|(?:GPU|MIG)-[A-Za-z0-9_/-]+)", d) for d in devices)):
    raise ValueError('gpu_devices must list at most 16 CUDA index/UUID strings (e.g. ["0"]); all/-1 are not allocations')
  result["gpu_devices"] = list(dict.fromkeys(devices))
  if result["gpu"] != bool(devices):
    raise ValueError("gpu=true requires explicit allocated gpu_devices; gpu_devices requires gpu=true")
  for name, needed in (("ssh_targets", result["ssh"]), ("network_targets", result["network"])):
    targets = data.get(name, [])
    if not isinstance(targets, list) or len(targets) > 4:
      raise ValueError(name + " must contain at most four host/port objects")
    normalized = []
    for target in targets:
      if (not isinstance(target, dict) or set(target) != {"host", "port"}
          or not isinstance(target["host"], str) or len(target["host"]) > 253
          or not re.fullmatch(r"[A-Za-z0-9_.:-]+", target["host"])
          or type(target["port"]) is not int or not 1 <= target["port"] <= 65535):
        raise ValueError(name + " entries require a plain host/IP and integer port 1..65535, without credentials")
      if target not in normalized:
        normalized.append(copy.deepcopy(target))
    if normalized and not needed:
      raise ValueError(name + " requires its corresponding capability=true")
    result[name] = normalized
  if result["ssh"] and not result["ssh_targets"]:
    raise ValueError("ssh=true requires explicit ssh_targets for launch-time TCP checks; no SSH login is attempted")
  if result["network"] and not result["network_targets"] and not result["ssh_targets"]:
    raise ValueError("network=true requires network_targets (or declared ssh_targets) for reachability checks")
  result["network"] = result["network"] or result["ssh"]
  return result


def select(
  role: dict[str, Any],
  provider: dict[str, Any],
  requirements: dict[str, Any] | None,
  execution_profile: str = "auto",
  permissions: str | None = "auto",
  writable_roots: list[str] | None = None,
  network_access: bool | None = None,
) -> dict[str, Any]:
  """Resolve permitted execution only; honor explicit parent sandbox grants.

  Args:
    role: Configured agent role dictionary.
    provider: Configured provider dictionary.
    requirements: Optional declared GPU, SSH, or network requirements.
    execution_profile: Requested sandbox profile (``auto``/``sandboxed``/``network``/``host``).
    permissions: Requested subagent permission tier or ``auto`` for role default.
    writable_roots: Optional list of shared writable directory paths outside ``cwd``.
    network_access: Optional explicit network boolean override.

  Returns:
    Resolved execution environment dictionary.

  Raises:
    ValueError: If requested profile, permissions, roots, or hardware grants are disallowed.
  """
  needed = normalize(requirements)
  validate_profile(execution_profile)
  norm_roots = normalize_writable_roots(writable_roots)
  net_req = normalize_network_access(network_access)
  eff_perms, eff_workspace = resolve_permissions(
    role, provider, permissions, writable_roots=norm_roots,
  )
  allowed = role.get("execution_profiles", ["sandboxed"])
  if not isinstance(allowed, list) or any(not isinstance(p, str) or p not in PROFILES for p in allowed):
    raise ValueError("Role has invalid execution_profiles")
  is_codex = provider.get("kind", "codex") == "codex"
  if net_req is False and (
    eff_perms in {"network-shell", "full-access"}
    or execution_profile in {"network", "host"}
    or needed["network"]
    or (not is_codex and eff_perms == "workspace-shell")
  ):
    raise ValueError("network_access=false conflicts with requested network or shell bypassPermissions execution")
  if not is_codex and eff_perms in {"workspace-shell", "network-shell", "full-access"}:
    if (eff_perms == "workspace-shell" and net_req is not True
        and execution_profile not in {"auto", "sandboxed"} and execution_profile not in allowed):
      raise ValueError(
        "Execution profile '" + execution_profile + "' is not permitted for this role; allowed: "
        + ", ".join(allowed) + ". Configure a suitable role explicitly; requirements do not grant permissions."
      )
    if eff_perms == "network-shell" and execution_profile == "host" and "host" not in allowed:
      raise ValueError(
        "Execution profile 'host' is not permitted for this role; allowed: "
        + ", ".join(allowed) + ". Pass permissions='full-access' or configure role explicitly."
      )
    selected = "full-access" if eff_perms == "full-access" else "workspace-shell"
  elif not is_codex:
    if (net_req is True or any(needed[k] for k in ("gpu", "ssh", "network"))
        or execution_profile not in {"auto", "sandboxed"}):
      raise ValueError(
        "This provider uses file-only tools and cannot execute GPU/SSH/network commands; "
        "pass permissions='workspace-shell', 'network-shell', or 'full-access', "
        "or choose a permitted Codex shell role"
      )
    selected = "file-tools"
  elif eff_perms == "full-access":
    if execution_profile in {"sandboxed", "network"}:
      raise ValueError("permissions='full-access' conflicts with restricted execution_profile")
    selected = "host"
  elif eff_perms == "network-shell":
    if execution_profile == "sandboxed":
      raise ValueError("permissions='network-shell' conflicts with execution_profile='sandboxed'")
    if execution_profile == "host":
      if "host" not in allowed:
        raise ValueError(
          "Execution profile 'host' is not permitted for this role; allowed: "
          + ", ".join(allowed) + ". Pass permissions='full-access' or configure role explicitly."
        )
      selected = "host"
    else:
      selected = "network"
  elif net_req is True:
    if execution_profile == "sandboxed":
      raise ValueError("network_access=true conflicts with execution_profile='sandboxed'")
    if execution_profile == "host":
      if "host" not in allowed:
        raise ValueError(
          "Execution profile 'host' is not permitted for this role; allowed: "
          + ", ".join(allowed) + ". Configure a suitable role explicitly; requirements do not grant permissions."
        )
      if eff_workspace != "worktree":
        raise ValueError("host execution requires a worktree role; it cannot preserve a read-only role's OS boundary")
      selected = "host"
    else:
      selected = "network"
  else:
    selected = ("network" if needed["network"] else "sandboxed") if execution_profile == "auto" else execution_profile
    if selected not in allowed:
      raise ValueError(
        "Execution profile '" + selected + "' is not permitted for this role; allowed: "
        + ", ".join(allowed) + ". Configure a suitable role explicitly; requirements do not grant permissions."
      )
    if selected == "host" and eff_workspace != "worktree":
      raise ValueError("host execution requires a worktree role; it cannot preserve a read-only role's OS boundary")
    if needed["network"] and selected == "sandboxed":
      raise ValueError("Declared SSH/network requires the role-permitted network profile, or explicit host profile")
  if (needed["gpu"] and role.get("gpu_devices") is not None
      and not set(needed["gpu_devices"]) <= set(role["gpu_devices"])):
    raise ValueError("Requested GPU devices are outside this role's configured allocation allowlist")
  required = any(needed[k] for k in ("gpu", "ssh", "network"))
  sandbox_mode = (
    None if selected in {"file-tools", "workspace-shell", "full-access"}
    else "danger-full-access" if selected == "host"
    else "read-only" if eff_workspace == "read-only" else "workspace-write"
  )
  claude_mode = None if is_codex else (
    "dontAsk" if eff_perms == "read-only"
    else "acceptEdits" if eff_perms == "workspace-write" else "bypassPermissions"
  )
  network_enabled = selected in {"network", "host", "workspace-shell", "full-access"}
  filesystem_isolation = (
    None if selected == "file-tools"
    else (selected not in {"host", "workspace-shell", "full-access"})
  )
  return {
    "profile": selected,
    "permissions": eff_perms,
    "workspace": eff_workspace,
    "writable_roots": norm_roots,
    "network_access_requested": net_req,
    "shell_access": is_codex or eff_perms in {"workspace-shell", "network-shell", "full-access"},
    "claude_permission_mode": claude_mode,
    "requirements": needed,
    "declared": requirements is not None,
    "sandbox_mode": sandbox_mode,
    "network_access": network_enabled,
    "filesystem_isolation": filesystem_isolation,
    "network_scope": "Declared targets are probes, not an enforced destination allowlist",
    "preflight": {"status": "pending" if required or selected in {"network", "host"} else "not_requested"},
    "gpu_assignment_isolation": "CUDA_VISIBLE_DEVICES hint, not a device cgroup or scheduler lease",
  }


def from_job(job: dict[str, Any]) -> dict[str, Any]:
  """Use immutable dispatch intent; old ordinary jobs retain their original policy."""
  stored = job.get("execution_environment")
  perms = (stored or {}).get("permissions") or job.get("permissions", "auto")
  base_role = job.get("configured_role") or job["role"]
  if not stored:
    return select(
      job["role"], job["provider"], None,
      permissions=perms, writable_roots=job.get("writable_roots"),
    )
  roots = stored.get("writable_roots", job.get("writable_roots"))
  net_req = stored.get("network_access_requested")
  if stored["profile"] in {"file-tools", "workspace-shell"}:
    prof = "sandboxed"
  elif stored["profile"] == "full-access" or perms == "full-access":
    prof = "auto"
  elif (perms == "network-shell" or net_req is True) and stored["profile"] == "network":
    prof = "auto"
  else:
    prof = stored["profile"]
  result = select(
    base_role, job["provider"], stored["requirements"], prof,
    permissions=perms, writable_roots=roots, network_access=net_req,
  )
  result["declared"] = stored.get("declared", True)
  return result


def policy(job: dict[str, Any]) -> dict[str, Any]:
  """Generate the identical sandbox policy used by preflight and the model turn."""
  execution = from_job(job)
  if execution["profile"] == "host":
    return {"type": "dangerFullAccess"}
  if execution["workspace"] == "read-only":
    return {"type": "readOnly", "networkAccess": execution["network_access"]}
  roots = list(dict.fromkeys([job["cwd"], *execution.get("writable_roots", [])]))
  return {"type": "workspaceWrite", "writableRoots": roots, "networkAccess": execution["network_access"]}


def environment(job: dict[str, Any]) -> dict[str, str]:
  """Pass only selected device visibility; it never grants or reserves a GPU."""
  needed = from_job(job)["requirements"]
  return {"CUDA_VISIBLE_DEVICES": ",".join(needed["gpu_devices"])} if needed["gpu"] else {}


def validate_grant(job: dict[str, Any]) -> None:
  """Recheck expanded/resource grants at launch without touching accounts or hardware."""
  execution = from_job(job)
  current = st.config()
  role = current["agents"].get(job["agent"])
  provider = current["providers"].get(job["provider_name"])
  expected_role = job.get("configured_role") or job["role"]
  if (not role or not role.get("enabled") or role.get("provider") != job["provider_name"]
      or role.get("workspace") != expected_role["workspace"] or not provider or not provider.get("enabled")
      or provider.get("kind") != job["provider"]["kind"] or provider.get("command") != job["provider"].get("command")):
    raise ValueError("Execution grant changed before launch")
  if execution["profile"] in {"file-tools", "workspace-shell"}:
    prof = "sandboxed"
  elif execution["profile"] == "full-access" or execution.get("permissions") == "full-access":
    prof = "auto"
  elif (execution.get("permissions") == "network-shell"
        or execution.get("network_access_requested") is True) and execution["profile"] == "network":
    prof = "auto"
  else:
    prof = execution["profile"]
  select(
    role, provider, execution["requirements"], prof,
    permissions=execution.get("permissions", job.get("permissions", "auto")),
    writable_roots=execution.get("writable_roots"),
    network_access=execution.get("network_access_requested"),
  )


# Isolated stdlib interpreter: no project imports, CUDA contexts, kernels or memory
# allocation; no SSH invocation, credential access or remote command execution.
PROBE = r'''
import json, os, shutil, socket, sys
needs=json.loads(sys.argv[1]); checks={}
if needs['gpu']:
  if os.environ.get('CUDA_VISIBLE_DEVICES')!=','.join(needs['gpu_devices']):
    checks['gpu']={'ok':False,'error':'device_visibility_not_propagated','coverage':'CUDA not initialized; review shell environment filters'}
  else:
    try:
      import ctypes
      driver=ctypes.CDLL('libcuda.so.1')
      driver.cuInit.argtypes=[ctypes.c_uint];driver.cuInit.restype=ctypes.c_int
      code=driver.cuInit(0);count=ctypes.c_int()
      if code==0:
        driver.cuDeviceGetCount.argtypes=[ctypes.POINTER(ctypes.c_int)];driver.cuDeviceGetCount.restype=ctypes.c_int
        code=driver.cuDeviceGetCount(ctypes.byref(count))
      checks['gpu']={'ok':code==0 and count.value==len(needs['gpu_devices']), 'driver_code':code,
        'visible_devices':count.value,'coverage':'cuInit/device enumeration; no context, kernels, VRAM or framework check'}
    except (OSError,AttributeError) as error:
      checks['gpu']={'ok':False,'error':type(error).__name__,'coverage':'CUDA driver unavailable under selected policy'}
targets=[]
for target in needs['network_targets']+needs['ssh_targets']:
  if target not in targets:targets.append(target)
connections=[]
for target in targets:
  try:
    with socket.create_connection((target['host'],target['port']),timeout=1.5):pass
    connections.append({**target,'ok':True})
  except OSError as error:connections.append({**target,'ok':False,'error':type(error).__name__})
if needs['network']:checks['network']={'ok':bool(connections) and all(t['ok'] for t in connections),'targets':connections,'coverage':'direct TCP only; no HTTP/auth/service transaction'}
if needs['ssh']:
  ssh_connections=[row for row in connections if {'host':row['host'],'port':row['port']} in needs['ssh_targets']]
  checks['ssh']={'ok':bool(shutil.which('ssh')) and bool(ssh_connections) and all(t['ok'] for t in ssh_connections),'binary_present':bool(shutil.which('ssh')),'targets':ssh_connections,'coverage':'binary + declared TCP endpoints; login/host keys not checked'}
print(json.dumps({'checks':checks}),flush=True)
'''


def _parse_probe_checks(raw: Any, needed: dict[str, Any]) -> dict[str, Any]:
  """Validate capability-probe JSON stdout without trusting arbitrary probe output."""
  if not isinstance(raw, str) or len(raw) > 16384:
    raise ValueError("Invalid probe output")
  checks = json.loads(raw).get("checks")
  expected = {k for k in ("gpu", "ssh", "network") if needed[k]}
  if (not isinstance(checks, dict) or set(checks) != expected
      or any(type(checks[k].get("ok")) is not bool for k in checks)):
    raise ValueError("Invalid capability check schema")
  return checks


def preflight(rpc: Any, job: dict[str, Any]) -> dict[str, Any]:
  """Probe declared resources under the chosen policy before any inference request."""
  execution = from_job(job)
  needed = execution["requirements"]
  required = any(needed[k] for k in ("gpu", "ssh", "network"))
  if not required and execution["profile"] not in {"network", "host"}:
    return {"status": "not_requested", "checks": {}, "inference_calls": 0}
  report: dict[str, Any] = {"status": "blocked", "checks": {}, "checked_at": time.time(), "inference_calls": 0}
  try:
    validate_grant(job)
    requirements = rpc.call("configRequirements/read", timeout=10).get("requirements") or {}
    modes = requirements.get("allowedSandboxModes")
    if modes is not None and execution["sandbox_mode"] not in modes:
      report["reason"] = "Selected execution mode is disallowed by managed Codex requirements"
      return report
    if not required:
      report.update(status="passed", coverage="role grant and managed mode only; no resource availability declared")
      return report
    result = rpc.call("command/exec", {"command": [sys.executable, "-I", "-S", "-B", "-c", PROBE, json.dumps(needed)],
      "cwd": job["cwd"], "sandboxPolicy": policy(job),
      "env": {"CUDA_FORCE_PRELOAD_LIBRARIES": "0"}, "timeoutMs": 15000}, timeout=20)
    if result.get("exitCode") != 0:
      report["reason"] = "Sandbox capability probe failed; no model turn was started"
      report["exit_code"] = result.get("exitCode")
      error = result.get("stderr")
      if isinstance(error, str):
        secrets = getattr(getattr(rpc, "stderr", None), "values", [])
        report["command_error"] = diagnostics.redact(error, secrets if isinstance(secrets, list) else [])[-600:]
      return report
    checks = _parse_probe_checks(result.get("stdout", ""), needed)
    report["checks"] = checks
    blocked = [name for name, row in checks.items() if not row["ok"]]
    report["status"] = "blocked" if blocked else "passed"
    report["blocked"] = blocked
    if blocked:
      report["reason"] = ("Required capabilities failed: " + ", ".join(blocked)
        + "; no model turn was started. No automatic sandbox escalation.")
  except Exception as exc:
    report["reason"] = (((str(exc)[:240] + "; ") if isinstance(exc, ValueError) else type(exc).__name__ + ": ")
      + "capability preflight unavailable; no inference was started")
  return report


def local_preflight(job: dict[str, Any]) -> dict[str, Any]:
  """Probe declared resources locally for non-Codex workspace-shell workers before inference.

  Args:
    job: Job record dictionary containing provider, role, cwd, and execution environment.

  Returns:
    Preflight status dictionary matching the ``preflight`` schema.
  """
  execution = from_job(job)
  needed = execution["requirements"]
  required = any(needed[k] for k in ("gpu", "ssh", "network"))
  if not required:
    return {"status": "not_requested", "checks": {}, "inference_calls": 0}
  report: dict[str, Any] = {"status": "blocked", "checks": {}, "checked_at": time.time(), "inference_calls": 0}
  try:
    validate_grant(job)
    probe_env = {**os.environ, **environment(job), "CUDA_FORCE_PRELOAD_LIBRARIES": "0"}
    proc = subprocess.run(
      [sys.executable, "-I", "-S", "-B", "-c", PROBE, json.dumps(needed)],
      cwd=job["cwd"], env=probe_env, capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
      report["reason"] = "Capability probe failed; no model turn was started"
      report["exit_code"] = proc.returncode
      if isinstance(proc.stderr, str) and proc.stderr:
        report["command_error"] = diagnostics.redact(proc.stderr, diagnostics.secrets(dict(os.environ)))[-600:]
      return report
    checks = _parse_probe_checks(proc.stdout, needed)
    report["checks"] = checks
    blocked = [name for name, row in checks.items() if not row["ok"]]
    report["status"] = "blocked" if blocked else "passed"
    report["blocked"] = blocked
    if blocked:
      report["reason"] = ("Required capabilities failed: " + ", ".join(blocked)
        + "; no model turn was started. No automatic sandbox escalation.")
  except Exception as exc:
    report["reason"] = (((str(exc)[:240] + "; ") if isinstance(exc, ValueError) else type(exc).__name__ + ": ")
      + "capability preflight unavailable; no inference was started")
  return report

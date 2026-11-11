"""Declared execution needs, explicit sandbox grants and pre-inference checks."""
from pathlib import Path
from typing import Any
import copy
import json
import re
import sys
import time
import storage as st
import diagnostics

PROFILES = {"sandboxed", "network", "host"}


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


def select(role: dict[str, Any], provider: dict[str, Any], requirements: dict[str, Any] | None,
           execution_profile: str = "auto") -> dict[str, Any]:
  """Resolve permitted execution only; never probe or silently broaden permissions."""
  needed = normalize(requirements)
  validate_profile(execution_profile)
  selected = ("network" if needed["network"] else "sandboxed") if execution_profile == "auto" else execution_profile
  allowed = role.get("execution_profiles", ["sandboxed"])
  if not isinstance(allowed, list) or any(not isinstance(p, str) or p not in PROFILES for p in allowed):
    raise ValueError("Role has invalid execution_profiles")
  if selected not in allowed:
    raise ValueError("Execution profile '" + selected + "' is not permitted for this role; allowed: " +
      ", ".join(allowed) + ". Configure a suitable role explicitly; requirements do not grant permissions.")
  if selected == "host" and role["workspace"] != "worktree":
    raise ValueError("host execution requires a worktree role; it cannot preserve a read-only role's OS boundary")
  if needed["network"] and selected == "sandboxed":
    raise ValueError("Declared SSH/network requires the role-permitted network profile, or explicit host profile")
  if needed["gpu"] and role.get("gpu_devices") is not None and not set(needed["gpu_devices"]) <= set(role["gpu_devices"]):
    raise ValueError("Requested GPU devices are outside this role's configured allocation allowlist")
  if provider.get("kind", "codex") != "codex":
    if any(needed[k] for k in ("gpu", "ssh", "network")) or selected != "sandboxed":
      raise ValueError("This provider uses file-only tools and cannot execute GPU/SSH/network commands; choose a permitted Codex shell role")
    selected = "file-tools"
  required = any(needed[k] for k in ("gpu", "ssh", "network"))
  return {"profile": selected, "requirements": needed, "declared": requirements is not None,
    "sandbox_mode": None if selected == "file-tools" else "danger-full-access" if selected == "host" else "read-only" if role["workspace"] == "read-only" else "workspace-write",
    "network_access": selected in {"network", "host"}, "filesystem_isolation": None if selected == "file-tools" else selected != "host",
    "network_scope": "Declared targets are probes, not an enforced destination allowlist",
    "preflight": {"status": "pending" if required or selected in {"network", "host"} else "not_requested"},
    "gpu_assignment_isolation": "CUDA_VISIBLE_DEVICES hint, not a device cgroup or scheduler lease"}


def from_job(job: dict[str, Any]) -> dict[str, Any]:
  """Use immutable dispatch intent; old ordinary jobs retain their original policy."""
  stored = job.get("execution_environment")
  if not stored:
    return select(job["role"], job["provider"], None)
  result = select(job["role"], job["provider"], stored["requirements"], stored["profile"] if stored["profile"] != "file-tools" else "sandboxed")
  result["declared"] = stored.get("declared", True)
  return result


def policy(job: dict[str, Any]) -> dict[str, Any]:
  """Generate the identical sandbox policy used by preflight and the model turn."""
  execution = from_job(job)
  if execution["profile"] == "host":
    return {"type": "dangerFullAccess"}
  if job["role"]["workspace"] == "read-only":
    return {"type": "readOnly", "networkAccess": execution["network_access"]}
  return {"type": "workspaceWrite", "writableRoots": [job["cwd"]], "networkAccess": execution["network_access"]}


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
  if (not role or not role.get("enabled") or role.get("provider") != job["provider_name"]
      or role.get("workspace") != job["role"]["workspace"] or not provider or not provider.get("enabled")
      or provider.get("kind") != job["provider"]["kind"] or provider.get("command") != job["provider"].get("command")):
    raise ValueError("Execution grant changed before launch")
  select(role, provider, execution["requirements"], execution["profile"])


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
    raw = result.get("stdout", "")
    if not isinstance(raw, str) or len(raw) > 16384:
      raise ValueError("Invalid probe output")
    checks = json.loads(raw).get("checks")
    expected = {k for k in ("gpu", "ssh", "network") if needed[k]}
    if not isinstance(checks, dict) or set(checks) != expected or any(type(checks[k].get("ok")) is not bool for k in checks):
      raise ValueError("Invalid capability check schema")
    report["checks"] = checks
    blocked = [name for name, row in checks.items() if not row["ok"]]
    report["status"] = "blocked" if blocked else "passed"
    report["blocked"] = blocked
    if blocked:
      report["reason"] = "Required capabilities failed: " + ", ".join(blocked) + "; no model turn was started. No automatic sandbox escalation."
  except Exception as exc:
    report["reason"] = ((str(exc)[:240] + "; ") if isinstance(exc, ValueError) else type(exc).__name__ + ": ") + "capability preflight unavailable; no inference was started"
  return report

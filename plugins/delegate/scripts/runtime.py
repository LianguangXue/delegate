"""Optional wall-clock limits and observable process/progress state, without model polling."""
from typing import Any
from pathlib import Path
import os
import time
from lifecycle import ACTIVE, alive, identity
import storage as st
import diagnostics


def classify_processes(rows: list[dict[str, Any]], provider_kind: str, leader_pid: int) -> None:
  """Label only captured, recognizable backend helpers; unknown children stay actionable."""
  candidates = {row["pid"]: row for row in rows if provider_kind == "codex"
    and row.get("command_line_status") == "captured" and not row.get("arguments_truncated")
    and row.get("executable") and Path(row["executable"]).name == "codex"
    and row.get("argv", [])[1:3] == ["app-server", "--stdio"]}
  servers = {leader_pid: candidates[leader_pid]} if leader_pid in candidates else {}
  leader = next((r for r in rows if r["pid"] == leader_pid), {})
  argv = leader.get("argv", [])
  if (not servers and leader.get("command_line_status") == "captured"
      and not leader.get("arguments_truncated") and leader.get("executable")
      and Path(leader["executable"]).name in {"node", "nodejs"} and len(argv) >= 4
      and Path(argv[1]).name in {"codex", "codex.js"} and argv[2:4] == ["app-server", "--stdio"]):
    servers = {pid: r for pid, r in candidates.items()
      if r["ppid"] == leader_pid and r["argv"][1:] == argv[2:]}
  for row in rows:
    row.update(process_kind="workload_or_unknown", classification_reason="Not a verified backend helper")
    if row["pid"] in servers:
      row.update(process_kind="runtime_helper",
        classification_reason="Registered backend app-server or its direct matching Node launcher child")
    else:
      parent = servers.get(row["ppid"])
      executable = row.get("executable")
      if (parent and executable and row.get("command_line_status") == "captured"
          and not row.get("arguments_truncated") and row.get("argv")
          and Path(executable).name == "codex-code-mode"
          and Path(executable).parent == Path(parent["executable"]).parent
          and Path(row["argv"][0]).name == "codex-code-mode"):
        row.update(process_kind="runtime_helper",
          classification_reason="Codex code-mode helper, matching app-server executable directory and parent")


def processes(job: dict[str, Any]) -> dict[str, Any]:
  """Inspect verified descendants via host procfs, including namespace PID mappings."""
  leader = job.get("backend_identity")
  if not leader or not alive(leader):
    return {"available": False, "processes": [], "reason": "No live verified backend leader"}
  rows = {}
  with os.scandir("/proc") as entries:
    for entry in entries:
      if not entry.name.isdigit():
        continue
      try:
        path = Path(entry.path)
        raw = (path / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        pid = int(entry.name)
        status = (path / "status").read_text()
        namespace = next((line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")), [])
        rows[pid] = {"pid": pid, "ppid": int(fields[1]), "pgrp": int(fields[2]), "state": fields[0],
                     "name": raw[raw.find("(") + 1:raw.rfind(")")], "namespace_pids": [int(p) for p in namespace],
                     "identity": {"pid": pid, "start": fields[19], "boot": leader["boot"]}}
      except (OSError, ValueError, IndexError):
        continue
  owned = {leader["pid"]}
  while True:
    added = {pid for pid, row in rows.items() if row["ppid"] in owned or row["pgrp"] == leader["pid"]}
    if added <= owned:
      break
    owned.update(added)
  if not alive(leader):
    return {"available": False, "processes": [], "reason": "Backend exited during inspection"}
  # Include the leader during classification: native launches have no wrapper child.
  result = [row for pid, row in sorted(rows.items()) if pid in owned and row["state"] != "Z"]
  values = diagnostics.secrets(dict(os.environ))
  captured = sorted(result, key=lambda row: row["pid"] != leader["pid"])[:257]
  for row in captured:
    row.update(command_line=None, argv=[], command_line_status="unavailable", cwd=None, executable=None)
    if identity(row["pid"]) != row["identity"]:
      row["command_line_status"] = "process_exited_or_identity_changed"
      continue
    try:
      with Path(f"/proc/{row['pid']}/cmdline").open("rb") as stream:
        raw = stream.read(16385)
      truncated = len(raw) > 16384 or bool(raw and not raw.endswith(b"\0"))
      raw = raw[:16384]
      # Omit a partial last argument, which might contain a credential fragment.
      parts = raw.split(b"\0")[:-1]
      arguments = [part.decode("utf-8", errors="replace") for part in parts]
      observed = diagnostics.command_line(arguments, values)
      observed["arguments_truncated"] |= truncated
      try:
        cwd = os.readlink(f"/proc/{row['pid']}/cwd")
      except OSError:
        cwd = None
      try:
        executable = os.readlink(f"/proc/{row['pid']}/exe")
      except OSError:
        executable = None
      if identity(row["pid"]) == row["identity"]:
        row.update(observed, command_line_status="captured" if arguments else "empty", cwd=cwd, executable=executable)
      else:
        row["command_line_status"] = "process_exited_or_identity_changed"
    except OSError:
      pass
  classify_processes(captured, job.get("provider", {}).get("kind", "codex"), leader["pid"])
  children = [row for row in result if row["pid"] != leader["pid"]]
  helpers = sum(row.get("process_kind") == "runtime_helper" for row in children)
  return {"available": True, "observation_version": 2, "live_child_count": len(children),
          "runtime_helper_count": helpers, "workload_or_unknown_count": len(children) - helpers,
          "processes": children[:256], "omitted": max(0, len(children) - 256),
          "scope": "verified descendants/process group visible in host procfs; escaped processes may be absent"}


def finalize_shutdown(job: dict[str, Any]) -> None:
  """Recheck recorded identities after teardown without signaling or rewriting the snapshot."""
  observation = job.get("shutdown_observation")
  if not observation or observation.get("cleanup"):
    return
  remaining = []
  unknown = (not observation.get("available") or bool(observation.get("omitted"))
    or len(observation.get("processes", [])) < observation.get("live_child_count", 0))
  backend_alive = None
  try:
    backend_alive = alive(job.get("backend_identity"))
  except OSError:
    unknown = True
  for row in observation.get("processes", []):
    record = row.get("identity")
    if not record:
      unknown = True
      continue
    try:
      if alive(record):
        remaining.append(row["pid"])
    except OSError:
      unknown = True
  cleanup = {"status": "processes_still_alive" if remaining or backend_alive else
      "unknown" if unknown else "observed_processes_stopped", "checked_at": time.time(),
    "backend_leader_alive": backend_alive, "observed_live_child_count": len(remaining),
    "live_child_pids": remaining, "scope": "Only original recorded identities; not a global process/leak audit"}
  st.update_job(job["id"], shutdown_observation={**observation, "cleanup": cleanup})


def inspect(job: dict[str, Any]) -> dict[str, Any]:
  """Return owned processes, recorded commands and known log paths without inference."""
  logs = []
  for name in ("stderr-" + job["id"] + ".txt", "output-" + job["id"] + ".json"):
    path = st.root() / name
    if path.is_file():
      logs.append({"path": str(path), "bytes": path.stat().st_size})
  return {**processes(job), "active_commands": job.get("active_commands", []), "logs": logs,
          "runtime_image": job.get("runtime_image"), "wait": job.get("worker_wait"),
          "delivery_check": job.get("delivery_check"),
          "timeout_warning": job.get("timeout_warning"), "shutdown_observation": job.get("shutdown_observation")}


def warning_due(job: dict[str, Any], started: float, now: float) -> bool:
  """Warn once per finite deadline, leaving time for a checkpoint before shutdown."""
  seconds = job["timeout_seconds"]
  if not seconds or job.get("cancel_requested"):
    return False
  limit = deadline(job, started)
  return (0 < limit - now <= min(60, max(2, seconds // 5)) and
          (job.get("timeout_warning") or {}).get("timeout_seconds") != seconds)


def validate(seconds: int, maximum: int) -> None:
  """Zero explicitly disables the total time limit; bounded limits retain their cap."""
  if type(seconds) is not int or (seconds != 0 and not 10 <= seconds <= maximum):
    received = repr(seconds) if type(seconds) in {int, bool} else type(seconds).__name__
    raise ValueError(f"timeout_seconds={received} is outside configured bounds. Allowed: 0 (no total limit), "
                     f"or integer seconds 10..{maximum} (configured maximum). "
                     "For a running supported worker use delegate_job action=runtime or action=extend.")


def bounds(maximum: int) -> dict[str, Any]:
  """Expose the actual configured finite range and explicit unbounded sentinel."""
  return {"minimum": 10, "maximum": maximum, "unbounded": 0, "unit": "seconds"}


def settings(job_id: str) -> dict[str, Any]:
  """Acknowledge the latest settings only when worker code actually reads them."""
  value = st.job(job_id)
  if (value.get("runtime_ack_version") == 1 and value.get("runtime_applied_revision") == value.get("runtime_revision", 0)
      and value.get("runtime_applied_timeout_seconds") == value["timeout_seconds"]):
    return value
  with st.locked():
    value = st.job(job_id)
    value.update(runtime_ack_version=1, runtime_applied_revision=value.get("runtime_revision", 0),
      runtime_applied_timeout_seconds=value["timeout_seconds"], runtime_applied_at=time.time())
    value["execution_deadline"] = (value["execution_started_at"] + value["timeout_seconds"]
      if value.get("execution_started_at") and value["timeout_seconds"] else None)
    st.write("job-" + st.key(job_id) + ".json", value)
  return value


def claim_stop(job_id: str, started: float) -> str | None:
  """Serialize deadline cancellation against a parent's concurrent extension."""
  with st.locked():
    value = st.job(job_id)
    if value.get("execution_finished_at"):
      return None
    if value.get("status") == "cancelling":
      return value.get("runtime_stop_reason")
    if value.get("status") not in {"queued", "running"}:
      return None
    reason = "cancelled" if value.get("cancel_requested") else "timed_out" if time.monotonic() >= deadline(value, started) else None
    if reason:
      value.update(status="cancelling", runtime_stop_reason=reason, runtime_stop_claimed_at=time.time())
      st.write("job-" + st.key(job_id) + ".json", value)
    return reason


def live_deadline(job_id: str, started: float) -> float:
  """Read dynamic budgets and claim expiration before a budgeted RPC closes its backend."""
  value = settings(job_id)
  limit = deadline(value, started)
  if time.monotonic() >= limit:
    claim_stop(job_id, started)
    # An extension may have won the lock after the earlier settings read.
    limit = deadline(settings(job_id), started)
  return limit


def deadline(job: dict[str, Any], started: float) -> float:
  """Recompute the limit from the same start when the parent changes runtime settings."""
  seconds = job["timeout_seconds"]
  return started + seconds if seconds else float("inf")


def observe(job: dict[str, Any]) -> dict[str, Any]:
  """Separate liveness from useful progress; missing output is not proof of a stall."""
  now = time.time()
  heartbeat = job.get("heartbeat")
  progress = job.get("last_progress_at")
  worker = job.get("worker_identity")
  backend = job.get("backend_identity")
  running = job["status"] in ACTIVE
  warnings = []
  if running and heartbeat and now - heartbeat > 30:
    warnings.append("worker heartbeat stale; inspect process state")
  if running and progress and now - progress > 600:
    warnings.append("no recent observable progress; long computation may still be legitimate")
  if running and job.get("progress_observable") is False:
    warnings.append("backend only returns its output at completion; progress is unknown")
  if job.get("timeout_seconds") and (job.get("timeout_warning") or {}).get("timeout_seconds") == job.get("timeout_seconds"):
    warnings.append("finite deadline warning issued; checkpoint and inspect active commands before shutdown")
  observation = job.get("shutdown_observation") or {}
  cleanup = observation.get("cleanup") or {}
  if cleanup.get("status") == "processes_still_alive":
    warnings.append("recorded backend/child processes remain alive after teardown; inspect shutdown_observation.cleanup")
  if observation.get("active_commands") or observation.get("workload_or_unknown_count"):
    warnings.append("task commands or unclassified children were active before teardown; verify completion/checkpoints")
  elif observation.get("live_child_count") and observation.get("observation_version") != 2:
    warnings.append("legacy pre-teardown children are unclassified; their count alone does not prove unfinished computation")
  delivery = job.get("delivery_check") or {}
  if delivery.get("status") == "warning":
    warnings.append("Static test dependency check found missing/uncommitted/external helpers; review delivery_check before acceptance")
  elif delivery.get("status") == "incomplete":
    warnings.append("Static test dependency check was incomplete; this is not proof of clean-checkout reproducibility")
  seconds = job.get("timeout_seconds")
  start = job.get("execution_started_at")
  end = now if running else (job.get("finished_at") or now)
  revision = job.get("runtime_revision", 0)
  applied = job.get("runtime_applied_revision")
  ack_supported = job.get("runtime_ack_version") == 1
  change_state = ("applied" if applied is not None and applied >= revision and
    job.get("runtime_applied_timeout_seconds") == seconds else "pending") if (ack_supported or job.get("runtime_ack_expected")) else "unconfirmed"
  return {"live_changes_supported": job.get("runtime_control_version") == 1, "mode": "unbounded" if seconds == 0 else "bounded", "timeout_seconds": seconds,
    "can_change_timeout": job.get("runtime_control_version") == 1 and job.get("status") in {"queued", "running"}
      and not job.get("cancel_requested") and not job.get("execution_finished_at"),
    "elapsed_seconds": round(max(0, end - (start or job.get("created_at", end))), 1),
    "deadline_at": start + seconds if start and seconds else None,
    "remaining_seconds": round(max(0, start + seconds - now), 1)
      if running and not job.get("execution_finished_at") and start and seconds else None,
    "settings_revision": revision, "worker_applied_revision": applied, "worker_ack_supported": ack_supported,
    "change_state": change_state, "worker_applied_at": job.get("runtime_applied_at"),
    "worker_applied_timeout_seconds": job.get("runtime_applied_timeout_seconds"),
    "heartbeat_age_seconds": round(max(0, now - heartbeat), 1) if heartbeat else None,
    "progress_age_seconds": round(max(0, now - progress), 1) if progress else None,
    "worker_alive": alive(worker) if worker else None,
    "backend_leader_alive": alive(backend) if backend else None,
    "phase": job.get("phase"), "worker_wait": job.get("worker_wait"), "warnings": warnings,
    "scope": "point-in-time process identities; heartbeat is not semantic progress or a hard token budget"}

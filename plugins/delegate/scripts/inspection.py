"""Evidence checkpoints, handoff preflight and measured routing summaries."""
from pathlib import Path
from typing import Any
import hashlib
import os
import stat
import subprocess
import statistics
import time
import storage as st
import accounting
from lifecycle import ACTIVE, alive, group_running


def snapshot(job: dict[str, Any], files: list[str]) -> dict[str, Any]:
  """Inspect only explicitly named files and the workspace HEAD; never scan data trees."""
  cwd = Path(job["cwd"]).resolve()
  result: dict[str, Any] = {"cwd": str(cwd), "files": {}, "head": None}
  try:
    p = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    if p.returncode == 0:
      result["head"] = p.stdout.strip()
  except (OSError, subprocess.TimeoutExpired):
    pass
  if len(files) > 16:
    raise ValueError("At most 16 evidence files per checkpoint")
  for name in files:
    lexical = (cwd / name).absolute()
    path = lexical.resolve()
    if lexical != path:
      raise ValueError("Symlink or noncanonical evidence path is not supported")
    if not path.is_relative_to(cwd):
      raise ValueError("Evidence files must belong to the workspace")
    try:
      fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
      with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
          raise ValueError("Evidence must be a regular file")
        data = f.read(16 * 1024 * 1024 + 1)
      if len(data) > 16 * 1024 * 1024:
        raise ValueError("Evidence file exceeds 16 MiB")
      result["files"][str(path)] = hashlib.sha256(data).hexdigest()
    except FileNotFoundError:
      result["files"][str(path)] = None
  return result


def inspect(session_id: str, action: str = "check", job_id: str = "",
            files: list[str] | None = None) -> dict[str, Any]:
  """Inspect a point-in-time checkpoint under the conversation resume guard."""
  if action == "metrics":
    return _inspect(session_id, action, job_id, files)
  if job_id not in st.session(session_id)["jobs"]:
    raise ValueError("Job does not belong to this session")
  import thread_cleanup
  job = st.job(job_id)
  with thread_cleanup.guard(job.get("thread_id") or job_id):
    return _inspect(session_id, action, job_id, files)


def in_use(session_id: str, job: dict[str, Any]) -> bool:
  """Include registered sibling attempts sharing this thread or workspace."""
  for jid in st.session(session_id)["jobs"]:
    other = st.job(jid)
    if (other.get("cwd") == job.get("cwd") or
        (job.get("thread_id") and other.get("thread_id") == job["thread_id"])):
      if other["status"] in ACTIVE or alive(other.get("worker_identity")) or group_running(other.get("backend_identity")):
        return True
  return False


def _inspect(session_id: str, action: str = "check", job_id: str = "",
            files: list[str] | None = None) -> dict[str, Any]:
  """Checkpoint selected evidence, report stale/live blockers, or summarize measured attempts."""
  state = st.session(session_id)
  if action == "metrics":
    groups: dict[str, list] = {}
    for jid in state["jobs"]:
      j = st.job(jid)
      key = ":".join(str(j.get(k, "unknown")) for k in ("category", "complexity", "provider_name", "model", "effort"))
      groups.setdefault(key, []).append(j)
    output = {}
    for key, jobs in groups.items():
      durations = [j["finished_at"] - j["created_at"] for j in jobs if j.get("finished_at") and j.get("created_at")]
      tokens = [accounting.job_usage(j) for j in jobs]
      reviewed = [j for j in jobs if j.get("review") in {"accepted", "needs-work", "blocked"}]
      output[key] = {"attempts": len(jobs), "reviewed": len(reviewed),
        "accepted": sum(j.get("review") == "accepted" for j in jobs),
        "reviewed_acceptance_rate": sum(j.get("review") == "accepted" for j in reviewed) / len(reviewed) if reviewed else None,
        "failed_execution": sum(j["status"] in {"failed", "timed_out", "cancelled"} for j in jobs),
        "median_seconds": statistics.median(durations) if durations else None,
        "measured_tokens": accounting.totals([u for u in tokens if u]),
        "missing_usage": sum(u is None for u in tokens)}
    return {"groups": output, "units": "worker attempts/tokens; parent overhead remains in task report; not billed cost"}
  if job_id not in state["jobs"]:
    raise ValueError("Job does not belong to this session")
  job = st.job(job_id)
  if action == "checkpoint":
    if in_use(session_id, job):
      raise ValueError("Cannot checkpoint work that is still running")
    if not files:
      raise ValueError("Name the relevant code/data/evidence files explicitly")
    value = snapshot(job, files)
    if any(v is None for v in value["files"].values()):
      raise ValueError("Checkpoint evidence must exist")
    st.update_job(job_id, evidence_checkpoint=value, checkpoint_at=time.time(), review="pending", review_evidence="")
    return {"checkpoint": value, "scope": "Only named files and HEAD; not all project dependencies"}
  if action != "check":
    raise ValueError("action must be check/checkpoint/metrics")
  checkpoint = job.get("evidence_checkpoint")
  blockers = []
  if in_use(session_id, job):
    blockers.append("execution still active or cleanup unconfirmed")
  if not Path(job["cwd"]).is_dir():
    blockers.append("workspace missing")
  if checkpoint:
    try:
      current = snapshot(job, list(checkpoint["files"]))
      freshness = "unchanged" if current == checkpoint else "stale"
    except (OSError, ValueError):
      freshness = "stale"
    if freshness == "stale":
      blockers.append("checkpoint inputs or HEAD changed; revalidate before reuse")
  else:
    freshness = "unknown"
    blockers.append("no evidence checkpoint; inspect current workspace and inputs")
  return {"job_id": job_id, "freshness": freshness, "blockers": blockers,
          "lease_transfer_authorized": False, "scope": "Named evidence only; this does not claim an am lease"}

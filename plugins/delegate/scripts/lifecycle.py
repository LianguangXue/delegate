"""Linux process identities for safe reconciliation of interrupted workers."""
from pathlib import Path
from typing import Any
import os
import signal
import subprocess
import time

import storage as st

ACTIVE = {"queued", "running", "cancelling", "orphaned", "archiving"}


def identity(pid: int, include_zombies: bool = False) -> dict[str, Any] | None:
  """Return boot/start identity, treating zombies as exited without trusting PID reuse."""
  try:
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if stat[0] == "Z" and not include_zombies:
      return None
    return {"pid": pid, "start": stat[19], "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
  except (FileNotFoundError, ProcessLookupError):
    return None


def alive(record: dict[str, Any] | None) -> bool:
  """Match both process start time and boot, not only a recycled PID."""
  return bool(record and identity(record["pid"]) == record)


def group_exists(record: dict[str, Any] | None) -> bool:
  """Conservatively retain an uncertain group until cancellation/inspection resolves it."""
  if not record or record["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
    return False
  try:
    os.killpg(record["pid"], 0)
    return True
  except ProcessLookupError:
    return False


def group_running(record: dict[str, Any] | None) -> bool:
  """Check live members on local /proc; zombies cannot keep executing or writing."""
  if not group_exists(record):
    return False
  with os.scandir("/proc") as entries:
    for entry in entries:
      if not entry.name.isdigit():
        continue
      try:
        fields = Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
        if int(fields[2]) == record["pid"] and fields[0] != "Z":
          return True
      except (FileNotFoundError, ProcessLookupError):
        continue
  return False


def stop_owned(proc: subprocess.Popen, record: dict[str, Any]) -> None:
  """Stop the entire newly-created backend group before publishing completion."""
  current = identity(record["pid"])
  if current is not None and current != record:
    raise RuntimeError("Backend leader identity changed; refusing to signal an unrelated process")
  if group_running(record):
    os.killpg(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while group_running(record) and time.monotonic() < deadline:
      time.sleep(0.1)
    if group_running(record):
      os.killpg(record["pid"], signal.SIGKILL)
      deadline = time.monotonic() + 3
      while group_running(record) and time.monotonic() < deadline:
        time.sleep(0.1)
    if group_running(record):
      raise RuntimeError("Owned backend group is still running")
  proc.wait(timeout=3)


def active_groups(value: dict[str, Any]) -> list[dict[str, Any]]:
  """Return distinct process-group identity records recorded on ``value``."""
  groups: list[dict[str, Any]] = []
  seen: set[tuple[int, str, str]] = set()
  for field in ("inference_identity", "backend_identity", "helper_identity"):
    rec = value.get(field)
    if isinstance(rec, dict) and isinstance(rec.get("pid"), int):
      sig = (rec["pid"], str(rec.get("start", "")), str(rec.get("boot", "")))
      if sig not in seen:
        seen.add(sig)
        groups.append(rec)
  return groups


def has_active_group(value: dict[str, Any]) -> bool:
  """Return True if any inference or helper process group is starting or running."""
  targets: list[dict[str, Any] | None] = list(active_groups(value)) or [None]
  return bool(value.get("backend_starting") or value.get("helper_starting")
              or any(group_running(record) for record in targets))


def mark_helper(job_id: str, starting: bool, record: dict[str, Any] | None = None, **extra: Any) -> None:
  """Record helper process state without overwriting a live inference backend identity."""
  with st.locked():
    value = st.job(job_id)
    updates: dict[str, Any] = {"helper_starting": starting, **extra}
    if record is not None:
      updates["helper_identity"] = record
    if not group_running(value.get("inference_identity")):
      updates["backend_starting"] = starting
      if record is not None:
        updates["backend_identity"] = record
    value.update(updates)
    st.write("job-" + st.key(job_id) + ".json", value)


def reconcile(job_id: str) -> dict[str, Any]:
  """Release crashed workers only once their backend group has also disappeared."""
  with st.locked():
    value = st.job(job_id)
    if value["status"] not in ACTIVE or time.time() - value["created_at"] < 15:
      return value
    if alive(value.get("worker_identity")):
      return value
    if has_active_group(value):
      value.update(status="orphaned", error="Worker exited but backend group remains; cancel before resuming")
    else:
      value.update(status="failed", error="Worker exited unexpectedly; backend is no longer running",
                   finished_at=time.time())
    st.write("job-" + st.key(job_id) + ".json", value)
    return value


def cancel_orphan(job_id: str) -> dict[str, Any]:
  """Stop an orphan backend cleanly, or settle dead orphaned jobs when no group remains."""
  value = st.job(job_id)
  groups = active_groups(value)
  live_or_running = [rec for rec in groups if alive(rec) or group_running(rec)]
  if not live_or_running:
    if value.get("backend_starting") or value.get("helper_starting"):
      raise ValueError("Backend startup state is active; inspect before recovery")
    return st.update_job(job_id, status="cancelled", finished_at=time.time(), cancel_requested=True)
  for record in live_or_running:
    if not alive(record):
      raise ValueError("Backend identity cannot be verified; inspect the recorded process group before recovery")
    os.killpg(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while group_running(record) and time.monotonic() < deadline:
      time.sleep(0.1)
    if group_running(record):
      # Only escalate while the original leader still has the same identity.
      if alive(record):
        os.killpg(record["pid"], signal.SIGKILL)
      else:
        raise ValueError("Backend leader exited; group still present, inspect before recovery")
      deadline = time.monotonic() + 3
      while group_running(record) and time.monotonic() < deadline:
        time.sleep(0.1)
    if group_running(record):
      raise ValueError("Backend group is still exiting; retry result after it stops")
  return st.update_job(job_id, status="cancelled", finished_at=time.time(), cancel_requested=True)

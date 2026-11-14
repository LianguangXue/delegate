"""Conservative, dry-run-first garbage collection for the delegate state store."""
from pathlib import Path
from typing import Any
import os
import re
import shlex
import time

from lifecycle import ACTIVE
import runtime_image
import storage as st


def _launcher_runtime_digest() -> str | None:
  """Return the runtime digest currently referenced by ``~/.local/bin/delegate-agent``."""
  launcher = Path.home() / ".local" / "bin" / "delegate-agent"
  try:
    if launcher.is_symlink() or not launcher.is_file():
      return None
    text = launcher.read_text(encoding="utf-8", errors="ignore")
    for token in shlex.split(text):
      match = re.search(r"/runtimes/([0-9a-f]{64})/delegate\.py$", token)
      if match:
        return match.group(1)
  except OSError:
    return None
  return None


def _current_source_digest() -> str | None:
  """Compute the digest of the active script directory without writing a runtime image."""
  source = Path(__file__).resolve().parent
  try:
    files = {p.name: p.read_bytes() for p in sorted(source.glob("*.py")) if p.is_file() and not p.is_symlink()}
    return runtime_image.digest(files) if files else None
  except OSError:
    return None


def collect_garbage(apply: bool = False, retention_days: int = 14) -> dict[str, Any]:
  """Inspect or remove expired terminal state and orphaned WAL; preserve coordination locks and runtimes.

  Args:
    apply: When False (default), return a dry-run inventory without deleting files.
    retention_days: Minimum age in days before settled job/plan artifacts become eligible.

  Returns:
    Summary containing mode, retention window, protected counts, reclaimable counts,
    reclaimed byte total, and bounded sample paths.

  Raises:
    ValueError: If ``apply`` is not boolean or ``retention_days`` is outside ``1..3650``.
  """
  if type(apply) is not bool:
    raise ValueError("apply must be a boolean")
  if type(retention_days) is not int or not 1 <= retention_days <= 3650:
    raise ValueError("retention_days must be 1..3650")

  with st.locked():
    root = st.root()
    now = time.time()
    cutoff = now - retention_days * 86400
    runtime_cutoff = now - 86400
    wal_cutoff = now - 3600

    active_jobs = {str(x) for x in st.read("active.json", []) if isinstance(x, str)}
    pending_wal = {str(x) for x in st.read("dispatch-pending.json", []) if isinstance(x, str)}
    protected_jobs: set[str] = set(active_jobs)
    protected_plans: set[str] = set()
    protected_digests: set[str] = set()
    for digest in (_current_source_digest(), _launcher_runtime_digest()):
      if digest:
        protected_digests.add(digest)

    for loop_path in sorted(root.glob("loop-*.json")):
      if loop_path.is_symlink() or not loop_path.is_file():
        continue
      loop = st.read(loop_path.name)
      if not isinstance(loop, dict):
        continue
      # All retained loop states can be resumed or extended, including completed loops.
      digest = (loop.get("runtime_image") or {}).get("digest")
      if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        protected_digests.add(digest)
      for field in ("current_job", "previous_job"):
        if isinstance(loop.get(field), str) and loop[field]:
          protected_jobs.add(loop[field])
      for child in loop.get("children", []) if isinstance(loop.get("children"), list) else []:
        if isinstance(child, dict) and isinstance(child.get("id"), str) and child["id"]:
          protected_jobs.add(child["id"])

    for task_path in sorted(root.glob("task-*.json")):
      if task_path.name == "task-index.json" or task_path.is_symlink() or not task_path.is_file():
        continue
      task = st.read(task_path.name)
      if not isinstance(task, dict):
        continue
      if task.get("status") != "closed" or bool(task.get("case_key")):
        for jid in task.get("jobs", []) if isinstance(task.get("jobs"), list) else []:
          if isinstance(jid, str) and jid:
            protected_jobs.add(jid)

    file_targets: list[Path] = []
    removed_job_ids: set[str] = set()
    counts = {
      "jobs": 0,
      "deliveries": 0,
      "logs": 0,
      "plans": 0,
      "wal": 0,
      "locks": 0,
      "runtimes": 0,
      "runtimes_retained": 0,
      "sessions_updated": 0,
      "tasks_updated": 0,
    }

    for job_path in sorted(root.glob("job-*.json")):
      if job_path.is_symlink() or not job_path.is_file():
        continue
      jid = job_path.name[len("job-"):-len(".json")]
      record = st.read(job_path.name)
      try:
        mtime = job_path.stat().st_mtime
      except OSError:
        continue
      if not isinstance(record, dict):
        continue
      stamp = record.get("finished_at") or record.get("updated_at") or record.get("created_at") or mtime
      uncollected = record.get("await_completion", True) and not record.get("collected_at")
      is_live = jid in protected_jobs or record.get("status") in ACTIVE or uncollected or float(stamp) >= cutoff
      if is_live:
        digest = (record.get("runtime_image") or {}).get("digest")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
          protected_digests.add(digest)
        plan_id = record.get("plan_id")
        if isinstance(plan_id, str) and plan_id:
          protected_plans.add(plan_id)
        continue

      removed_job_ids.add(jid)
      file_targets.append(job_path)
      counts["jobs"] += 1
      delivery = root / ("delivery-" + jid + ".json")
      if delivery.is_file() and not delivery.is_symlink():
        file_targets.append(delivery)
        counts["deliveries"] += 1
      for log_name in ("stderr-" + jid + ".txt", "output-" + jid + ".json"):
        log_path = root / log_name
        if log_path.is_file() and not log_path.is_symlink():
          file_targets.append(log_path)
          counts["logs"] += 1

    for plan_path in sorted(root.glob("plan-*.json")):
      if plan_path.is_symlink() or not plan_path.is_file():
        continue
      pid = plan_path.name[len("plan-"):-len(".json")]
      if pid in protected_plans:
        continue
      try:
        if plan_path.stat().st_mtime < cutoff:
          file_targets.append(plan_path)
          counts["plans"] += 1
      except OSError:
        continue

    for wal_path in sorted(root.glob("dispatch-*.json")):
      if wal_path.name == "dispatch-pending.json" or wal_path.is_symlink() or not wal_path.is_file():
        continue
      jid = wal_path.name[len("dispatch-"):-len(".json")]
      if jid in pending_wal:
        continue
      try:
        if wal_path.stat().st_mtime < wal_cutoff:
          file_targets.append(wal_path)
          counts["wal"] += 1
      except OSError:
        continue

    # Lock inode identity must survive idle periods and waiting/older processes. Never unlink locks.

    runtimes_dir = root / "runtimes"
    if runtimes_dir.is_dir():
      for entry in sorted(runtimes_dir.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
          continue
        if entry.name.startswith(".") or entry.name in protected_digests:
          continue
        try:
          if entry.stat().st_mtime < runtime_cutoff:
            # Older immutable MCPs have no lifetime lease. An unreferenced image
            # can still be needed for their next worker; age cannot prove it is unused.
            counts["runtimes_retained"] += 1
        except OSError:
          continue

    reclaimable_bytes = 0
    for path in file_targets:
      try:
        reclaimable_bytes += path.stat().st_size
      except OSError:
        continue

    samples = [str(p.relative_to(root)) for p in file_targets[:20]]
    if apply:
      for path in file_targets:
        path.unlink(missing_ok=True)
      if removed_job_ids:
        for sess_path in sorted(root.glob("session-*.json")):
          if sess_path.is_symlink() or not sess_path.is_file():
            continue
          state = st.read(sess_path.name)
          if not isinstance(state, dict):
            continue
          changed = False
          for field in ("jobs", "pending_jobs"):
            if isinstance(state.get(field), list):
              filtered = [jid for jid in state[field] if jid not in removed_job_ids]
              if len(filtered) != len(state[field]):
                state[field] = filtered
                changed = True
          if changed:
            st.write(sess_path.name, state)
            counts["sessions_updated"] += 1
        for task_path in sorted(root.glob("task-*.json")):
          if task_path.name == "task-index.json" or task_path.is_symlink() or not task_path.is_file():
            continue
          task = st.read(task_path.name)
          if not isinstance(task, dict) or task.get("status") != "closed" or task.get("case_key"):
            continue
          if isinstance(task.get("jobs"), list):
            filtered = [jid for jid in task["jobs"] if jid not in removed_job_ids]
            if len(filtered) != len(task["jobs"]):
              task["jobs"] = filtered
              st.write(task_path.name, task)
              counts["tasks_updated"] += 1

    return {
      "dry_run": not apply,
      "preserved": "Coordination locks and frozen runtimes are retained; old processes may still depend on them",
      "retention_days": retention_days,
      "counts": counts,
      "reclaimable_bytes": reclaimable_bytes,
      "sample_paths": samples,
    }

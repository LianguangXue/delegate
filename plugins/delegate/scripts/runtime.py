"""Optional wall-clock limits and observable process/progress state, without model polling."""
from typing import Any
import time
from lifecycle import ACTIVE, alive


def validate(seconds: int, maximum: int) -> None:
  """Zero explicitly disables the total time limit; bounded limits retain their cap."""
  if type(seconds) is not int or (seconds != 0 and not 10 <= seconds <= maximum):
    raise ValueError("timeout_seconds must be 0 (no total limit), or 10..configured maximum")


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
  seconds = job.get("timeout_seconds")
  start = job.get("execution_started_at")
  end = now if running else (job.get("finished_at") or now)
  return {"live_changes_supported": job.get("runtime_control_version") == 1, "mode": "unbounded" if seconds == 0 else "bounded", "timeout_seconds": seconds,
    "elapsed_seconds": round(max(0, end - (start or job.get("created_at", end))), 1),
    "deadline_at": start + seconds if start and seconds else None,
    "heartbeat_age_seconds": round(max(0, now - heartbeat), 1) if heartbeat else None,
    "progress_age_seconds": round(max(0, now - progress), 1) if progress else None,
    "worker_alive": alive(worker) if worker else None,
    "backend_leader_alive": alive(backend) if backend else None,
    "phase": job.get("phase"), "warnings": warnings,
    "scope": "point-in-time process identities; heartbeat is not semantic progress or a hard token budget"}

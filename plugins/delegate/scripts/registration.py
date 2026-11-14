"""Recoverable multi-file dispatch registration; caller holds the store lock."""
from pathlib import Path
from typing import Any
import time
import storage as st


def finish_locked(journal: dict[str, Any]) -> None:
  """Publish the job before references and idempotently finish an interrupted registration."""
  job = journal["job"]
  jid = st.key(job["id"])
  if st.read("job-" + jid + ".json") is None:
    st.write("job-" + jid + ".json", job)
  state = st.session(job["session_id"])
  changed = False
  if jid not in state["jobs"]:
    state["jobs"].append(jid)
    changed = True
  if "pending_jobs" not in state:
    state["pending_jobs"] = [
      existing for existing in state["jobs"]
      if existing != jid and not (st.read("job-" + st.key(existing) + ".json") or {}).get("collected_at")
    ]
    changed = True
  if jid not in state["pending_jobs"]:
    state["pending_jobs"].append(jid)
    changed = True
  if changed:
    st.save_session(job["session_id"], state)
  task_id = st.key(job["task_id"])
  task = st.read("task-" + task_id + ".json")
  if jid not in task["jobs"]:
    task["jobs"].append(jid)
    st.write("task-" + st.key(task["id"]) + ".json", task)
  active = st.read("active.json", [])
  if jid not in active:
    st.write("active.json", active + [jid])
  plan_id = job.get("plan_id")
  if plan_id:
    safe_plan = st.key(plan_id)
    plan = st.read("plan-" + safe_plan + ".json")
    if plan.get("used_by") not in {None, jid}:
      raise ValueError("Conflicting plan registration; preserve journal for inspection")
    plan["used_by"] = jid
    st.write("plan-" + safe_plan + ".json", plan)


def recover_locked() -> None:
  """Recover only explicitly indexed pending registrations, never scan directories."""
  pending = st.read("dispatch-pending.json", [])
  for raw_jid in list(pending):
    jid = st.key(raw_jid)
    journal = st.read("dispatch-" + jid + ".json")
    if journal is None:
      raise ValueError("Pending dispatch journal is missing")
    finish_locked(journal)
    # Pending is cleared before Popen in the normal path, so this interrupted
    # registration never launched inference. Do not leave a phantom active slot.
    job = st.job(jid)
    if job["status"] == "queued" and not job.get("worker_identity"):
      job.update(status="failed", error="Registration interrupted before worker launch", finished_at=time.time(),
                 inference_state="not_started", failure_kind="infrastructure")
      if job["provider"]["kind"] == "codex":
        job["usage_delta"] = {"inputTokens": 0, "outputTokens": 0, "cachedInputTokens": 0}
      else:
        job["usage"] = {"input_tokens": 0, "output_tokens": 0}
      st.write("job-" + jid + ".json", job)
    pending.remove(raw_jid)
    st.write("dispatch-pending.json", pending)
    (st.root() / ("dispatch-" + jid + ".json")).unlink(missing_ok=True)


def commit_locked(job: dict[str, Any]) -> None:
  """Create a write-ahead record before any cross-file references become visible."""
  jid = st.key(job["id"])
  wal = st.root() / ("dispatch-" + jid + ".json")
  st.write(wal.name, {"job": job})
  pending = st.read("dispatch-pending.json", [])
  st.write("dispatch-pending.json", pending + [jid])
  finish_locked({"job": job})
  st.write("dispatch-pending.json", pending)
  wal.unlink(missing_ok=True)

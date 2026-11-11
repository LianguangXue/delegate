"""Recoverable multi-file dispatch registration; caller holds the store lock."""
from typing import Any
import time
import storage as st


def finish_locked(journal: dict[str, Any]) -> None:
  """Publish the job before references and idempotently finish an interrupted registration."""
  job = journal["job"]
  jid = job["id"]
  if st.read("job-" + jid + ".json") is None:
    st.write("job-" + jid + ".json", job)
  state = st.session(job["session_id"])
  if jid not in state["jobs"]:
    state["jobs"].append(jid)
    st.save_session(job["session_id"], state)
  task = st.read("task-" + job["task_id"] + ".json")
  if jid not in task["jobs"]:
    task["jobs"].append(jid)
    st.write("task-" + task["id"] + ".json", task)
  active = st.read("active.json", [])
  if jid not in active:
    st.write("active.json", active + [jid])
  plan_id = job.get("plan_id")
  if plan_id:
    plan = st.read("plan-" + plan_id + ".json")
    if plan.get("used_by") not in {None, jid}:
      raise ValueError("Conflicting plan registration; preserve journal for inspection")
    plan["used_by"] = jid
    st.write("plan-" + plan_id + ".json", plan)


def recover_locked() -> None:
  """Recover only explicitly indexed pending registrations, never scan directories."""
  pending = st.read("dispatch-pending.json", [])
  for jid in list(pending):
    journal = st.read("dispatch-" + st.key(jid) + ".json")
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
    pending.remove(jid)
    st.write("dispatch-pending.json", pending)


def commit_locked(job: dict[str, Any]) -> None:
  """Create a write-ahead record before any cross-file references become visible."""
  jid = job["id"]
  st.write("dispatch-" + jid + ".json", {"job": job})
  pending = st.read("dispatch-pending.json", [])
  st.write("dispatch-pending.json", pending + [jid])
  finish_locked({"job": job})
  st.write("dispatch-pending.json", pending)

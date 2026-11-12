"""Durable completion notices: notification attempts are distinct from result collection."""
from typing import Any
import time
import storage as st
from lifecycle import ACTIVE


def notice(job_id: str, force: bool = False) -> str:
  """Return a stable completion event for retry, or silence after collection/cooldown."""
  with st.locked():
    job = st.job(job_id)
    if job["status"] in ACTIVE - {"orphaned"}:
      return ""
    name = "delivery-" + st.key(job_id) + ".json"
    record = st.read(name, {"event_id": "completion-" + job_id, "job_id": job_id, "attempts": 0})
    if job.get("collected_at"):
      record.update(status="acknowledged", acknowledged_at=job["collected_at"])
      st.write(name, record)
      return ""
    now = time.time()
    if not force and now - record.get("last_attempt_at", 0) < 60:
      return ""
    record.update(status="pending", last_attempt_at=now, attempts=record["attempts"] + 1)
    st.write(name, record)
    return (record["event_id"] + ": delegate job " + job_id + " has status " + job["status"] +
            "; retrieve delegate_job result and follow next_result_call until result_delivery_complete, "
            "then inspect evidence and verify. A notice is not acceptance.")

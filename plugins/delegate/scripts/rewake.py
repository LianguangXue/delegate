"""Wake the owning Claude session when a dispatched plugin job reaches a terminal state."""
import fcntl
import json
import sys
import time
from typing import Any

import lifecycle
import storage as st
import delivery
import handoff


def receipt(value: Any) -> str:
  """Extract only a job ID from the structured MCP tool response."""
  if isinstance(value, str):
    try:
      return receipt(json.loads(value))
    except (ValueError, TypeError):
      return ""
  if isinstance(value, list):
    for item in value:
      found = receipt(item)
      if found:
        return found
  if isinstance(value, dict):
    candidate = value.get("id")
    if isinstance(candidate, str) and candidate.startswith("j") and value.get("status"):
      return st.key(candidate)
    for key in ("content", "text", "result", "tool_response"):
      if key in value:
        found = receipt(value[key])
        if found:
          return found
  return ""


def returned_job(value: Any) -> dict[str, Any] | None:
  """Decode a successful structured tool receipt without inspecting free-form output."""
  if isinstance(value, str):
    try:
      return returned_job(json.loads(value))
    except (ValueError, TypeError):
      return None
  if isinstance(value, list):
    for item in value:
      found = returned_job(item)
      if found:
        return found
  if isinstance(value, dict) and not value.get("isError") and not value.get("is_error"):
    if isinstance(value.get("id"), str) and value["id"].startswith("j") and value.get("status"):
      return value
    for key in ("content", "text", "result", "structuredContent"):
      if key in value:
        found = returned_job(value[key])
        if found:
          return found
  return None


def archive_delivered_history(job_id: str) -> None:
  """Cover legacy workers that predate CLI conversation archival."""
  job = st.job(job_id)
  if (job["provider"]["kind"] == "codex" or job["status"] in lifecycle.ACTIVE
      or (job.get("cli_session") or {}).get("status") == "archived"):
    return
  if not job.get("thread_id"):
    return
  import thread_cleanup
  try:
    thread_cleanup.archive(job_id)
  except (ValueError, OSError) as exc:
    st.update_job(job_id, cli_session={"status": "pending", "error": str(exc)})


def observe_collection(payload: dict[str, Any]) -> None:
  """Acknowledge delivered results even when a pre-0.4.1 MCP server wrote no marker."""
  if payload.get("agent_id") or payload.get("hook_event_name") != "PostToolUse":
    return
  name = str(payload.get("tool_name", ""))
  args = payload.get("tool_input", {})
  sid = payload.get("session_id")
  if not isinstance(args, dict) or not sid or args.get("session_id") != sid or not name.startswith("mcp__"):
    return
  result_call = name.endswith("__delegate_job") and args.get("action") == "result"
  if not result_call and not name.endswith("__delegate_run"):
    return
  response = returned_job(payload.get("tool_response"))
  if not response or response["status"] in lifecycle.ACTIVE - {"orphaned"}:
    return
  failure = response["status"] in {"failed", "cancelled", "timed_out", "orphaned"} and "error" in response
  if "result" not in response and not failure:
    return
  jid = response["id"]
  if result_call and args.get("job_id") != jid:
    return
  try:
    with st.locked():
      job = st.job(jid)
      if (job.get("session_id") != sid or response.get("session_id") != sid
          or response["status"] != job["status"]):
        return
      if not job.get("collected_at"):
        job.update(collected_at=time.time(), collection_source="PostToolUse delivered result")
        st.write("job-" + jid + ".json", job)
  except ValueError:
    return
  archive_delivered_history(jid)


def watch(payload: dict[str, Any]) -> bool:
  """Wait for this response's owned job; return true only when Claude needs its result."""
  if payload.get("agent_id") or not str(payload.get("tool_name", "")).endswith("delegate_run"):
    return False
  sid = payload.get("session_id")
  jid = receipt(payload.get("tool_response"))
  if not sid or not jid:
    return False
  job = st.job(jid)
  if job.get("session_id") != sid:
    return False
  # Concurrent duplicate hooks cannot notify the same live job simultaneously.
  lock = st.root() / ("rewake-" + st.key(jid) + ".lock")
  with lock.open("a") as stream:
    try:
      fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return False
    deadline = time.monotonic() + max(10, int(job.get("timeout_seconds", 900))) + 90
    while time.monotonic() < deadline:
      job = lifecycle.reconcile(jid)
      if job.get("collected_at"):
        archive_delivered_history(jid)
        return False
      if job["status"] not in lifecycle.ACTIVE or job["status"] == "orphaned":
        archive_delivered_history(jid)
        handoff.sync(sid)
        note = delivery.notice(jid)
        if note:
          print(note, file=sys.stderr, flush=True)
          return True
        return False
      time.sleep(2)
  return False


def main() -> None:
  """Read a PostToolUse payload and use asyncRewake's exit-2 notification."""
  try:
    payload = json.load(sys.stdin)
    sys.exit(2 if watch(payload) else 0)
  except (ValueError, OSError, KeyError, TypeError):
    sys.exit(0)


if __name__ == "__main__":
  main()

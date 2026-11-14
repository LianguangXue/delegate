"""Wake the owning Claude session when a dispatched plugin job reaches a terminal state."""
import json
import sys
import time
from typing import Any

import lifecycle
import storage as st
import delivery
import handoff
import completion
import result_delivery
import runtime_image


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


def runtime_notice(payload: dict[str, Any]) -> str:
  """Warn once when a newly dispatched job proves its parent is still using old code."""
  name = str(payload.get("tool_name", ""))
  sid = payload.get("session_id")
  if (payload.get("agent_id") or payload.get("hook_event_name") != "PostToolUse"
      or not name.startswith("mcp__") or not name.endswith("__delegate_run") or not sid):
    return ""
  response = returned_job(payload.get("tool_response"))
  if not response or response.get("session_id") != sid:
    return ""
  try:
    job = st.job(response["id"])
    if job.get("session_id") != sid:
      return ""
    version = runtime_image.job_version(job)
    if version == "unknown" or tuple(map(int, version.split("."))) >= tuple(map(int, runtime_image.VERSION.split("."))):
      return ""
    key = name + ":" + version
    with st.locked():
      state = st.session(sid)
      shown = state.get("runtime_version_notices", [])
      if key in shown:
        return ""
      state["runtime_version_notices"] = (shown + [key])[-16:]
      st.save_session(sid, state)
    return ("This new job was dispatched by delegate " + version + ", while this hook is " + runtime_image.VERSION +
      ". Installed versions do not replace an already connected MCP server. Before another paid attempt, "
      "check the MCP delegate_control status server_runtime.version. Use the installed delegate-agent call CLI "
      "with the SAME session_id=" + sid + " for new dispatch and paginated results, or reload/start a current session. "
      "Run delegate-agent --help or doctor for the input format. Full text: delegate-agent result JOB --session-id SID --output REPORT.md. "
      "Do not rerun a completed review just to upgrade it; preserve its results and acceptance.")
  except (ValueError, OSError, KeyError, TypeError):
    return ""


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
  """Acknowledge exact terminal text/diagnostics delivered by legacy run/result/verify calls."""
  if payload.get("agent_id") or payload.get("hook_event_name") != "PostToolUse":
    return
  name = str(payload.get("tool_name", ""))
  args = payload.get("tool_input", {})
  sid = payload.get("session_id")
  if not isinstance(args, dict) or not sid or args.get("session_id") != sid or not name.startswith("mcp__"):
    return
  job_call = name.endswith("__delegate_job") and args.get("action") in {"result", "verify"}
  if not job_call and not name.endswith("__delegate_run"):
    return
  response = returned_job(payload.get("tool_response"))
  if not response or response["status"] in lifecycle.ACTIVE - {"orphaned"}:
    return
  if response.get("result_text_included") is False:
    return  # Receipts/summary views are not newly delivered report text.
  failure = response["status"] in {"failed", "cancelled", "timed_out", "orphaned"} and "error" in response
  partial = (response.get("response_truncated") or response.get("result_truncated")
             or response.get("result_delivery_complete") is False)
  if "result" not in response and not failure and not partial:
    return
  jid = st.key(response["id"])
  if job_call and args.get("job_id") != jid:
    return
  try:
    with st.locked():
      job = st.job(jid)
      if (job.get("session_id") != sid or response.get("session_id") != sid
          or response["status"] != job["status"]):
        return
      if failure and response.get("error") != job.get("error"):
        return
      text = job.get("result") or ""
      digest = result_delivery.fingerprint(text)
      proof = job.get("result_delivery") or {}
      complete = (proof.get("sha256") == digest and proof.get("complete")
                  and proof.get("ranges") == [[0, len(text)]])
      if partial:
        # Old servers marked collection before returning their 8k preview.
        # Do not let that legacy marker suppress delivery of the missing tail.
        if not complete and job.get("collected_at"):
          job.pop("collected_at", None)
          job.pop("collection_source", None)
          st.write("job-" + jid + ".json", job)
        return
      returned_text = response.get("result")
      if returned_text is None:
        returned_text = ""
      if not isinstance(returned_text, str) or (not complete and returned_text != text):
        return
      if not job.get("collected_at"):
        job.update(collected_at=time.time(), collection_source="PostToolUse delivered result")
      if not complete:
        job["result_delivery"] = {"sha256": digest, "total_chars": len(text), "ranges": [[0, len(text)]],
                                  "complete": True, "last_page_at": time.time()}
      st.write("job-" + jid + ".json", job)
  except ValueError:
    return
  archive_delivered_history(jid)


def compact_echo(payload: dict[str, Any]) -> Any:
  """Compatibility shim for old MCP servers that echo reports after verify or reread."""
  name, sid = str(payload.get("tool_name", "")), payload.get("session_id")
  args = payload.get("tool_input", {})
  if (payload.get("agent_id") or payload.get("hook_event_name") != "PostToolUse" or not name.startswith("mcp__")
      or not name.endswith("__delegate_job") or not isinstance(args, dict) or args.get("session_id") != sid):
    return None
  action = args.get("action")
  if action not in {"verify", "result"}:
    return None
  raw = payload.get("tool_response")
  response = returned_job(raw)
  if not response or "result" not in response or args.get("job_id") != response["id"]:
    return None
  try:
    job = st.job(response["id"])
    if job.get("session_id") != sid or response.get("session_id") != sid or job["status"] != response["status"]:
      return None
    if action == "verify":
      if response.get("review") != args.get("verdict") or job.get("review") != args.get("verdict"):
        return None
      if not result_delivery.proof(job)[2]:
        return None  # Its first full legacy echo is still needed to establish actual delivery.
    else:
      if response.get("result_view") == "page" or "result_offset" in response or "result_sha256" in response:
        return None  # New servers already handle dedup; never hide their first prepared page.
      if args.get("result_view") == "page" or args.get("result_offset") or args.get("result_sha256") or args.get("detail"):
        return None
      _, _, complete = result_delivery.proof(job)
      text = response.get("result")
      if not complete or not isinstance(text, str) or not (job.get("result") or "").startswith(text):
        return None
    value = result_delivery.receipt(job, unchanged=action == "result")
    value["server_runtime_version"] = response.get("server_runtime_version") or "unknown (legacy MCP response)"
    value["formatter_runtime_version"] = runtime_image.VERSION
    rendered = result_delivery.serialize(value)
    if isinstance(raw, dict) and "content" in raw:
      replacement = {**raw, "content": [{"type": "text", "text": rendered}]}
      if "structuredContent" in replacement:
        replacement["structuredContent"] = value
      return replacement
    if isinstance(raw, str):
      return rendered
    if isinstance(raw, list):
      return [{"type": "text", "text": rendered}]
    return value
  except (ValueError, KeyError, OSError, TypeError):
    return None


def watch(payload: dict[str, Any]) -> bool:
  """Wait for this response's owned job; return true only when Claude needs its result."""
  if str(payload.get("tool_name", "")).endswith("__delegate_loop"):
    import persistent_tasks
    return persistent_tasks.watch(payload)
  name = str(payload.get("tool_name", ""))
  args = payload.get("tool_input", {})
  supported = name.endswith("__delegate_run") or (name.endswith("__delegate_job") and isinstance(args, dict)
    and args.get("action") in {"runtime", "result", "watch"})
  if payload.get("agent_id") or not supported:
    return False
  sid = payload.get("session_id")
  jid = receipt(payload.get("tool_response"))
  if not sid or not jid:
    return False
  job = st.job(jid)
  if job.get("session_id") != sid:
    return False
  # Concurrent duplicate hooks cannot notify the same live job simultaneously.
  with st.coordination_files("rewake-" + st.key(jid)) as streams:
    try:
      st.try_coordination_lock(streams)
    except BlockingIOError:
      return False
    token = completion.register(jid, "claude-asyncRewake")
    heartbeat_at = 0.0
    try:
      while True:
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
        if time.monotonic() - heartbeat_at >= 10:
          completion.heartbeat(jid, token)
          heartbeat_at = time.monotonic()
        time.sleep(2)
    finally:
      completion.close(jid, token)
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

"""Archive only plugin-owned Codex threads, with resumable, auditable receipts."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator
import inspect
import time

import storage as st
from codex_rpc import CodexRPC
from lifecycle import ACTIVE, alive, group_running, has_active_group, identity, mark_helper


@contextmanager
def guard(thread_id: str, wait_seconds: float = 0) -> Iterator[None]:
  """Serialize resume registration and archival without holding the global JSON lock."""
  with st.coordination_files("thread-" + st.key(thread_id)) as streams:
    deadline = time.monotonic() + wait_seconds
    while True:
      try:
        st.try_coordination_lock(streams)
        break
      except BlockingIOError:
        if time.monotonic() >= deadline:
          raise ValueError("Thread cleanup/resume is in progress; retry after it finishes")
        time.sleep(0.05)
    yield


def guard_resume(function: Callable) -> Callable:
  """Keep cleanup from interleaving with a resumed job's queue registration."""
  @wraps(function)
  def wrapped(*args: Any, **kwargs: Any) -> Any:
    bound = inspect.signature(function).bind(*args, **kwargs)
    previous = bound.arguments.get("resume_job")
    record = st.job(previous) if previous else None
    if record and record.get("thread_id"):
      with guard(record["thread_id"]):
        return function(*args, **kwargs)
    return function(*args, **kwargs)
  return wrapped


def is_archived(thread: dict[str, Any]) -> bool:
  """Recognize the archive path returned by the installed local app-server."""
  return "archived_sessions" in Path(thread.get("path") or "").parts


def ownership(thread: dict[str, Any], session_id: str, thread_id: str) -> bool:
  """Retain server-verified ownership across Codex's originator relabel on resume."""
  filename = "thread-owner-" + st.key(thread_id) + ".json"
  if thread.get("originator") == "claude_delegate" or thread.get("threadSource") == "claude_delegate":
    st.write(filename, {"thread_id": thread_id, "session_id": session_id,
                        "source": "Codex thread/read", "observed_at": time.time()})
    return True
  previous = st.read(filename, {})
  return previous.get("thread_id") == thread_id and previous.get("session_id") == session_id


def restore(rpc: CodexRPC, thread_id: str, session_id: str) -> None:
  """Restore only the exact thread requested for explicit job continuation."""
  thread = rpc.call("thread/read", {"threadId": thread_id})["thread"]
  if not ownership(thread, session_id, thread_id):
    raise ValueError("Thread ownership is unconfirmed; leave it untouched")
  if is_archived(thread):
    rpc.call("thread/unarchive", {"threadId": thread_id})


def archive(job_id: str, finishing: bool = False) -> dict[str, Any]:
  """Remove a finished owned thread from normal listings, retaining its recoverable history."""
  value = st.job(job_id)
  thread_id = value.get("thread_id")
  if not thread_id:
    return {"status": "not-applicable"}
  if value["provider"]["kind"] != "codex":
    import cli_history
    return cli_history.archive(job_id, finishing)
  with guard(thread_id):
    value = st.job(job_id)
    if (value["status"] in ACTIVE and not finishing) or has_active_group(value):
      raise ValueError("Cannot archive before the owned backend has stopped")
    siblings = []
    for other_id in st.session(value["session_id"])["jobs"]:
      other = st.job(other_id)
      if other.get("thread_id") != thread_id:
        continue
      if other_id != job_id and (other["status"] in ACTIVE or alive(other.get("worker_identity")) or has_active_group(other)):
        raise ValueError("Another job is still using this thread")
      siblings.append(other_id)
    receipt = {"status": "archiving", "thread_id": thread_id, "started_at": time.time()}
    st.update_job(job_id, codex_session=receipt)
    try:
      mark_helper(job_id, True)
      def registered(pid: int) -> None:
        mark_helper(job_id, False, identity(pid, include_zombies=True))
      try:
        connection = CodexRPC(value["provider"]["command"], on_spawn=registered)
      except FileNotFoundError:
        mark_helper(job_id, False)
        raise
      with connection as rpc:
        before = rpc.call("thread/read", {"threadId": thread_id})["thread"]
        # Codex 0.164 can relabel originator to codex_vscode on resume; the explicit
        # threadSource tag survives. Legacy jobs predate the tag and use originator.
        if not ownership(before, value["session_id"], thread_id):
          raise ValueError("Thread originator is not this plugin; leave it untouched")
        if before.get("status", {}).get("type") == "active":
          raise ValueError("Codex reports an active thread; leave it untouched")
        if not is_archived(before):
          rpc.call("thread/archive", {"threadId": thread_id})
        after = rpc.call("thread/read", {"threadId": thread_id})["thread"]
        if not is_archived(after):
          raise RuntimeError("Archive readback did not confirm archived_sessions path")
      receipt.update(status="archived", completed_at=time.time(), archive_path=after["path"],
                     results_preserved=True, recoverable=True)
    except Exception as exc:
      receipt.update(status="pending", error=str(exc))
      current = st.job(job_id)
      if has_active_group(current):
        st.update_job(job_id, codex_session=receipt, status="orphaned",
                      error="Archive backend cleanup unconfirmed; resume is blocked")
      else:
        st.update_job(job_id, codex_session=receipt)
      raise
    for other_id in siblings:
      st.update_job(other_id, codex_session=receipt)
    return receipt

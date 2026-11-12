"""Leased, process-verified waiters; a live waiter is not result collection or acceptance."""
from typing import Any
from pathlib import Path
import os
import time
import uuid

import storage as st
from lifecycle import ACTIVE, alive, identity, reconcile
import result_delivery

LEASE_SECONDS = 30


def parent_mode() -> str:
  """Recognize a one-shot Claude ancestor without recording its arguments or environment."""
  pid = os.getppid()
  for _ in range(8):
    try:
      path = Path(f"/proc/{pid}")
      with (path / "cmdline").open("rb") as stream:
        argv = [p.decode(errors="replace") for p in stream.read(65536).split(b"\0") if p]
      fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
      claude = any(Path(p).name in {"claude", "claude.exe"} or
        p.endswith("/@anthropic-ai/claude-code/cli.js") for p in argv[:2])
      if claude:
        if not any(p in {"-p", "--print"} or p.startswith("--print=") for p in argv):
          return "interactive"
        if any(argv[i:i+2] == ["--input-format", "stream-json"] for i in range(len(argv))) or "--input-format=stream-json" in argv:
          return "streaming"
        return "one-shot"
      pid = int(fields[1])
      if pid <= 1:
        break
    except (OSError, ValueError, IndexError):
      break
  return "unknown"


def live(job: dict[str, Any]) -> list[dict[str, Any]]:
  """Require both a fresh heartbeat and the original boot/start/PID, never just a flag."""
  now = time.time()
  result = []
  for token, row in (job.get("completion_waiters") or {}).items():
    if row.get("status") != "watching" or not 0 <= now - row.get("heartbeat_at", 0) <= LEASE_SECONDS:
      continue
    try:
      verified = alive(row.get("identity"))
    except OSError:
      verified = False
    if verified:
      result.append({"token": token, **row})
  return result


def defers_stop(job: dict[str, Any]) -> bool:
  """Keep required work open only for explicit notification mode with a viable waiter."""
  return job.get("completion_mode") == "notify" and any(
    row.get("kind") in {"claude-asyncRewake", "host-background"} and row.get("parent_mode") in {"interactive", "streaming"}
    for row in live(job))


def register(job_id: str, kind: str) -> str:
  """Register only this process; callers already validated the owning session/job."""
  token = uuid.uuid4().hex
  record = identity(os.getpid())
  mode = parent_mode()
  if not record:
    raise ValueError("Cannot verify completion waiter process identity")
  with st.locked():
    job = st.job(job_id)
    rows = {row["token"]: {k: v for k, v in row.items() if k != "token"} for row in live(job)}
    if len(rows) >= 8:
      raise ValueError("This job already has eight live completion waiters; use the existing waiter")
    rows[token] = {"identity": record, "kind": kind, "parent_mode": mode, "status": "watching", "heartbeat_at": time.time(),
                   "started_at": time.time()}
    job["completion_waiters"] = rows
    st.write("job-" + job_id + ".json", job)
  return token


def heartbeat(job_id: str, token: str) -> None:
  """Refresh only this waiter's lease without changing unrelated job fields."""
  with st.locked():
    job = st.job(job_id)
    row = (job.get("completion_waiters") or {}).get(token)
    if row and row.get("status") == "watching":
      row["heartbeat_at"] = time.time()
      st.write("job-" + job_id + ".json", job)


def close(job_id: str, token: str) -> None:
  """Remove this waiter's lease on completion, cancellation or broken output."""
  with st.locked():
    job = st.job(job_id)
    if token in (job.get("completion_waiters") or {}):
      job["completion_waiters"].pop(token)
      st.write("job-" + job_id + ".json", job)


def describe(job: dict[str, Any]) -> dict[str, Any]:
  """Expose a usable waiter command and honest delivery/collection status."""
  host = st.session(job["session_id"]).get("host_kind", "claude")
  suffix = " " + host if host in {"codex", "dsh"} else ""
  watchers = live(job)
  return {"mode": job.get("completion_mode", "wait"), "watcher_active": bool(watchers),
    "watchers": [{"kind": r["kind"], "parent_mode": r.get("parent_mode"),
                  "pid": r["identity"]["pid"], "heartbeat_at": r["heartbeat_at"]} for r in watchers],
    "stop_may_defer": defers_stop(job),
    "wait_command": "delegate-agent wait" + suffix,
    "wait_stdin": {"session_id": job["session_id"], "job_id": job["id"], "background": True},
    "delivery_scope": "Claude asyncRewake or host background-task completion while the session remains running; closed sessions replay pending notices",
    "instruction": "Run wait_command with wait_stdin through the host background-command facility; it does not collect or accept results"}


def wait(payload: dict[str, Any]) -> dict[str, Any]:
  """Wait in code for arbitrarily long jobs, returning a notice rather than falsely collecting output."""
  import service
  background = payload.get("background", False)
  if type(background) is not bool:
    raise ValueError("wait background must be a boolean")
  sid, jid = payload["session_id"], payload["job_id"]
  # Reuse the public ownership checks before registering a real waiter.
  service.jobs(sid, "watch" if background else "status", jid)
  token = register(jid, "host-background" if background else "foreground")
  last_heartbeat = 0.0
  try:
    while True:
      job = reconcile(jid)
      if job["status"] not in ACTIVE - {"orphaned"}:
        return {"id": jid, "session_id": sid, "status": job["status"],
          "notification": "Delegate finished; collect all result pages and validate before acceptance",
          "needs_result": not bool(job.get("collected_at")), "next_result_call": result_delivery.next_call(job)}
      if time.monotonic() - last_heartbeat >= 10:
        heartbeat(jid, token)
        last_heartbeat = time.monotonic()
      time.sleep(1)
  finally:
    close(jid, token)

"""A Codex dynamic tool that waits in code while the control loop stays responsive."""
from pathlib import Path
from typing import Any
import json
import queue
import threading
import time

import storage as st
import runtime
from lifecycle import identity, alive

SPEC = {"type": "function", "name": "delegate_wait",
  "description": "Wait without repeated model calls for a completion file or a job-owned process to exit. Exactly one of path/pid. No shell, network or file contents are exposed. Use a file written atomically at successful completion; process exit alone does not prove success. timeout_seconds=0 waits until ready or job cancellation/deadline warning. Prefer this to repeated short polling of long computations.",
  "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "pid": {"type": "integer"},
    "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 86400}}, "additionalProperties": False}}


class Waiter:
  """Own at most one asynchronous condition wait; never start another inference turn."""

  def __init__(self, job: dict[str, Any]) -> None:
    self.job = job
    self.stop = threading.Event()
    self.results: queue.Queue[tuple[Any, dict[str, Any]]] = queue.Queue()
    self.thread: threading.Thread | None = None

  def start(self, message: dict[str, Any]) -> None:
    """Validate and launch a tool wait without blocking event/steering processing."""
    if self.thread and self.thread.is_alive():
      raise ValueError("One delegate_wait is already active")
    arguments = message["params"]["arguments"]
    if isinstance(arguments, str):
      arguments = json.loads(arguments)
    if not isinstance(arguments, dict) or set(arguments) - {"path", "pid", "timeout_seconds"}:
      raise ValueError("Invalid delegate_wait arguments")
    path, pid = arguments.get("path"), arguments.get("pid")
    if (path is None) == (pid is None):
      raise ValueError("Specify exactly one of path or pid")
    seconds = arguments.get("timeout_seconds", 0)
    if type(seconds) is not int or not 0 <= seconds <= 86400:
      raise ValueError("timeout_seconds must be 0..86400")
    target, process = None, None
    if path is not None:
      if not isinstance(path, str) or not path:
        raise ValueError("path must be non-empty")
      target = Path(self.job["cwd"]) / path
      self.validate_path(target)
    else:
      if type(pid) is not int or pid < 1:
        raise ValueError("pid must be positive")
      rows = runtime.processes(st.job(self.job["id"])).get("processes", [])
      row = next((r for r in rows if r["pid"] == pid), None)
      if not row:
        matches = [r for r in rows if r["namespace_pids"] and r["namespace_pids"][-1] == pid]
        row = matches[0] if len(matches) == 1 else None
      if not row:
        raise ValueError("PID is not a currently verified child of this job; use an explicit completion file")
      process = identity(row["pid"])
      if not process or process != row["identity"]:
        raise ValueError("Process exited before wait registration; verify its completion artifact")
    self.stop.clear()
    st.update_job(self.job["id"], worker_wait={"state": "waiting", "path": str(target) if target else None,
                  "pid": process["pid"] if process else None, "started_at": time.time(), "inference_calls": 0})
    self.thread = threading.Thread(target=self._wait, args=(message["id"], target, process, seconds), daemon=True)
    self.thread.start()

  def validate_path(self, target: Path) -> None:
    """Allow only workspace paths, rechecking symlink resolution on every probe."""
    roots = [Path(self.job[k]).resolve() for k in ("cwd", "source_cwd") if self.job.get(k)]
    if not any(target.resolve().is_relative_to(root) for root in roots):
      raise ValueError("Completion file must be inside this job's workspace/source directory")

  def _wait(self, request_id: Any, target: Path | None, process: dict[str, Any] | None, seconds: int) -> None:
    started = time.monotonic()
    status = "stopped"
    success = True
    try:
      while not self.stop.is_set():
        if target:
          self.validate_path(target)
          if target.is_file():
            status = "file_exists"
            break
        elif not alive(process):
          status = "process_exited; exit status unknown, verify artifacts"
          break
        if seconds and time.monotonic() - started >= seconds:
          status = "wait_timeout"
          break
        self.stop.wait(2)
    except Exception as exc:
      status, success = str(exc), False
    result = {"state": status, "elapsed_seconds": round(time.monotonic() - started, 1), "inference_calls": 0}
    st.update_job(self.job["id"], worker_wait=result)
    self.results.put((request_id, {"success": success,
      "contentItems": [{"type": "inputText", "text": json.dumps(result)}]}))

  def drain(self, rpc: Any) -> None:
    """Deliver only completed waits; no response is sent during the waiting period."""
    while True:
      try:
        request_id, result = self.results.get_nowait()
      except queue.Empty:
        break
      rpc.send({"id": request_id, "result": result})

  def close(self) -> None:
    """Stop a wait before backend teardown or timeout checkpoint steering."""
    self.stop.set()
    if self.thread:
      self.thread.join(timeout=3)

  def __enter__(self) -> "Waiter":
    return self

  def __exit__(self, *args: Any) -> None:
    self.close()
    current = st.job(self.job["id"])
    st.update_job(self.job["id"], shutdown_observation={**runtime.processes(current),
      "active_commands": current.get("active_commands", []), "observed_at": time.time(),
      "note": "Before backend teardown: raw child count includes runtime helpers. Inspect process_kind and cleanup; helper presence alone is not unfinished computation."})

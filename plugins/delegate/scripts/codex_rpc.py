"""Codex app-server transport with bounded waits and streamed events."""
from collections import deque
from typing import Any, Callable
import json
import os
import queue
import signal
import select
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from lifecycle import identity, stop_owned
from diagnostics import StderrCapture, secrets


class CodexRPC:
  """Own one stdio app-server and close it without touching other sessions."""

  def __init__(self, command: str = "codex", on_spawn: Callable[[int], None] | None = None, deadline: float | Callable[[], float] | None = None,
               cancelled: Callable[[], bool] | None = None, stderr_path: Path | None = None,
               env_overrides: dict[str, str] | None = None) -> None:
    self.deadline = deadline
    self.cancelled = cancelled
    env = dict(os.environ)
    if env_overrides and (set(env_overrides) - {"CUDA_VISIBLE_DEVICES"} or any(not isinstance(v, str) for v in env_overrides.values())):
      raise ValueError("Only validated CUDA_VISIBLE_DEVICES can override a worker environment")
    env.update(env_overrides or {})
    env["DELEGATE_WORKER"] = "1"
    # A ChatGPT-backed Codex worker must never fall back to a paid API key.
    env.pop("OPENAI_API_KEY", None)
    # A writable sandbox temporary path avoids repeatedly discovering home-cache failures.
    env["NUMBA_CACHE_DIR"] = "/tmp/delegate-numba-" + str(os.getuid())
    argv = [command, "app-server", "--stdio", "-c", 'model_provider="openai"',
            "-c", 'forced_login_method="chatgpt"']
    if env_overrides and "CUDA_VISIBLE_DEVICES" in env_overrides:
      argv += ["-c", "shell_environment_policy.set.CUDA_VISIBLE_DEVICES=" + json.dumps(env_overrides["CUDA_VISIBLE_DEVICES"])]
    config = Path(env.get("CODEX_HOME", str(Path.home()/".codex"))) / "config.toml"
    try:
      with config.open('rb') as stream:
        configured = tomllib.load(stream)
      if "delegate" in configured.get("mcp_servers", {}):
        argv += ["-c", "mcp_servers.delegate.enabled=false"]
    except FileNotFoundError:
      pass
    secret_values = secrets(env)
    self.proc = subprocess.Popen(
      argv,
      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
      text=True, env=env, start_new_session=True,
    )
    self.messages: queue.Queue[Any] = queue.Queue()
    self.pending: deque[dict[str, Any]] = deque()
    self.serial = 0
    self.process_identity = identity(self.proc.pid, include_zombies=True)
    self.stderr = None
    self.reader = None
    try:
      if on_spawn:
        on_spawn(self.proc.pid)
      self.stderr = StderrCapture(self.proc.stderr, secret_values, stderr_path)
      self.reader = threading.Thread(target=self._reader, daemon=True)
      self.reader.start()
      os.set_blocking(self.proc.stdin.fileno(), False)
      self.call("initialize", {"clientInfo": {"name": "claude_delegate", "version": "0.17.1"},
                               "capabilities": {"experimentalApi": True}})
      self.send({"method": "initialized", "params": {}})
    except BaseException:
      self.close()
      raise

  def _reader(self) -> None:
    try:
      for line in self.proc.stdout:
        try:
          self.messages.put(json.loads(line))
        except json.JSONDecodeError:
          continue
    finally:
      self.messages.put(None)

  def budget_deadline(self) -> float:
    """Read a dynamic task budget while keeping individual RPC waits bounded."""
    value = self.deadline() if callable(self.deadline) else self.deadline
    return value if value is not None else float("inf")

  def wait_deadline(self, request_deadline: float, phase: str) -> float:
    """Re-read a crossed dynamic limit; the fixed RPC timeout never moves."""
    while True:
      sampled_at = time.monotonic()
      limit = min(request_deadline, self.budget_deadline())
      now = time.monotonic()
      if now >= request_deadline:
        raise TimeoutError(phase + " timed out (per-request timeout)")
      if now < limit:
        return limit
      if limit <= sampled_at:
        # The budget was expired before its callback was invoked. The worker's
        # callback has now either claimed cancellation or observed an extension.
        raise TimeoutError("Task deadline reached during " + phase)
      # It crossed between the callback and the clock read. Let the callback
      # recheck under the job lock before deciding to close a live backend.

  def send(self, value: dict[str, Any]) -> None:
    """Write one JSON-RPC message."""
    data = (json.dumps(value) + "\n").encode()
    fd = self.proc.stdin.fileno()
    deadline = time.monotonic() + 10
    while data:
      if self.cancelled and self.cancelled():
        raise InterruptedError("Codex input cancelled")
      remaining = self.wait_deadline(deadline, "Codex input pipe") - time.monotonic()
      if remaining <= 0:
        continue
      if not select.select([], [fd], [], min(1, remaining))[1]:
        continue
      try:
        data = data[os.write(fd, data):]
      except BlockingIOError:
        continue

  def receive(self, timeout: float = 1) -> dict[str, Any] | None:
    """Return an event, preserving notifications received during requests."""
    if self.pending:
      return self.pending.popleft()
    try:
      value = self.messages.get(timeout=timeout)
    except queue.Empty:
      return None
    if value is None:
      raise RuntimeError(self.disconnected("event stream"))
    return value

  def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30) -> Any:
    """Wait for one response while buffering other messages in order."""
    request_deadline = time.monotonic() + timeout
    self.wait_deadline(request_deadline, method)
    if self.cancelled and self.cancelled():
      raise InterruptedError("Codex request cancelled before " + method)
    self.serial += 1
    request_id = self.serial
    self.send({"id": request_id, "method": method, "params": params or {}})
    while True:
      deadline = self.wait_deadline(request_deadline, method)
      if self.cancelled and self.cancelled():
        raise InterruptedError("Codex request cancelled during " + method)
      try:
        value = self.messages.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
      except queue.Empty:
        continue
      if value is None:
        raise RuntimeError(self.disconnected(method))
      if value.get("id") == request_id and "method" not in value:
        if "error" in value:
          raise RuntimeError(method + ": " + json.dumps(value["error"]))
        return value.get("result")
      # Never leave approval/tool requests waiting inside a synchronous call.
      if "id" in value and "method" in value:
        if value["method"] == "item/tool/call" and value.get("params", {}).get("tool") == "delegate_wait":
          self.pending.append(value)
        else:
          self.deny(value)
      else:
        self.pending.append(value)

  def disconnected(self, phase: str) -> str:
    """Explain a disconnect with the exit status and a redacted stderr tail."""
    try:
      code = self.proc.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
      code = self.proc.poll()
    self.stderr.reader.join(timeout=0.2)
    self.stderr.save()
    tail = self.stderr.text()[-3000:]
    return f"Codex disconnected during {phase}; exit_code={code}; stderr_tail={tail or '[empty]'}"

  def deny(self, message: dict[str, Any]) -> None:
    """Deny unexpected server requests instead of granting broader permissions."""
    method = message["method"]
    if method.endswith("requestApproval"):
      self.send({"id": message["id"], "result": {"decision": "decline"}})
    else:
      self.send({"id": message["id"], "error": {"code": -32601, "message": "Interactive request unavailable"}})

  def close(self) -> None:
    """Terminate the owned server and reap the child."""
    try:
      stop_owned(self.proc, self.process_identity)
    finally:
      if self.stderr:
        self.stderr.close()
      else:
        self.proc.stderr.close()
    if self.reader:
      self.reader.join(timeout=1)
    self.proc.stdin.close()
    # An escaped descendant retaining stdout must not hold the reader lock forever.
    if not self.reader or not self.reader.is_alive():
      self.proc.stdout.close()

  def __enter__(self) -> "CodexRPC":
    return self

  def __exit__(self, *args: Any) -> None:
    self.close()


def models(command: str = "codex") -> list[dict[str, Any]]:
  """Fetch the live paginated model list and supported reasoning efforts."""
  rows = []
  with CodexRPC(command) as rpc:
    cursor = None
    for _ in range(20):
      result = rpc.call("model/list", {"cursor": cursor, "limit": 100})
      rows.extend(result["data"])
      cursor = result.get("nextCursor")
      if not cursor:
        return rows
  raise RuntimeError("Model catalog pagination exceeded limit")

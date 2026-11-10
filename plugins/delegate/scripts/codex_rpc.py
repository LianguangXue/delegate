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


class CodexRPC:
  """Own one stdio app-server and close it without touching other sessions."""

  def __init__(self, command: str = "codex", on_spawn: Callable[[int], None] | None = None, deadline: float | Callable[[], float] | None = None,
               cancelled: Callable[[], bool] | None = None) -> None:
    self.deadline = deadline
    self.cancelled = cancelled
    env = dict(os.environ)
    env["DELEGATE_WORKER"] = "1"
    # A ChatGPT-backed Codex worker must never fall back to a paid API key.
    env.pop("OPENAI_API_KEY", None)
    argv = [command, "app-server", "--stdio", "-c", 'model_provider="openai"',
            "-c", 'forced_login_method="chatgpt"']
    config = Path(env.get("CODEX_HOME", str(Path.home()/".codex"))) / "config.toml"
    try:
      with config.open('rb') as stream:
        configured = tomllib.load(stream)
      if "delegate" in configured.get("mcp_servers", {}):
        argv += ["-c", "mcp_servers.delegate.enabled=false"]
    except FileNotFoundError:
      pass
    self.proc = subprocess.Popen(
      argv,
      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
      text=True, env=env, start_new_session=True,
    )
    self.messages: queue.Queue[Any] = queue.Queue()
    self.pending: deque[dict[str, Any]] = deque()
    self.serial = 0
    self.process_identity = identity(self.proc.pid, include_zombies=True)
    self.reader = threading.Thread(target=self._reader, daemon=True)
    self.reader.start()
    try:
      if on_spawn:
        on_spawn(self.proc.pid)
      os.set_blocking(self.proc.stdin.fileno(), False)
      self.call("initialize", {"clientInfo": {"name": "claude_delegate", "version": "0.12.0"}})
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

  def send(self, value: dict[str, Any]) -> None:
    """Write one JSON-RPC message."""
    data = (json.dumps(value) + "\n").encode()
    fd = self.proc.stdin.fileno()
    deadline = time.monotonic() + 10
    while data:
      if self.cancelled and self.cancelled():
        raise InterruptedError("Codex input cancelled")
      remaining = min(deadline, self.budget_deadline()) - time.monotonic()
      if remaining <= 0:
        raise TimeoutError("Codex input pipe timed out")
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
      raise RuntimeError("Codex app-server disconnected")
    return value

  def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30) -> Any:
    """Wait for one response while buffering other messages in order."""
    deadline = time.monotonic() + timeout
    request_deadline = deadline
    deadline = min(request_deadline, self.budget_deadline())
    if deadline <= time.monotonic():
      raise TimeoutError("Task deadline reached before " + method)
    if self.cancelled and self.cancelled():
      raise InterruptedError("Codex request cancelled before " + method)
    self.serial += 1
    request_id = self.serial
    self.send({"id": request_id, "method": method, "params": params or {}})
    while True:
      deadline = min(request_deadline, self.budget_deadline())
      if time.monotonic() >= deadline:
        break
      if self.cancelled and self.cancelled():
        raise InterruptedError("Codex request cancelled during " + method)
      try:
        value = self.messages.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
      except queue.Empty:
        continue
      if value is None:
        raise RuntimeError("Codex disconnected during " + method)
      if value.get("id") == request_id and "method" not in value:
        if "error" in value:
          raise RuntimeError(method + ": " + json.dumps(value["error"]))
        return value.get("result")
      # Never leave approval/tool requests waiting inside a synchronous call.
      if "id" in value and "method" in value:
        self.deny(value)
      else:
        self.pending.append(value)
    raise TimeoutError(method + " timed out")

  def deny(self, message: dict[str, Any]) -> None:
    """Deny unexpected server requests instead of granting broader permissions."""
    method = message["method"]
    if method.endswith("requestApproval"):
      self.send({"id": message["id"], "result": {"decision": "decline"}})
    else:
      self.send({"id": message["id"], "error": {"code": -32601, "message": "Interactive request unavailable"}})

  def close(self) -> None:
    """Terminate the owned server and reap the child."""
    stop_owned(self.proc, self.process_identity)
    self.reader.join(timeout=1)
    self.proc.stdin.close()
    # An escaped descendant retaining stdout must not hold the reader lock forever.
    if not self.reader.is_alive():
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

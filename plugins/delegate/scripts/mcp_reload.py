"""Hot-reload delegate MCP engines across plugin updates without restarting the host session."""
from pathlib import Path
from typing import Any, Callable
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

import lifecycle
import runtime_entry
import runtime_image


def _parse_version(text: Any) -> tuple[int, int, int] | None:
  """Parse a strict ``X.Y.Z`` semver string into an integer triple."""
  if not isinstance(text, str) or not re.fullmatch(r"\d+\.\d+\.\d+", text):
    return None
  major, minor, patch = (int(part) for part in text.split("."))
  return (major, minor, patch)


def _max_py_mtime(directory: Path) -> int:
  """Return the newest ``.py`` nanosecond mtime under ``directory``, or 0."""
  newest = 0
  try:
    with os.scandir(directory) as entries:
      for entry in entries:
        if entry.name.endswith(".py") and entry.is_file(follow_symlinks=False):
          newest = max(newest, entry.stat(follow_symlinks=False).st_mtime_ns)
  except OSError:
    return 0
  return newest


def _claude_plugins_dir() -> Path:
  """Return the active Claude plugins root honoring ``CLAUDE_CONFIG_DIR``."""
  root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()
  return root / "plugins"


def should_auto_reload(origin_scripts: Path) -> bool:
  """Determine whether an MCP server should follow newer installed or marketplace releases.

  Args:
    origin_scripts: Original ``scripts/`` directory from which the server launched.

  Returns:
    True when the server should monitor and hot-reload newer plugin runtimes.
  """
  if os.environ.get("DELEGATE_MCP_BACKEND") == "1" or os.environ.get("DELEGATE_WORKER"):
    return False
  flag = os.environ.get("DELEGATE_MCP_HOT_RELOAD")
  if flag == "0":
    return False
  if flag == "1" or os.environ.get("DELEGATE_MANAGED_ENTRY") == "1":
    return True
  try:
    plugins_root = _claude_plugins_dir().resolve()
    return origin_scripts.resolve().is_relative_to(plugins_root)
  except OSError:
    return False


def _user_delegate_marketplace() -> str:
  """Return the marketplace name of the unique user-scoped delegate install, if any."""
  registry = _claude_plugins_dir() / "installed_plugins.json"
  value = runtime_entry.read_json(registry)
  if not isinstance(value, dict) or not isinstance(value.get("plugins"), dict):
    return ""
  matches: list[str] = []
  for name, rows in value["plugins"].items():
    if name.split("@")[0] != "delegate" or not isinstance(rows, list):
      continue
    if any(isinstance(row, dict) and row.get("scope") == "user" for row in rows):
      matches.append(name.split("@", 1)[1] if "@" in name else "")
  return matches[0] if len(matches) == 1 else ""


def _local_marketplace_candidate(marketplace: str) -> tuple[Path, str] | None:
  """Resolve a local directory marketplace's delegate script and version when valid."""
  if not marketplace:
    return None
  known_path = _claude_plugins_dir() / "known_marketplaces.json"
  known = runtime_entry.read_json(known_path)
  if not isinstance(known, dict):
    return None
  entry = known.get(marketplace)
  if not isinstance(entry, dict):
    return None
  source = entry.get("source")
  if not isinstance(source, dict) or source.get("source") != "directory":
    return None
  raw_root = source.get("path") or entry.get("installLocation")
  if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
    return None
  mkt_root = Path(raw_root).resolve()
  manifest = runtime_entry.read_json(mkt_root / ".claude-plugin" / "marketplace.json")
  if not isinstance(manifest, dict) or not isinstance(manifest.get("plugins"), list):
    return None
  rel_source = ""
  for row in manifest["plugins"]:
    if isinstance(row, dict) and row.get("name") == "delegate" and isinstance(row.get("source"), str):
      rel_source = row["source"]
      break
  if not rel_source:
    return None
  plugin_dir = Path(os.path.abspath(mkt_root / rel_source))
  if not plugin_dir.is_relative_to(mkt_root):
    return None
  plugin_dir = plugin_dir.resolve()
  if not plugin_dir.is_relative_to(mkt_root):
    return None
  pkg_manifest = runtime_entry.read_json(plugin_dir / ".claude-plugin" / "plugin.json")
  if not isinstance(pkg_manifest, dict) or pkg_manifest.get("name") != "delegate":
    return None
  ver_text = pkg_manifest.get("version")
  ver = _parse_version(ver_text)
  if ver is None or ver < (0, 26, 0):
    return None
  assert isinstance(ver_text, str)
  script = runtime_entry.entry_file(plugin_dir / "scripts" / "delegate.py")
  return (script, ver_text)


def discover_target(self_entry: Path, self_version: str) -> tuple[Path, str]:
  """Select the highest eligible delegate entrypoint from installed plugins or local marketplaces.

  Args:
    self_entry: Fallback entrypoint currently executing this server.
    self_version: Version string of ``self_entry``.

  Returns:
    ``(entrypoint_path, version_string)`` never older than ``self_version``.
  """
  chosen_entry = self_entry
  chosen_ver = self_version
  try:
    resolved = runtime_entry.resolve(self_entry)
    pkg_dir = resolved.parent.parent
    manifest_path = pkg_dir / ".claude-plugin" / "plugin.json"
    if manifest_path.is_file():
      manifest = runtime_entry.read_json(manifest_path)
      if (isinstance(manifest, dict) and manifest.get("name") == "delegate"
          and _parse_version(manifest.get("version")) is not None):
        chosen_entry = resolved
        chosen_ver = str(manifest["version"])
  except (OSError, ValueError, TypeError, KeyError):
    chosen_entry = self_entry
    chosen_ver = self_version

  try:
    mkt_name = _user_delegate_marketplace()
    mkt_hit = _local_marketplace_candidate(mkt_name)
    if mkt_hit is not None:
      mkt_entry, mkt_ver_text = mkt_hit
      mkt_ver = _parse_version(mkt_ver_text)
      cur_ver = _parse_version(chosen_ver)
      if mkt_ver is not None and (cur_ver is None or mkt_ver > cur_ver or (
          mkt_ver == cur_ver and _max_py_mtime(mkt_entry.parent) > _max_py_mtime(chosen_entry.parent))):
        chosen_entry, chosen_ver = mkt_entry, mkt_ver_text
  except (OSError, ValueError, TypeError, KeyError):
    pass

  self_ver_tuple = _parse_version(self_version)
  chosen_ver_tuple = _parse_version(chosen_ver)
  if self_ver_tuple is not None and (chosen_ver_tuple is None or chosen_ver_tuple < self_ver_tuple):
    return (self_entry, self_version)
  return (chosen_entry, chosen_ver)


def _stat_fingerprint(self_entry: Path) -> tuple[Any, ...]:
  """Build a cheap metadata fingerprint to skip redundant freezes when nothing changed."""
  plugins = _claude_plugins_dir()
  reg_stat: tuple[int, int] | None = None
  mkt_stat: tuple[int, int] | None = None
  for target_name, slot in (("installed_plugins.json", "reg"), ("known_marketplaces.json", "mkt")):
    try:
      info = (plugins / target_name).stat(follow_symlinks=False)
      val = (info.st_mtime_ns, info.st_size)
    except OSError:
      val = None
    if slot == "reg":
      reg_stat = val
    else:
      mkt_stat = val
  return (reg_stat, mkt_stat, _max_py_mtime(self_entry.parent))


class BackendClient:
  """Managed JSON-RPC child process serving an upgraded frozen delegate runtime."""

  def __init__(self, entry: Path, version: str, digest: str, caller_host: str,
               outer_identity: dict[str, Any] | None) -> None:
    """Spawn a persistent backend ``delegate.py`` MCP process and complete ``initialize``.

    Args:
      entry: Frozen ``delegate.py`` path.
      version: Semantic version string for ``entry``.
      digest: Content digest of the frozen runtime image.
      caller_host: Optional host mode (``""``, ``"codex"``, or ``"dsh"``).
      outer_identity: Live process identity of the front-door MCP server.
    """
    self.entry = entry
    self.version = version
    self.digest = digest
    self._lock = threading.Lock()
    self._call_lock = threading.Lock()
    self._inflight = 0
    self._retired = False
    self._closed = False
    self._serial = 0
    self._responses: queue.Queue[dict[str, Any] | None] = queue.Queue()

    env = dict(os.environ)
    env["DELEGATE_MCP_BACKEND"] = "1"
    env.pop("DELEGATE_MANAGED_ENTRY", None)
    if outer_identity is not None:
      env["DELEGATE_MCP_OUTER_IDENTITY"] = json.dumps(outer_identity, separators=(",", ":"))
    norm_host = "antigravity" if caller_host == "agy" else caller_host
    argv = [sys.executable, "-B", str(entry)]
    argv += ["host-mcp", norm_host] if norm_host in {"codex", "dsh", "antigravity"} else ["mcp"]
    self.proc = subprocess.Popen(
      argv,
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
      text=True,
      env=env,
      start_new_session=True,
    )
    self.proc_identity = lifecycle.identity(self.proc.pid, include_zombies=True)
    self._reader = threading.Thread(target=self._read_loop, daemon=True)
    self._reader.start()
    try:
      init_params = {"protocolVersion": "2024-11-05", "clientInfo": {"name": "delegate_hot_bridge", "version": version}}
      response = self.call("initialize", init_params, timeout=20.0)
      if not isinstance(response, dict) or response.get("serverInfo") != {"name": "delegate", "version": version}:
        raise ValueError("Hot-reload backend version does not match the selected release")
      self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    except BaseException:
      self.close()
      raise

  def _read_loop(self) -> None:
    """Read newline-delimited JSON-RPC responses from the child backend."""
    assert self.proc.stdout is not None
    try:
      for raw in self.proc.stdout:
        try:
          msg = json.loads(raw)
        except json.JSONDecodeError:
          continue
        if isinstance(msg, dict) and "id" in msg:
          self._responses.put(msg)
    finally:
      self._responses.put(None)

  def acquire(self) -> bool:
    """Reserve an active reference unless this backend is retired or closed.

    Returns:
      True when the reference was acquired, False if unavailable or retiring.
    """
    with self._lock:
      if self._retired or self._closed or self.proc.poll() is not None:
        return False
      self._inflight += 1
      return True

  def release(self) -> None:
    """Release an active reference and terminate the process once retired and idle.

    Returns:
      None.
    """
    should_close = False
    with self._lock:
      self._inflight = max(0, self._inflight - 1)
      if self._retired and self._inflight == 0 and not self._closed:
        should_close = True
    if should_close:
      self.close()

  def retire(self) -> None:
    """Mark this backend superseded and stop it immediately when idle.

    Returns:
      None.
    """
    should_close = False
    with self._lock:
      self._retired = True
      if self._inflight == 0 and not self._closed:
        should_close = True
    if should_close:
      self.close()

  def alive(self) -> bool:
    """Return whether the backend child process is still running.

    Returns:
      True while the backend has not been closed and has not exited.
    """
    with self._lock:
      return not self._closed and self.proc.poll() is None

  def send(self, payload: dict[str, Any]) -> None:
    """Write one JSON-RPC message to the child's stdin pipe.

    Args:
      payload: JSON-RPC message object to serialize and send.
    """
    assert self.proc.stdin is not None
    self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    self.proc.stdin.flush()

  def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
    """Execute one serialized JSON-RPC request and return its ``result`` dictionary.

    Args:
      method: JSON-RPC method name.
      params: Parameter dictionary passed to the child backend.
      timeout: Optional timeout in seconds for waiting on the reply.

    Returns:
      Decoded ``result`` field from the child's JSON-RPC response.

    Raises:
      RuntimeError: If the backend child process exits before replying.
      TimeoutError: If ``timeout`` expires before a matching reply arrives.
      ValueError: If the backend returns a JSON-RPC ``error`` payload.
    """
    with self._call_lock:
      if self.proc.poll() is not None:
        raise RuntimeError("Hot-reloaded delegate backend exited unexpectedly")
      self._serial += 1
      req_id = self._serial
      self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
      deadline = None if timeout is None else (time.monotonic() + timeout)
      while True:
        wait_s = 0.5 if deadline is None else min(0.5, max(0.05, deadline - time.monotonic()))
        try:
          msg = self._responses.get(timeout=wait_s)
        except queue.Empty:
          if self.proc.poll() is not None:
            raise RuntimeError("Hot-reloaded delegate backend terminated during request")
          if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"Hot-reloaded backend timed out waiting for {method}")
          continue
        if msg is None:
          raise RuntimeError("Hot-reloaded delegate backend closed stdout")
        if msg.get("id") != req_id:
          continue
        if "error" in msg:
          err = msg["error"]
          detail = err.get("message", "Backend JSON-RPC error") if isinstance(err, dict) else str(err)
          raise ValueError(detail)
        return msg.get("result")

  def close(self) -> None:
    """Stop the backend cleanly on EOF and ensure its process group and pipes are closed.

    Returns:
      None.
    """
    with self._lock:
      if self._closed:
        return
      self._closed = True
    try:
      if self.proc.stdin is not None and not self.proc.stdin.closed:
        self.proc.stdin.close()
    except OSError:
      pass
    try:
      self.proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
      if self.proc_identity is not None:
        try:
          lifecycle.stop_owned(self.proc, self.proc_identity)
        except (OSError, RuntimeError):
          pass
      if self.proc.poll() is None:
        try:
          self.proc.terminate()
          self.proc.wait(timeout=1.5)
        except (OSError, subprocess.TimeoutExpired):
          try:
            self.proc.kill()
            self.proc.wait(timeout=1.0)
          except (OSError, subprocess.TimeoutExpired):
            pass
    try:
      if self.proc.stdout is not None and not self.proc.stdout.closed:
        self.proc.stdout.close()
    except OSError:
      pass


class HotRuntimeBridge:
  """Front-door runtime supervisor supporting in-place zero-restart upgrades."""

  def __init__(self, self_entry: Path, self_version: str, origin_scripts: Path,
               caller_host: str = "") -> None:
    """Initialize the bridge around the currently executing frozen or source image.

    Args:
      self_entry: Active ``delegate.py`` path loaded by this process.
      self_version: Running semantic version string.
      origin_scripts: Original ``scripts/`` directory prior to ``runtime_image`` freeze.
      caller_host: Optional host identifier (``"codex"``, ``"dsh"``, or ``""``).
    """
    self.self_entry = self_entry.resolve()
    self.self_version = self_version
    self.origin_scripts = origin_scripts.resolve()
    self.caller_host = caller_host
    self.outer_identity = lifecycle.identity(os.getpid())
    self.enabled = should_auto_reload(self.origin_scripts)
    self.self_digest = self._read_self_digest()
    self.active_version = self_version
    self.active_digest = self.self_digest
    self._client: BackendClient | None = None
    self._retired_clients: list[BackendClient] = []
    self._state_lock = threading.Lock()
    self._refresh_lock = threading.Lock()
    self._last_check = 0.0
    self._last_fp: tuple[Any, ...] | None = None
    self._failed_fp: tuple[Any, ...] | None = None
    self._stop = threading.Event()
    self._watcher: threading.Thread | None = None

  def _read_self_digest(self) -> str:
    """Return the digest of the current process's frozen image, if frozen."""
    manifest = self.self_entry.parent / "runtime-image.json"
    if manifest.is_file():
      try:
        return str(json.loads(manifest.read_text()).get("digest") or "")
      except (OSError, ValueError):
        return ""
    return ""

  def _prune_retired(self) -> None:
    """Drop already-closed retired clients from the tracking list."""
    with self._state_lock:
      self._retired_clients = [c for c in self._retired_clients if c.alive()]

  def refresh(self, force: bool = False) -> bool:
    """Check whether a newer plugin install or local marketplace build is available and switch to it.

    Args:
      force: Bypass the short poll-interval throttle when True.

    Returns:
      True when the active backend digest/version changed.
    """
    if not self.enabled or self._stop.is_set():
      return False
    now = time.monotonic()
    with self._refresh_lock:
      if self._stop.is_set():
        return False
      if not force and (now - self._last_check) < 1.5:
        return False
      self._last_check = now
      self._prune_retired()
      target_entry, target_version = discover_target(self.origin_scripts / "delegate.py", self.self_version)
      target_semver = _parse_version(target_version)
      active_semver = _parse_version(self.active_version)
      if target_semver is None or (active_semver is not None and target_semver < active_semver):
        return False
      fp = (_stat_fingerprint(self.self_entry), str(target_entry), target_version,
            _max_py_mtime(target_entry.parent))
      cur_client = self._client
      cur_alive = True if cur_client is None else cur_client.alive()
      if not force and cur_alive and (fp == self._last_fp or fp == self._failed_fp):
        return False
      try:
        frozen = runtime_image.freeze(target_entry.parent)
        new_digest = str(frozen["digest"])
        frozen_entry = Path(frozen["path"]) / "delegate.py"
        if new_digest == self.active_digest and cur_alive:
          self._last_fp = fp
          self._failed_fp = None
          return False
        if new_digest == self.self_digest and self.self_digest and target_version == self.self_version:
          old = None
          with self._state_lock:
            if not self._stop.is_set():
              old = self._client
              self._client = None
              self.active_digest = self.self_digest
              self.active_version = target_version
              if old is not None:
                self._retired_clients.append(old)
          if old is not None:
            old.retire()
          self._last_fp = fp
          self._failed_fp = None
          return True
        if self._stop.is_set():
          return False
        new_client = BackendClient(frozen_entry, target_version, new_digest, self.caller_host, self.outer_identity)
      except (OSError, ValueError, SyntaxError, TimeoutError, RuntimeError):
        self._failed_fp = fp
        return False

      aborted = False
      old = None
      with self._state_lock:
        if self._stop.is_set():
          aborted = True
        else:
          old = self._client
          self._client = new_client
          self.active_digest = new_digest
          self.active_version = target_version
          if old is not None:
            self._retired_clients.append(old)
      if aborted:
        new_client.close()
        return False
      self._last_fp = fp
      self._failed_fp = None
      if old is not None:
        old.retire()
      return True

  def start_watcher(self, on_change: Callable[[], None], interval: float = 2.0) -> None:
    """Start a lightweight polling thread emitting ``on_change`` on hot upgrades.

    Args:
      on_change: Callback invoked whenever the active backend digest changes.
      interval: Polling interval in seconds between upgrade checks.
    """
    if not self.enabled or self._watcher is not None:
      return

    def loop() -> None:
      while not self._stop.wait(interval):
        try:
          if self.refresh():
            on_change()
        except Exception:
          continue

    self._watcher = threading.Thread(target=loop, daemon=True)
    self._watcher.start()

  def _acquire_client(self) -> BackendClient | None:
    """Obtain a live backend client if running a hot-reloaded runtime, bounded to two tries."""
    for attempt in range(2):
      with self._state_lock:
        if self._stop.is_set():
          return None
        client = self._client
        if client is None:
          return None
        if client.acquire():
          return client
      if attempt == 0:
        self.refresh(force=True)
    raise RuntimeError("Hot-reloaded delegate backend is unavailable; try /reload-plugins or retry")

  def list_tools(self, local_fn: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Return ``tools`` from the active hot-reloaded backend or the in-process handler.

    Args:
      local_fn: Zero-argument callable returning in-process MCP tool definitions.

    Returns:
      List of MCP tool schema dictionaries.

    Raises:
      RuntimeError: If an active hot-reloaded backend returns an invalid tool list.
    """
    self.refresh()
    client = self._acquire_client()
    if client is None:
      return local_fn()
    try:
      result = client.call("tools/list", {}, timeout=20.0)
      tools = result.get("tools") if isinstance(result, dict) else None
      if isinstance(tools, list):
        return tools
      raise RuntimeError("Invalid tools/list response from hot-reloaded backend")
    finally:
      client.release()

  def call_tool(self, name: str, arguments: dict[str, Any],
                local_fn: Callable[[str, dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Forward ``tools/call`` to the active hot-reloaded backend or execute in-process.

    Args:
      name: Delegate MCP tool name.
      arguments: Tool argument dictionary.
      local_fn: Fallback in-process executor returning an MCP tool result dict.

    Returns:
      MCP ``tools/call`` result dictionary.

    Raises:
      RuntimeError: If an active hot-reloaded backend fails or returns malformed output.
    """
    self.refresh()
    client = self._acquire_client()
    if client is None:
      return local_fn(name, arguments)
    try:
      result = client.call("tools/call", {"name": name, "arguments": arguments}, timeout=None)
      if isinstance(result, dict):
        return result
      raise RuntimeError("Invalid tools/call response from hot-reloaded backend")
    finally:
      client.release()

  def close(self) -> None:
    """Stop the background watcher and terminate any child backend processes cleanly.

    Returns:
      None.
    """
    self._stop.set()
    watcher = self._watcher
    if watcher is not None and watcher.is_alive() and threading.current_thread() is not watcher:
      watcher.join(timeout=3.0)
    with self._refresh_lock:
      with self._state_lock:
        clients = ([self._client] if self._client is not None else []) + list(self._retired_clients)
        self._client = None
        self._retired_clients = []
    for client in clients:
      client.close()

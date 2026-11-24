"""Zero-restart MCP hot-reload tests across plugin updates and local directory marketplaces."""
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import lifecycle
import mcp_reload
import runtime_gate
import runtime_image
import storage as st


def _copy_release(dest: Path, version: str, *, extra_tool_desc: str = "") -> Path:
  """Create a complete isolated delegate plugin release tree with a custom version."""
  pkg = dest / ("pkg-" + version)
  (pkg / ".claude-plugin").mkdir(parents=True, exist_ok=True)
  shutil.copytree(SCRIPTS, pkg / "scripts")
  (pkg / ".claude-plugin" / "plugin.json").write_text(
    json.dumps({"name": "delegate", "version": version, "description": "test"})
  )
  img_path = pkg / "scripts" / "runtime_image.py"
  img_text = img_path.read_text()
  img_path.write_text(re.sub(r'^VERSION\s*=\s*"[^"]+"', f'VERSION = "{version}"', img_text, count=1, flags=re.M))
  if extra_tool_desc:
    del_path = pkg / "scripts" / "delegate.py"
    del_text = del_path.read_text()
    del_path.write_text(del_text.replace("Check connected MCP", extra_tool_desc + " Check connected MCP"))
  return pkg


def _read_rpc_until(proc: subprocess.Popen, predicate: Callable[[list[dict[str, Any]]], bool],
                    timeout: float = 12.0) -> list[dict[str, Any]]:
  """Read JSON-RPC lines from ``proc.stdout`` until ``predicate(messages)`` holds.

  Args:
    proc: Live MCP server subprocess with readable ``stdout``.
    predicate: Stop condition evaluated against the collected JSON-RPC messages.
    timeout: Maximum wait duration in seconds.

  Returns:
    Accumulated parsed JSON-RPC message dictionaries.

  Raises:
    AssertionError: If ``timeout`` expires before ``predicate`` is satisfied.
  """
  assert proc.stdout is not None
  fd = proc.stdout.fileno()
  collected: list[dict[str, Any]] = []
  deadline = time.monotonic() + timeout
  buf = b""
  while time.monotonic() < deadline:
    remaining = max(0.05, deadline - time.monotonic())
    ready, _, _ = select.select([fd], [], [], remaining)
    if not ready:
      continue
    chunk = os.read(fd, 65536)
    if not chunk:
      break
    buf += chunk
    while b"\n" in buf:
      raw, buf = buf.split(b"\n", 1)
      text = raw.decode("utf-8", errors="replace").strip()
      if text:
        collected.append(json.loads(text))
    if predicate(collected):
      return collected
  raise AssertionError(f"Timed out waiting for JSON-RPC condition; got {collected!r}")


class McpHotReloadTests(unittest.TestCase):
  """Verify live MCP servers hot-upgrade without restarting the host session."""

  def setUp(self) -> None:
    """Create disposable state and plugin directories."""
    self.tmp = tempfile.TemporaryDirectory(prefix="delegate-hot-")
    self.root = Path(self.tmp.name)
    self.state_dir = self.root / "state"
    self.claude_dir = self.root / "claude"
    (self.claude_dir / "plugins").mkdir(parents=True, exist_ok=True)

  def tearDown(self) -> None:
    """Clean up disposable directories."""
    self.tmp.cleanup()

  def _register_user_plugin(self, pkg: Path, version: str, mkt: str = "local-delegation") -> None:
    """Write ``installed_plugins.json`` pointing user-scoped delegate at ``pkg``."""
    registry = self.claude_dir / "plugins" / "installed_plugins.json"
    registry.write_text(json.dumps({
      "version": 2,
      "plugins": {
        f"delegate@{mkt}": [{
          "scope": "user",
          "installPath": str(pkg),
          "version": version,
        }]
      },
    }))

  def test_mcp_hot_reloads_on_plugin_update_and_local_marketplace(self) -> None:
    """Upgrade a running MCP server 0.28.x -> 0.29.0 -> 0.29.1 in-place with notifications."""
    base_ver = runtime_image.VERSION
    v1 = _copy_release(self.root, base_ver)
    v2 = _copy_release(self.root, "0.29.0")
    self._register_user_plugin(v1, base_ver)
    env = {
      **os.environ,
      "DELEGATE_STATE_DIR": str(self.state_dir),
      "CLAUDE_CONFIG_DIR": str(self.claude_dir),
      "DELEGATE_MCP_HOT_RELOAD": "1",
    }
    proc = subprocess.Popen(
      [sys.executable, "-B", str(v1 / "scripts" / "delegate.py"), "mcp"],
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      env=env,
    )
    backend_pids: list[dict] = []
    try:
      assert proc.stdin is not None
      proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n").encode())
      proc.stdin.flush()
      msgs = _read_rpc_until(proc, lambda xs: any(m.get("id") == 1 for m in xs))
      init = next(m["result"] for m in msgs if m.get("id") == 1)
      self.assertTrue(init["capabilities"]["tools"]["listChanged"])
      self.assertEqual(init["serverInfo"]["version"], base_ver)

      # Initial handshake on v1
      proc.stdin.write((json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "delegate_control", "arguments": {"session_id": "s-hot", "action": "status"}},
      }) + "\n").encode())
      proc.stdin.flush()
      msgs2 = _read_rpc_until(proc, lambda xs: any(m.get("id") == 2 for m in xs))
      ctrl1 = json.loads(next(m["result"] for m in msgs2 if m.get("id") == 2)["content"][0]["text"])
      outer_id = ctrl1["server_runtime"]["identity"]
      self.assertEqual(ctrl1["server_runtime"]["version"], base_ver)
      self.assertFalse(ctrl1["server_runtime"]["hot_reloaded"])
      self.assertIn("mcp-hot-reload", ctrl1["server_runtime"]["capabilities"])

      # Persist handshake as PreToolUse/PostToolUse hooks would in Claude Code.
      hook_env = {"DELEGATE_STATE_DIR": str(self.state_dir), "CLAUDE_CONFIG_DIR": str(self.claude_dir)}
      with patch.dict(os.environ, hook_env):
        runtime_gate.observe({
          "hook_event_name": "PostToolUse", "session_id": "s-hot",
          "tool_name": "mcp__delegate__delegate_control",
          "tool_input": {"session_id": "s-hot", "action": "status"},
          "tool_response": {"content": [{"type": "text", "text": json.dumps(ctrl1)}]},
        })

      # 1. Simulate external `claude plugin update` to 0.29.0 while MCP keeps running!
      self._register_user_plugin(v2, "0.29.0")
      notif_msgs = _read_rpc_until(
        proc, lambda xs: any(m.get("method") == "notifications/tools/list_changed" for m in xs), timeout=10.0,
      )
      self.assertTrue(any(m.get("method") == "notifications/tools/list_changed" for m in notif_msgs))

      # Calling delegate_control(status) returns 0.29.0 while preserving outer identity!
      proc.stdin.write((json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "delegate_control", "arguments": {"session_id": "s-hot", "action": "status"}},
      }) + "\n").encode())
      proc.stdin.flush()
      msgs3 = _read_rpc_until(proc, lambda xs: any(m.get("id") == 3 for m in xs))
      ctrl2 = json.loads(next(m["result"] for m in msgs3 if m.get("id") == 3)["content"][0]["text"])
      self.assertEqual(ctrl2["server_runtime"]["version"], "0.29.0")
      self.assertTrue(ctrl2["server_runtime"]["hot_reloaded"])
      self.assertEqual(ctrl2["server_runtime"]["identity"], outer_id)

      # Only the host observation can update the runtime proof.
      with patch.dict(os.environ, hook_env):
        self.assertEqual(st.session("s-hot")["runtime_handshakes"]["mcp__delegate"]["version"], base_ver)

      # 2. Simulate editing a local directory marketplace to 0.29.1 even BEFORE plugin update!
      mkt_dir = self.root / "mkt-repo"
      (mkt_dir / ".claude-plugin").mkdir(parents=True)
      (mkt_dir / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "local-delegation",
        "plugins": [{"name": "delegate", "source": "./plugins/delegate"}],
      }))
      v3 = _copy_release(mkt_dir / "plugins", "0.29.1")
      (mkt_dir / "plugins" / "delegate").unlink() if (mkt_dir / "plugins" / "delegate").is_symlink() else None
      v3.rename(mkt_dir / "plugins" / "delegate")
      (self.claude_dir / "plugins" / "known_marketplaces.json").write_text(json.dumps({
        "local-delegation": {"source": {"source": "directory", "path": str(mkt_dir)}}
      }))

      notif2 = _read_rpc_until(
        proc, lambda xs: any(m.get("method") == "notifications/tools/list_changed" for m in xs), timeout=10.0,
      )
      self.assertTrue(any(m.get("method") == "notifications/tools/list_changed" for m in notif2))

      proc.stdin.write((json.dumps({
        "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {},
      }) + "\n").encode())
      proc.stdin.flush()
      msgs4 = _read_rpc_until(proc, lambda xs: any(m.get("id") == 4 for m in xs))
      tools_res = next(m["result"] for m in msgs4 if m.get("id") == 4)
      self.assertIn("delegate_run", {tool["name"] for tool in tools_res["tools"]})
      self.assertNotIn("delegate_host", {tool["name"] for tool in tools_res["tools"]})

      proc.stdin.write((json.dumps({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "delegate_control", "arguments": {"session_id": "s-hot", "action": "status"}},
      }) + "\n").encode())
      proc.stdin.flush()
      msgs5 = _read_rpc_until(proc, lambda xs: any(m.get("id") == 5 for m in xs))
      ctrl3 = json.loads(next(m["result"] for m in msgs5 if m.get("id") == 5)["content"][0]["text"])
      self.assertEqual(ctrl3["server_runtime"]["version"], "0.29.1")
      self.assertEqual(ctrl3["server_runtime"]["identity"], outer_id)

      # 3. Broken syntax edit in marketplace must NOT crash or break the running 0.29.1 server.
      broken_file = mkt_dir / "plugins" / "delegate" / "scripts" / "service.py"
      broken_file.write_text("def broken(:\n")
      os.utime(broken_file, None)
      proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 6, "method": "ping", "params": {}}) + "\n").encode())
      proc.stdin.flush()
      msgs6 = _read_rpc_until(proc, lambda xs: any(m.get("id") == 6 for m in xs))
      self.assertEqual(next(m["result"] for m in msgs6 if m.get("id") == 6), {})
      backend_pids.append(ctrl3["server_runtime"])
    finally:
      if proc.stdin and not proc.stdin.closed:
        proc.stdin.close()
      proc.wait(timeout=8)
      if proc.stdout and not proc.stdout.closed:
        proc.stdout.close()
      if proc.stderr and not proc.stderr.closed:
        proc.stderr.close()
    self.assertEqual(proc.returncode, 0)

  def test_downgrade_blocked_and_gate_requires_observed_runtime(self) -> None:
    """A registered upgrade alone cannot admit controls absent from the observed server."""
    old_pkg = _copy_release(self.root, "0.26.6")
    new_pkg = _copy_release(self.root, "0.26.8")
    self._register_user_plugin(old_pkg, "0.26.6")
    with patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.state_dir), "CLAUDE_CONFIG_DIR": str(self.claude_dir)}):
      target, ver = mcp_reload.discover_target(SCRIPTS / "delegate.py", "0.26.8")
      self.assertEqual(ver, "0.26.8")
      self.assertEqual(target, SCRIPTS / "delegate.py")

      # The installed release might have failed to load.
      self._register_user_plugin(new_pkg, "0.26.8")
      runtime_gate.observe({
        "hook_event_name": "PostToolUse", "session_id": "s-gate",
        "tool_name": "mcp__delegate__delegate_control",
        "tool_input": {"session_id": "s-gate", "action": "status"},
        "tool_response": {"content": [{"type": "text", "text": json.dumps({
          "session_id": "s-gate",
          "server_runtime": {
            "version": "0.26.6",
            "capabilities": [*runtime_gate.CAPABILITIES, "mcp-hot-reload"],
            "identity": lifecycle.identity(os.getpid()),
          },
        })}]},
      })
      denied = runtime_gate.rejection({
        "session_id": "s-gate",
        "tool_name": "mcp__delegate__delegate_run",
        "tool_input": {"session_id": "s-gate", "permissions": "network-shell"},
      })
      self.assertIn("predates delegate >=0.26.7", denied)

  def test_backend_rejects_a_manifest_that_mislabels_running_code(self) -> None:
    pkg = _copy_release(self.root, "0.29.0")
    version_file = pkg / "scripts" / "runtime_image.py"
    version_file.write_text(version_file.read_text().replace('VERSION = "0.29.0"', 'VERSION = "0.28.0"'))
    with patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.state_dir),
                                 "CLAUDE_CONFIG_DIR": str(self.claude_dir)}):
      with self.assertRaisesRegex(ValueError, "does not match the selected release"):
        mcp_reload.BackendClient(pkg / "scripts" / "delegate.py", "0.29.0", "fixture", "", None)

  def test_broken_import_upgrade_fails_closed_and_close_never_orphans_client(self) -> None:
    """Keep serving prior runtime on syntax-valid import failure and close cleanly mid-refresh."""
    base_ver = runtime_image.VERSION
    v1 = _copy_release(self.root, base_ver)
    v_bad = _copy_release(self.root, "0.29.0")
    svc = v_bad / "scripts" / "service.py"
    svc.write_text("import nonexistent_broken_module_xyz\n" + svc.read_text())
    self._register_user_plugin(v_bad, "0.29.0")
    env = {
      **os.environ,
      "DELEGATE_STATE_DIR": str(self.state_dir),
      "CLAUDE_CONFIG_DIR": str(self.claude_dir),
      "DELEGATE_MCP_HOT_RELOAD": "1",
    }
    reqs = "".join(json.dumps(r) + "\n" for r in (
      {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
      {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
      {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
       "params": {"name": "delegate_control", "arguments": {"session_id": "s-bad", "action": "status"}}},
    ))
    proc = subprocess.run(
      [sys.executable, "-B", str(v1 / "scripts" / "delegate.py"), "mcp"],
      input=reqs, capture_output=True, text=True, timeout=20, env=env,
    )
    self.assertEqual(proc.returncode, 0, proc.stderr)
    resps = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    self.assertEqual(len(resps), 3)
    self.assertEqual(resps[0]["result"]["serverInfo"]["version"], base_ver)
    self.assertIn("delegate_run", {tool["name"] for tool in resps[1]["result"]["tools"]})
    ctrl = json.loads(resps[2]["result"]["content"][0]["text"])
    self.assertEqual(ctrl["server_runtime"]["version"], base_ver)
    self.assertFalse(ctrl["server_runtime"]["hot_reloaded"])

    # Shutdown race: close() while BackendClient.__init__ is mid-flight must close the new client.
    self._register_user_plugin(v_bad, "0.29.0")
    entered = threading.Event()
    release_init = threading.Event()
    closed_instances: list[object] = []

    class FakeBackend:
      def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False
        entered.set()
        release_init.wait(timeout=5.0)
      def alive(self) -> bool:
        return not self.closed
      def retire(self) -> None:
        self.close()
      def close(self) -> None:
        self.closed = True
        closed_instances.append(self)

    with patch.dict(os.environ, env), patch("mcp_reload.BackendClient", FakeBackend):
      bridge = mcp_reload.HotRuntimeBridge(v1 / "scripts" / "delegate.py", base_ver, v1 / "scripts")
      t = threading.Thread(target=bridge.refresh, kwargs={"force": True})
      t.start()
      self.assertTrue(entered.wait(timeout=5.0))
      closer = threading.Thread(target=bridge.close)
      closer.start()
      release_init.set()
      t.join(timeout=5.0)
      closer.join(timeout=5.0)
      self.assertIsNone(bridge._client)
      self.assertEqual(len(closed_instances), 1)


if __name__ == "__main__":
  unittest.main()

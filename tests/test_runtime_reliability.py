"""Regression coverage for real startup diagnostics, frozen workers and inference-free waits."""
from pathlib import Path
from unittest.mock import patch, Mock
import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import codex_rpc
import diagnostics
import lifecycle
import runtime
import runtime_image
import service
import storage as st
import worker
import worker_wait


class ReliabilityTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-reliability-")
    self.root = Path(self.temp.name)
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.root / "state"), "CODEX_HOME": str(self.root / "codex")})
    self.env.start()
    self.job = {"id": "jfixture", "session_id": "s", "status": "running", "created_at": time.time(),
      "timeout_seconds": 0, "provider": {"kind": "codex", "command": "codex"}, "model": "fixture", "effort": "low",
      "role": {"workspace": "read-only"}, "cwd": str(self.root / "work"), "source_cwd": str(self.root / "work"),
      "cancel_requested": False, "messages": []}
    (self.root / "work").mkdir()
    st.write("job-jfixture.json", self.job)

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def fake(self, script: str) -> Path:
    path = self.root / "fake-codex"
    path.write_text("#!/usr/bin/env python3\n" + script)
    path.chmod(0o700)
    return path

  def test_initialize_crash_preserves_exit_code_and_redacted_stderr(self) -> None:
    fake = self.fake("import sys\nsys.stderr.write('startup failed token=fixture-secret\\n')\nsys.stderr.flush()\nsys.exit(23)\n")
    path = st.root() / "stderr-jfixture.txt"
    seen = []
    with patch.dict(os.environ, {"CUSTOM_API_KEY": "fixture-secret"}):
      with self.assertRaises(RuntimeError) as error:
        codex_rpc.CodexRPC(str(fake), on_spawn=lambda pid: seen.append(lifecycle.identity(pid)), stderr_path=path)
    self.assertIn("initialize", str(error.exception))
    self.assertIn("exit_code=23", str(error.exception))
    self.assertIn("startup failed", path.read_text())
    self.assertNotIn("fixture-secret", path.read_text() + str(error.exception))
    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
    self.assertFalse(lifecycle.group_running(seen[0]))

  def test_stderr_pipe_is_drained_and_bounded(self) -> None:
    fake = self.fake("import sys,json\nsys.stderr.write('X'*200000+'\\ntail\\n');sys.stderr.flush()\n"
      "for line in sys.stdin:\n m=json.loads(line)\n if m.get('method')=='initialize':print(json.dumps({'id':m['id'],'result':{}}),flush=True)\n")
    path = st.root() / "stderr-jfixture.txt"
    with codex_rpc.CodexRPC(str(fake), stderr_path=path):
      pass
    self.assertLess(path.stat().st_size, 66000)
    self.assertIn("tail", path.read_text())

  def test_auth_fifo_and_symlink_never_block_or_load_secrets(self) -> None:
    auth = self.root / "codex/auth.json"
    auth.parent.mkdir()
    os.mkfifo(auth)
    start = time.monotonic()
    self.assertEqual(diagnostics.secrets({"CODEX_HOME": str(auth.parent)}), [])
    self.assertLess(time.monotonic() - start, 1)
    auth.unlink()
    real = self.root / "real.json"
    real.write_text('{"access_token":"do-not-read"}')
    auth.symlink_to(real)
    self.assertEqual(diagnostics.secrets({"CODEX_HOME": str(auth.parent)}), [])

  def test_structured_unicode_token_redaction(self) -> None:
    read, write = os.pipe()
    with os.fdopen(read, "rb") as source:
      capture = diagnostics.StderrCapture(source, ["密钥secret"], st.root() / "stderr-jfixture.txt")
      os.write(write, json.dumps({"error": "密钥secret"}).encode())
      os.close(write)
      capture.close()
    self.assertNotIn("secret", (st.root() / "stderr-jfixture.txt").read_text())

  def test_unknown_rotated_credentials_are_redacted_by_field(self) -> None:
    read, write = os.pipe()
    with os.fdopen(read, "rb") as source:
      capture = diagnostics.StderrCapture(source, [], st.root() / "stderr-jfixture.txt")
      os.write(write, json.dumps({"oauth": {"refresh_token": "new-secret"}, "message": "failure"}).encode())
      os.close(write)
      capture.close()
    text = (st.root() / "stderr-jfixture.txt").read_text()
    self.assertNotIn("new-secret", text)
    self.assertIn("failure", text)

  def test_runtime_image_survives_source_edits_and_rejects_tampering(self) -> None:
    source = self.root / "source"
    source.mkdir()
    (source / "worker.py").write_text("VALUE = 1\n")
    image = runtime_image.freeze(source)
    directory = Path(image["path"])
    (source / "worker.py").write_text("VALUE = 2\n")
    self.assertEqual((directory / "worker.py").read_text(), "VALUE = 1\n")
    self.assertEqual(runtime_image.freeze(directory), image)
    self.assertNotEqual(runtime_image.freeze(source)["digest"], image["digest"])
    (directory / "worker.py").chmod(0o600)
    (directory / "worker.py").write_text("VALUE = 3\n")
    with self.assertRaisesRegex(ValueError, "mismatch"):
      runtime_image.verify(directory, image["digest"])

  def test_runtime_image_rejects_half_written_sources(self) -> None:
    source = self.root / "source"
    source.mkdir()
    (source / "worker.py").write_text("def incomplete(\n")
    with self.assertRaises(SyntaxError):
      runtime_image.freeze(source)

  def test_worker_rejects_wrong_runtime_before_prepare(self) -> None:
    image = runtime_image.freeze(SCRIPTS)
    st.update_job("jfixture", runtime_image=image)
    with patch("worker.prepare") as prepare:
      worker.main("jfixture")
    prepare.assert_not_called()
    self.assertEqual(st.job("jfixture")["status"], "failed")
    self.assertIn("runtime mismatch", st.job("jfixture")["error"])

  def test_legacy_parent_cannot_launch_new_worker_or_trigger_archive(self) -> None:
    st.update_job("jfixture", thread_id="previous-thread")
    with patch("worker.prepare") as prepare, patch("worker.thread_cleanup.archive") as archive, \
         patch("worker.codex_worker") as backend:
      worker.main("jfixture")
    prepare.assert_not_called()
    archive.assert_not_called()
    backend.assert_not_called()
    self.assertEqual(st.job("jfixture")["status"], "failed")
    self.assertIn("Legacy parent", st.job("jfixture")["error"])

  def test_file_wait_has_no_early_response_and_stops_without_inference(self) -> None:
    waiter = worker_wait.Waiter(self.job)
    message = {"id": 5, "params": {"arguments": {"path": "complete.json"}}}
    waiter.start(message)
    rpc = Mock()
    waiter.drain(rpc)
    rpc.send.assert_not_called()
    self.assertEqual(st.job("jfixture")["worker_wait"]["state"], "waiting")
    (Path(self.job["cwd"]) / "complete.json").write_text("{}")
    waiter.thread.join(timeout=4)
    waiter.drain(rpc)
    response = rpc.send.call_args.args[0]
    self.assertTrue(response["result"]["success"])
    self.assertIn("file_exists", str(response))
    self.assertEqual(st.job("jfixture")["worker_wait"]["inference_calls"], 0)
    waiter.close()

  def test_wait_cancellation_is_prompt_and_blocks_escape_and_unowned_pid(self) -> None:
    waiter = worker_wait.Waiter(self.job)
    for args in ({"path": "../../outside"}, {"pid": os.getpid()}, {"path": ".", "pid": 1}):
      with self.assertRaises(ValueError):
        waiter.start({"id": 1, "params": {"arguments": args}})
    link = Path(self.job["cwd"]) / "escape"
    link.symlink_to(self.root)
    with self.assertRaises(ValueError):
      waiter.start({"id": 1, "params": {"arguments": {"path": "escape/file"}}})
    waiter.start({"id": 1, "params": {"arguments": {"path": "never"}}})
    start = time.monotonic()
    waiter.close()
    self.assertLess(time.monotonic() - start, 1)
    self.assertFalse(waiter.thread.is_alive())

  def test_inspect_lists_owned_child_but_not_unrelated_process(self) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import subprocess,time; subprocess.Popen(['sleep','30']); print('ready',flush=True); time.sleep(30)"],
      start_new_session=True, stdout=subprocess.PIPE, text=True)
    record = lifecycle.identity(proc.pid, include_zombies=True)
    try:
      self.assertEqual(proc.stdout.readline().strip(), "ready")
      result = runtime.processes({"backend_identity": record})
      self.assertTrue(result["available"])
      self.assertGreaterEqual(result["live_child_count"], 1)
      self.assertNotIn(os.getpid(), [p["pid"] for p in result["processes"]])
      self.assertTrue(any(p["name"] == "sleep" for p in result["processes"]))
      sleeper = next(p for p in result["processes"] if p["name"] == "sleep")
      self.assertIn("sleep 30", sleeper["command_line"])
      self.assertEqual(sleeper["command_line_status"], "captured")
      summary = service.summary({**self.job, "shutdown_observation": result}, result=True)
      self.assertIn("sleep 30", json.dumps(summary["shutdown_observation"]["processes"]))
    finally:
      lifecycle.stop_owned(proc, record)
      proc.stdout.close()

  def test_warning_and_timeout_range_are_explicit(self) -> None:
    job = {"timeout_seconds": 300}
    self.assertFalse(runtime.warning_due(job, 0, 239))
    self.assertTrue(runtime.warning_due(job, 0, 240))
    self.assertFalse(runtime.warning_due({**job, "timeout_warning": {"timeout_seconds": 300}}, 0, 250))
    self.assertFalse(runtime.warning_due({"timeout_seconds": 0}, 0, 10000))
    with self.assertRaisesRegex(ValueError, "10..14400"):
      runtime.validate(18000, 14400)

  def test_catalog_advertises_roles_even_from_cached_model_list(self) -> None:
    st.write("catalog-codex.json", {"observed_at": time.time(), "models": []})
    value = service.catalog("codex")
    self.assertIn("codex-builder", [r["name"] for r in value["agents"]])
    self.assertEqual(value["timeout_seconds"]["unbounded"], 0)

  def test_state_path_is_canonical_through_symlink(self) -> None:
    link = self.root / "alias"
    link.symlink_to(st.root(), target_is_directory=True)
    with patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(link)}):
      self.assertEqual(st.root(), self.root / "state")

  def test_timeout_steers_then_interrupts_and_retains_shutdown_commands(self) -> None:
    job = {**self.job, "timeout_seconds": 20}
    st.write("job-jfixture.json", job)
    clock, calls = [0], []
    class RPC:
      def __init__(self, *a, **kw):
        self.deadline = kw["deadline"]
        self.cancelled = kw["cancelled"]
      def __enter__(self): return self
      def __exit__(self, *a): pass
      def call(self, method, params=None, timeout=30):
        calls.append(method)
        if method == "turn/interrupt":
          if self.cancelled or self.deadline:
            raise AssertionError("Interrupt retains an expired/cancelled RPC budget")
        return {"account": {"type": "chatgpt"}, "thread": {"id": "thread"}, "turn": {"id": "turn"}}
      def receive(self, timeout):
        if clock[0] == 0:
          clock[0] = 16
          return {"method": "item/started", "params": {"item": {"id": "cmd", "type": "commandExecution", "command": "replay"}}}
        if clock[0] == 16:
          clock[0] = 21
          return None
        return {"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "interrupted"}}}
    with patch("worker.CodexRPC", RPC), patch("worker.time.monotonic", side_effect=lambda: clock[0]):
      result = worker.codex_worker(job, "fixture")
    self.assertEqual(result["status"], "timed_out")
    self.assertLess(calls.index("turn/steer"), calls.index("turn/interrupt"))
    self.assertTrue(st.job("jfixture")["shutdown_observation"]["active_commands"])

  def test_dynamic_tool_request_during_rpc_call_is_preserved(self) -> None:
    rpc = codex_rpc.CodexRPC.__new__(codex_rpc.CodexRPC)
    from collections import deque
    rpc.deadline, rpc.cancelled, rpc.serial = None, None, 0
    rpc.pending, rpc.messages = deque(), queue.Queue()
    rpc.send, rpc.deny = Mock(), Mock()
    request = {"id": "server-tool", "method": "item/tool/call", "params": {"tool": "delegate_wait"}}
    rpc.messages.put(request)
    rpc.messages.put({"id": 1, "result": {}})
    rpc.call("turn/steer")
    rpc.deny.assert_not_called()
    self.assertEqual(rpc.receive(), request)

  def test_legacy_resume_brief_does_not_claim_new_tool_exists(self) -> None:
    self.assertIn("delegate_wait(path", worker.environment_brief(self.job))
    legacy = {**self.job, "thread_id": "old-thread", "worker_wait_supported": False}
    self.assertIn("has no delegate_wait", worker.environment_brief(legacy))


if __name__ == "__main__":
  unittest.main()

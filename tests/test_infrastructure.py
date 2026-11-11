"""Infrastructure failures must be recoverable without spending model retries."""
from pathlib import Path
from unittest.mock import Mock, patch
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import accounting as ac
import attempts
import lifecycle
import optimizer
import service
import delegate
import storage as st
import workspaces


class InfrastructureFixture(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory(prefix="delegate-infra-")
    self.root = Path(self.tmp.name)
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.root / "state")})
    self.env.start()

  def tearDown(self) -> None:
    self.env.stop()
    self.tmp.cleanup()

  def job(self, jid: str, **fields: object) -> dict:
    value = {"id": jid, "session_id": "s", "work_item": "daily-stats", "status": "failed",
      "provider": {"kind": "codex"}, "provider_name": "codex", "agent": "codex-reviewer",
      "model": "fixture-luna", "effort": "low", "review": "pending", "created_at": time.time(), **fields}
    st.write("job-" + jid + ".json", value)
    return value

class InfrastructureTests(InfrastructureFixture):
  def test_reported_three_preflight_failures_do_not_block_retry(self) -> None:
    errors = ["Legacy parent has no frozen runtime identity; restart its MCP server on the installed release"] * 2 + [
      "Command '['git', '-C', '/repo', 'worktree', 'add', '--detach', '/worktrees/j', 'abc']' timed out after 30 seconds"]
    jobs = [self.job("j" + str(i), error=error) for i, error in enumerate(errors)]
    self.assertIsNone(optimizer.work_state({"jobs": [j["id"] for j in jobs], "max_attempts": 3}, "daily-stats"))
    for job in jobs:
      self.assertFalse(attempts.model_attempted(job))
      self.assertEqual(ac.totals([ac.job_usage(job)])["processed_tokens"], 0)

  def test_uncertain_model_requests_still_count_without_usage(self) -> None:
    for fields in ({}, {"inference_state": "attempted"}, {"inference_state": "not_started", "turn_id": "turn"},
                   {"inference_state": "not_started", "usage_delta": {"inputTokens": 1, "outputTokens": 0}}):
      self.assertTrue(attempts.model_attempted(self.job("j", **fields)))
    self.assertIsNone(ac.job_usage(self.job("unknown", inference_state="attempted")))
    jobs = [self.job("a" + str(i), inference_state="attempted")["id"] for i in range(3)]
    self.assertEqual(optimizer.work_state({"jobs": jobs, "max_attempts": 3}, "daily-stats")["action"], "replan")

  def test_unrelated_git_or_model_timeouts_are_not_exempted(self) -> None:
    for fields in ({"error": "initialize timed out"},
      {"error": "Command '['git', '-C', '/repo', 'status', '--porcelain']' timed out after 30 seconds"},
      {"error": "Legacy parent has no frozen runtime identity; x", "execution_started_at": 10},
      {"error": "Legacy parent has no frozen runtime identity; x", "usage_delta": {"inputTokens": 10, "outputTokens": 0}}):
      self.assertTrue(attempts.model_attempted(self.job("j", **fields)))

  def test_infrastructure_is_not_a_model_quality_sample(self) -> None:
    history = {"t": {"category": "c", "complexity": "simple", "parent_share": 200, "rows": [
      {"agent": "a", "model": "m", "effort": "low", "tokens": 0, "accepted": False, "model_attempted": False},
      {"agent": "a", "model": "m", "effort": "low", "tokens": 100, "accepted": True, "model_attempted": True}]}}
    result = optimizer.observations({"category": "c", "complexity": "simple"}, "a", "m", "low", history)
    self.assertEqual(result["attempts"], 1)
    self.assertEqual(result["accepted"], 1)

  def test_parent_overhead_is_not_lost_when_infrastructure_rows_are_excluded(self) -> None:
    self.job("infra", inference_state="not_started")
    self.job("model", inference_state="attempted")
    task = {"id": "t", "session_id": "s", "status": "closed", "category": "c", "complexity": "simple",
      "jobs": ["infra", "model"], "requests": {}, "arm": "optimized", "verdict": "accepted"}
    report = {"parent": {"processed_tokens": 2000}, "measurement_complete": True, "total": {"processed_tokens": 2100}}
    with patch("accounting.report", return_value=report):
      ac.calibrate_locked(task)
    self.assertEqual(st.read("optimization-samples.json")["t"]["parent_share"], 2000)

  def test_configurable_preparation_timeout_has_real_bounds(self) -> None:
    self.assertEqual(st.config()["worktree_timeout_seconds"], 600)
    self.assertEqual(service.configure("limits", value={"worktree_timeout_seconds": 1800})["worktree_timeout_seconds"], 1800)
    for value in (True, 9, 86401):
      with self.assertRaises(ValueError):
        service.configure("limits", value={"worktree_timeout_seconds": value})

  def test_git_wait_survives_more_than_thirty_seconds_and_keeps_heartbeat(self) -> None:
    self.job("j", cancel_requested=False)
    clock = [0]
    proc = Mock(pid=123, returncode=0)
    calls = []
    def communicate(timeout):
      calls.append(timeout)
      if len(calls) == 1:
        clock[0] = 35
        raise subprocess.TimeoutExpired("git", timeout)
      return "ready", ""
    proc.communicate.side_effect = communicate
    with patch("workspaces.subprocess.Popen", return_value=proc), patch("workspaces.identity", return_value={"pid":123}), \
         patch("workspaces.stop_owned"), patch("workspaces.time.monotonic", side_effect=lambda: clock[0]):
      self.assertEqual(workspaces.run("j", str(self.root), "status"), "ready")
    self.assertEqual(len(calls), 2)
    self.assertIsNotNone(st.job("j")["heartbeat"])

  def test_current_cli_reads_legacy_session_state_without_model_calls(self) -> None:
    self.job("j", error="Legacy parent has no frozen runtime identity; old server")
    state = st.session("s")
    state["jobs"] = ["j"]
    st.save_session("s", state)
    payload = {"name": "delegate_job", "arguments": {"session_id": "s", "action": "result", "job_id": "j"}}
    result = subprocess.run([sys.executable, str(SCRIPTS / "delegate.py"), "call"],
      input=json.dumps(payload), capture_output=True, text=True, timeout=15, check=True)
    value = json.loads(result.stdout)
    self.assertEqual(value["session_id"], "s")
    self.assertFalse(value["attempt"]["counts_toward_model_attempt_limit"])
    self.assertEqual(value["observed_tokens"]["processed_tokens"], 0)

  def test_upgrade_hook_gives_one_cli_hint_without_replaying_job(self) -> None:
    self.job("j", error="Legacy parent has no frozen runtime identity; old server")
    state = st.session("s")
    state["jobs"] = ["j"]
    st.save_session("s", state)
    payload = {"session_id": "s", "hook_event_name": "PostToolUse"}
    with patch("delegate.accounting.hook"), patch("delegate.rewake.observe_collection"), \
         patch("delegate.handoff.sync"), patch("delegate.service.run") as run:
      first = delegate.hook(payload)
      second = delegate.hook(payload)
    self.assertIn("delegate-agent call", json.dumps(first))
    self.assertNotIn("delegate-agent call", json.dumps(second))
    run.assert_not_called()


class WorkspaceRecoveryTests(InfrastructureFixture):
  def setUp(self) -> None:
    super().setUp()
    self.repo = self.root / "repo"
    self.repo.mkdir()
    self.git(self.repo, "init", "-q")
    (self.repo / "file.txt").write_text("original\n")
    (self.repo / ".gitignore").write_text("cache/\n")
    self.git(self.repo, "add", "file.txt", ".gitignore")
    self.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    self.value = self.job("setup", source_cwd=str(self.repo), cwd=str(self.repo),
      role={"workspace": "worktree"}, inference_state="not_started", status="running", cancel_requested=False)

  def git(self, cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, timeout=10).stdout

  def test_real_checkout_and_registry_are_clean_and_unlocked(self) -> None:
    path = Path(workspaces.prepare(self.value))
    self.assertEqual((path / "file.txt").read_text(), "original\n")
    self.assertEqual(st.job("setup")["workspace_setup"]["status"], "ready")
    self.assertNotIn("locked", workspaces.registered("setup", str(self.repo), path))
    self.assertFalse(lifecycle.group_running(st.job("setup")["backend_identity"]))

  def test_finished_checkout_after_timeout_is_validated_and_reused(self) -> None:
    original = workspaces.run
    def timed(job_id, cwd, *args, **kwargs):
      output = original(job_id, cwd, *args, **kwargs)
      if args[:2] == ("worktree", "add"):
        target = Path(args[-2])
        self.git(self.repo, "worktree", "lock", "--reason", "initializing", str(target))
        raise subprocess.TimeoutExpired(["git", *args], 600)
      return output
    with patch("workspaces.run", side_effect=timed):
      path = Path(workspaces.prepare(self.value))
    self.assertTrue(path.is_dir())
    self.assertTrue(st.job("setup")["workspace_setup"]["recovered_after_timeout"])
    self.assertNotIn("locked", workspaces.registered("setup", str(self.repo), path))

  def test_partial_or_modified_checkout_is_unlocked_and_preserved(self) -> None:
    original = workspaces.run
    def timed(job_id, cwd, *args, **kwargs):
      output = original(job_id, cwd, *args, **kwargs)
      if args[:2] == ("worktree", "add"):
        target = Path(args[-2])
        (target / "file.txt").write_text("unfinished or user changes\n")
        self.git(self.repo, "worktree", "lock", "--reason", "initializing", str(target))
        raise subprocess.TimeoutExpired(["git", *args], 600)
      return output
    with patch("workspaces.run", side_effect=timed), self.assertRaises(subprocess.TimeoutExpired):
      workspaces.prepare(self.value)
    value = st.job("setup")
    path = Path(value["worktree"])
    self.assertEqual(value["workspace_setup"]["status"], "retained")
    self.assertTrue(value["workspace_setup"]["lock_released"])
    self.assertNotIn("locked", workspaces.registered("setup", str(self.repo), path))
    self.assertEqual((path / "file.txt").read_text(), "unfinished or user changes\n")

  def test_clean_failed_setup_is_removed_without_force(self) -> None:
    with patch("workspaces.verify", return_value=False), self.assertRaises(ValueError):
      workspaces.prepare(self.value)
    value = st.job("setup")
    self.assertEqual(value["workspace_setup"]["status"], "removed")
    self.assertFalse(Path(value["worktree"]).exists())
    self.assertEqual((self.repo / "file.txt").read_text(), "original\n")

  def test_ignored_artifacts_are_preserved_during_rollback(self) -> None:
    path = Path(workspaces.prepare(self.value))
    (path / "cache").mkdir()
    (path / "cache/important.txt").write_text("preserve")
    workspaces.rollback("setup", str(self.repo), path)
    self.assertEqual(st.job("setup")["workspace_setup"]["status"], "retained")
    self.assertEqual((path / "cache/important.txt").read_text(), "preserve")

  def test_foreign_lock_is_never_unlocked(self) -> None:
    path = Path(workspaces.prepare(self.value))
    self.git(self.repo, "worktree", "lock", "--reason", "user-investigation", str(path))
    workspaces.rollback("setup", str(self.repo), path)
    self.assertEqual(workspaces.registered("setup", str(self.repo), path)["locked"], "user-investigation")
    self.assertTrue(path.is_dir())

  def test_uncertain_checkout_spawn_blocks_cleanup(self) -> None:
    original = workspaces.run
    def uncertain(job_id, cwd, *args, **kwargs):
      if args[:2] == ("worktree", "add"):
        st.update_job(job_id, backend_starting=True)
        raise RuntimeError("spawn ownership uncertain")
      return original(job_id, cwd, *args, **kwargs)
    with patch("workspaces.run", side_effect=uncertain), patch("workspaces.rollback") as cleanup:
      with self.assertRaises(RuntimeError):
        workspaces.prepare(self.value)
    cleanup.assert_not_called()
    self.assertEqual(st.job("setup")["workspace_setup"]["status"], "cleanup-unconfirmed")

  def test_owned_git_timeout_stops_its_child_processes(self) -> None:
    original = subprocess.Popen
    script = "import subprocess,time;subprocess.Popen(['sleep','30']);time.sleep(30)"
    def spawn(*args, **kwargs):
      return original([sys.executable, "-c", script], **kwargs)
    with patch("workspaces.subprocess.Popen", side_effect=spawn), self.assertRaises(subprocess.TimeoutExpired):
      workspaces.run("setup", str(self.repo), "status", timeout=0.2)
    record = st.job("setup")["workspace_git_identity"]
    self.assertFalse(lifecycle.group_running(record))

  def test_cancellation_during_git_stops_owned_group(self) -> None:
    original = subprocess.Popen
    def spawn(*args, **kwargs):
      return original([sys.executable, "-c", "import subprocess,time;subprocess.Popen(['sleep','30']);time.sleep(30)"], **kwargs)
    def cancel():
      deadline = time.monotonic() + 5
      while time.monotonic() < deadline:
        if st.job("setup").get("workspace_git_identity"):
          st.update_job("setup", cancel_requested=True)
          return
        time.sleep(0.01)
    thread = threading.Thread(target=cancel)
    thread.start()
    try:
      with patch("workspaces.subprocess.Popen", side_effect=spawn), self.assertRaises(InterruptedError):
        workspaces.run("setup", str(self.repo), "status", timeout=10)
    finally:
      thread.join(timeout=6)
    self.assertFalse(lifecycle.group_running(st.job("setup")["workspace_git_identity"]))


if __name__ == "__main__":
  unittest.main()

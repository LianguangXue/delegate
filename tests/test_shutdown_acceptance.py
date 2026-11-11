"""Shutdown helpers must not masquerade as work; acceptance conflicts stay explicit."""
from pathlib import Path
from unittest.mock import patch
import copy
import os
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import runtime
import service
import storage as st
import worker


def row(pid: int, ppid: int, executable: str, argv: list[str]) -> dict:
  """Build a captured fixture independent of live host processes."""
  return {"pid": pid, "ppid": ppid, "name": Path(executable).name, "executable": executable,
    "argv": argv, "command_line": " ".join(argv), "command_line_status": "captured",
    "arguments_truncated": False, "identity": {"pid": pid, "start": "fixture", "boot": "fixture"}}


class ShutdownAcceptanceTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-shutdown-acceptance-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.temp.name})
    self.env.start()
    self.job = {"id": "jfixture", "session_id": "s", "status": "completed", "timeout_seconds": 0,
      "created_at": time.time(), "provider": {"kind": "codex"},
      "role": {"instructions": "Review", "workspace": "read-only"},
      "source_cwd": self.temp.name, "cwd": self.temp.name, "task": "Check mapping",
      "acceptance": "1105 mapping equals the old result", "context": "The old mapping may be wrong"}

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def test_known_helpers_do_not_warn_but_raw_count_and_commands_remain(self) -> None:
    rows = [row(2, 1, "/vendor/bin/codex", ["/vendor/bin/codex", "app-server", "--stdio"]),
      row(3, 2, "/vendor/bin/codex-code-mode", ["/vendor/bin/codex-code-mode"])]
    launcher = row(1, 0, "/usr/bin/node", ["node", "/bin/codex", "app-server", "--stdio"])
    runtime.classify_processes([launcher, *rows], "codex", 1)
    self.assertTrue(all(r["process_kind"] == "runtime_helper" for r in rows))
    observation = {"available": True, "observation_version": 2, "live_child_count": 2,
      "runtime_helper_count": 2, "workload_or_unknown_count": 0, "processes": rows, "active_commands": []}
    job = {**self.job, "shutdown_observation": observation}
    self.assertEqual(runtime.observe(job)["warnings"], [])
    summary = service.summary(job, result=True)["shutdown_observation"]
    self.assertEqual(summary["runtime_helper_count"], 2)
    self.assertIn("app-server --stdio", summary["processes"][0]["command_line"])
    self.assertEqual(summary["processes"][1]["process_kind"], "runtime_helper")

  def test_same_name_wrong_parent_path_mode_or_missing_argv_remain_unknown(self) -> None:
    server = row(2, 1, "/vendor/bin/codex", ["codex", "app-server", "--stdio"])
    unknowns = [row(3, 2, "/other/codex-code-mode", ["codex-code-mode"]),
      row(4, 1, "/vendor/bin/codex-code-mode", ["codex-code-mode"]),
      row(5, 2, "/vendor/bin/codex", ["codex", "exec", "task"]),
      {**row(6, 2, "/vendor/bin/codex-code-mode", []), "command_line_status": "unavailable"},
      row(7, 2, "/usr/bin/python3", ["codex", "app-server", "--stdio"]),
      {**row(8, 2, "/vendor/bin/codex-code-mode", ["codex-code-mode"]), "arguments_truncated": True},
      row(9, 2, "/usr/bin/python3", ["python3", "replay.py"])]
    runtime.classify_processes([server, *unknowns], "codex", 2)
    self.assertTrue(all(r["process_kind"] == "workload_or_unknown" for r in unknowns))
    runtime.classify_processes([server], "anthropic", 2)
    self.assertEqual(server["process_kind"], "workload_or_unknown")

  def test_nested_app_servers_are_not_backend_helpers(self) -> None:
    launcher = row(1, 0, "/usr/bin/node", ["node", "/bin/codex", "app-server", "--stdio"])
    server = row(2, 1, "/vendor/bin/codex", ["codex", "app-server", "--stdio"])
    nested = [row(3, 2, "/vendor/bin/codex", ["codex", "app-server", "--stdio"]),
      row(4, 3, "/vendor/bin/codex-code-mode", ["codex-code-mode"]),
      row(5, 2, "/other/codex", ["codex", "app-server", "--stdio"])]
    for leader_pid in (1, 2):
      runtime.classify_processes([launcher, server, *nested], "codex", leader_pid)
      self.assertEqual(server["process_kind"], "runtime_helper")
      self.assertTrue(all(r["process_kind"] == "workload_or_unknown" for r in nested))
    runtime.classify_processes([server, *nested], "codex", 1)
    self.assertTrue(all(r["process_kind"] == "workload_or_unknown" for r in [server, *nested]))

  def test_task_commands_unknowns_and_legacy_snapshots_keep_distinct_warnings(self) -> None:
    base = {"observation_version": 2, "live_child_count": 2, "runtime_helper_count": 2,
      "workload_or_unknown_count": 0, "active_commands": []}
    for change in ({"workload_or_unknown_count": 1}, {"active_commands": [{"command": "replay"}]}):
      warnings = runtime.observe({**self.job, "shutdown_observation": {**base, **change}})["warnings"]
      self.assertTrue(any("verify completion" in w for w in warnings))
    legacy = {"live_child_count": 2, "processes": [{"name": "codex"}, {"name": "codex-code-mode"}]}
    warnings = runtime.observe({**self.job, "shutdown_observation": legacy})["warnings"]
    self.assertIn("legacy", warnings[0])
    self.assertIn("does not prove unfinished", warnings[0])

  def test_summary_prioritizes_unknown_workload_over_helpers(self) -> None:
    helpers = [{**row(i, 1, "/vendor/bin/codex", ["codex", "app-server", "--stdio"]),
      "process_kind": "runtime_helper"} for i in range(2, 12)]
    workload = {**row(12, 1, "/usr/bin/python3", ["python3", "replay.py"]), "process_kind": "workload_or_unknown"}
    summary = service.summary({**self.job, "shutdown_observation": {"processes": helpers + [workload],
      "live_child_count": 11}}, result=True)["shutdown_observation"]
    self.assertEqual(summary["processes"][0]["pid"], 12)
    self.assertEqual(summary["processes_omitted_from_summary"], 3)

  def test_cleanup_checks_original_identities_without_hiding_live_helpers(self) -> None:
    processes = [row(2, 1, "/vendor/bin/codex", ["codex", "app-server", "--stdio"]),
      row(3, 2, "/vendor/bin/codex-code-mode", ["codex-code-mode"])]
    for live_pid, expected in ((None, "observed_processes_stopped"), (3, "processes_still_alive")):
      job = {**self.job, "backend_identity": {"pid": 1}, "shutdown_observation": {
        "available": True, "live_child_count": 2, "processes": copy.deepcopy(processes)}}
      st.write("job-jfixture.json", job)
      with patch("runtime.alive", side_effect=lambda r: bool(r and r["pid"] == live_pid)):
        runtime.finalize_shutdown(job)
      result = st.job("jfixture")
      cleanup = result["shutdown_observation"]["cleanup"]
      self.assertEqual(cleanup["status"], expected)
      self.assertEqual(result["shutdown_observation"]["live_child_count"], 2)
      if live_pid:
        self.assertEqual(cleanup["live_child_pids"], [3])
        self.assertIn("remain alive after teardown", runtime.observe(result)["warnings"][0])

  def test_cleanup_incomplete_or_denied_does_not_claim_stopped(self) -> None:
    for observation in ({"available": False}, {"available": True, "live_child_count": 2, "processes": []},
        {"available": True, "omitted": 1},
        {"available": True, "processes": [row(2, 1, "/bin/sleep", ["sleep", "30"])]}):
      job = {**self.job, "shutdown_observation": observation}
      st.write("job-jfixture.json", job)
      with patch("runtime.alive", side_effect=PermissionError):
        runtime.finalize_shutdown(job)
      self.assertEqual(st.job("jfixture")["shutdown_observation"]["cleanup"]["status"], "unknown")

  def test_acceptance_policy_is_in_every_backend_fresh_and_resumed_packet(self) -> None:
    for kind in ("codex", "claude", "anthropic"):
      for resume in (None, "jprevious"):
        job = {**self.job, "provider": {"kind": kind}, "resume_job": resume}
        packet = worker.packet(job)
        self.assertIn(worker.ACCEPTANCE_POLICY, packet)
        self.assertIn(self.job["acceptance"], packet)
        self.assertIn("Mark it CONFLICT", packet)
        self.assertIn("never PASS", packet)
        self.assertIn("Only an explicit parent revision", packet)
        self.assertIn("not assumed ground truth", packet)


if __name__ == "__main__":
  unittest.main()

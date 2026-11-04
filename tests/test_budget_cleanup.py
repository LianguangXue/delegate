"""Quota-driven routing and finished-thread lifecycle regression checks."""
from pathlib import Path
from unittest.mock import patch
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"))
import budget
import delegate
import service
import storage as st
import thread_cleanup


class BudgetTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-budget-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.temp.name})
    self.env.start()
    self.refresh = patch("budget.schedule_refresh")
    self.refresh.start()

  def tearDown(self) -> None:
    self.refresh.stop()
    self.env.stop()
    self.temp.cleanup()

  def sample(self, remaining: float, sid: str = "s1") -> None:
    budget.capture({"session_id": sid, "rate_limits": {
      "five_hour": {"used_percentage": 100 - remaining, "resets_at": time.time() + 300}}})

  def test_low_and_critical_quota_change_main_thread_instructions(self) -> None:
    service.control("s1", "on")
    self.sample(15)
    self.assertEqual(budget.recommendation("s1")["mode"], "external-preferred")
    hook = delegate.hook({"session_id": "s1", "hook_event_name": "PostToolUse"})
    self.assertIn("most independent", json.dumps(hook))
    self.sample(3)
    hook = delegate.hook({"session_id": "s1", "hook_event_name": "PostToolUse"})
    self.assertIn("handoff", json.dumps(hook))
    self.assertIn("critical", json.dumps(hook))

  def test_off_stays_off_even_when_quota_is_critical(self) -> None:
    self.sample(1)
    self.assertEqual(budget.recommendation("s1")["mode"], "disabled")
    self.assertEqual(delegate.hook({"session_id": "s1", "hook_event_name": "PostToolUse"}), {})

  def test_unknown_is_explicit_and_conserves_main_only_after_opt_in(self) -> None:
    service.control("s1", "on")
    delegate.statusline({"session_id": "s1", "context_window": {"used_percentage": 99}})
    result = budget.recommendation("s1")
    self.assertIsNone(result["claude_remaining_percent"])
    self.assertIn("unknown", result["reason"])
    self.assertEqual(result["mode"], "external-preferred")

  def test_fresh_high_quota_restores_balanced(self) -> None:
    service.control("s1", "on")
    self.sample(4)
    self.sample(90)
    self.assertEqual(budget.recommendation("s1")["mode"], "balanced")

  def test_session_isolation_and_stale_or_reset_windows(self) -> None:
    self.sample(5)
    self.assertEqual(budget.read_claude("s2")["status"], "unknown")
    sample = st.read("claude-quota-s1.json")
    sample["observed_at"] -= 301
    st.write("claude-quota-s1.json", sample)
    self.assertEqual(budget.read_claude("s1")["status"], "stale")
    sample["data"]["five_hour"]["resets_at"] = time.time() - 1
    sample["observed_at"] = time.time() - 10
    self.assertIsNone(budget.remaining(sample, "claude"))

  def test_fresh_backend_limit_is_not_assumed_reset_from_rounded_display_time(self) -> None:
    sample = {"status": "known", "observed_at": time.time(),
              "data": {"five_hour": {"used_percentage": 100, "resets_at": time.time() - 30}}}
    self.assertEqual(budget.remaining(sample, "claude"), 0)

  def test_external_pool_and_codex_reserve_are_respected(self) -> None:
    service.control("s1", "on", agents=["claude-explorer"])
    self.sample(1)
    self.assertEqual(budget.recommendation("s1")["mode"], "no-external-capacity")
    service.control("s1", "on", agents=["codex-reviewer"])
    st.write("quota-codex.json", {"status": "known", "observed_at": time.time(),
      "data": {"rateLimits": {"primary": {"usedPercent": 97}}}})
    self.assertEqual(budget.recommendation("s1")["external_agents"], [])

  def test_manual_quota_is_labelled_expires_and_survives_missing_fields(self) -> None:
    budget.report("s1", 12, 60)
    budget.capture({"session_id": "s1"})
    self.assertEqual(budget.read_claude("s1")["source"], "user-reported")
    self.assertEqual(budget.remaining(budget.read_claude("s1"), "claude"), 12)
    self.sample(70)
    self.assertEqual(budget.read_claude("s1")["source"], "Claude statusline")
    with self.assertRaises(ValueError):
      budget.report("s1", float("nan"))

  def test_native_usage_parser_reads_real_windows_not_diagnostic_shares(self) -> None:
    text = ('Current session: 100% used · resets Oct 26, 10:10pm (Asia/Shanghai)\n'
            'Current week (all models): 39% used · resets Nov 2, 7am (Asia/Shanghai)\n'
            '  Top subagents: general-purpose 72%, Plan 1%\n')
    value = budget.parse_usage(text)
    self.assertEqual(value["status"], "known")
    self.assertEqual(value["data"]["five_hour"]["used_percentage"], 100)
    self.assertEqual(value["data"]["seven_day"]["used_percentage"], 39)
    self.assertIn("resets_at", value["data"]["five_hour"])
    self.assertEqual(budget.parse_usage('Top subagents: 72%')["status"], "unknown")

  def test_native_usage_cache_is_shared_without_overwriting_session_identity(self) -> None:
    value = budget.parse_usage('Current session: 90% used\nCurrent week (all models): 39% used\n')
    st.write("claude-usage.json", value)
    budget.capture({"session_id": "s1"})
    self.assertEqual(budget.remaining(budget.read_claude("s1"), "claude"), 10)
    self.assertEqual(budget.read_claude("s2")["source"], "Claude CLI /usage")

  def test_native_usage_timeout_does_not_fabricate_a_balance(self) -> None:
    import subprocess
    with patch('budget.native_usage', side_effect=subprocess.TimeoutExpired('claude', 12)):
      self.assertEqual(budget.refresh_cli(True)["status"], "unknown")


class ArchiveTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-archive-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.temp.name})
    self.env.start()
    self.value = {"id": "j1", "session_id": "s1", "thread_id": "thread1", "status": "completed",
                  "provider": {"kind": "codex", "command": "codex"}, "result": "preserve me"}
    st.write("job-j1.json", self.value)
    st.save_session("s1", {"mode": "off", "jobs": ["j1"]})

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def test_archive_readback_and_restore_preserve_job_result(self) -> None:
    with patch("thread_cleanup.CodexRPC") as cls:
      rpc = cls.return_value.__enter__.return_value
      rpc.call.side_effect = [
        {"thread": {"originator": "claude_delegate", "path": "/home/codex/sessions/t.jsonl", "status": {"type": "notLoaded"}}}, {},
        {"thread": {"path": "/home/codex/archived_sessions/t.jsonl"}}]
      receipt = thread_cleanup.archive("j1")
      self.assertEqual(receipt["status"], "archived")
      self.assertIn("thread/archive", [c.args[0] for c in rpc.call.call_args_list])
      self.assertEqual(st.job("j1")["result"], "preserve me")
      rpc.call.reset_mock()
      rpc.call.side_effect = [{"thread": {"path": "/home/codex/archived_sessions/t.jsonl"}}, {}]
      thread_cleanup.restore(rpc, "thread1", "s1")
      self.assertEqual(rpc.call.call_args_list[-1].args[0], "thread/unarchive")

  def test_running_sibling_blocks_cleanup_before_any_rpc(self) -> None:
    st.write("job-j2.json", dict(self.value, id="j2", status="queued"))
    st.save_session("s1", {"mode": "off", "jobs": ["j1", "j2"]})
    with patch("thread_cleanup.CodexRPC") as cls:
      with self.assertRaisesRegex(ValueError, "still using"):
        thread_cleanup.archive("j1")
      cls.assert_not_called()

  def test_archive_failure_is_not_reported_as_cleaned(self) -> None:
    with patch("thread_cleanup.CodexRPC") as cls:
      cls.return_value.__enter__.return_value.call.side_effect = RuntimeError("busy")
      with self.assertRaises(RuntimeError):
        thread_cleanup.archive("j1")
      self.assertEqual(st.job("j1")["codex_session"]["status"], "pending")
      self.assertEqual(st.job("j1")["status"], "orphaned")

  def test_unrelated_thread_originator_is_not_archived(self) -> None:
    with patch("thread_cleanup.CodexRPC") as cls:
      rpc = cls.return_value.__enter__.return_value
      rpc.call.return_value = {"thread": {"originator": "codex_cli_rs", "path": "/home/codex/sessions/user.jsonl"}}
      with self.assertRaisesRegex(ValueError, "originator"):
        thread_cleanup.archive("j1")
      self.assertEqual([c.args[0] for c in rpc.call.call_args_list], ["thread/read"])

  def test_resume_relabelled_originator_keeps_ownership_tag(self) -> None:
    with patch("thread_cleanup.CodexRPC") as cls:
      rpc = cls.return_value.__enter__.return_value
      rpc.call.side_effect = [
        {"thread": {"originator": "codex_vscode", "threadSource": "claude_delegate",
                    "path": "/home/codex/sessions/t.jsonl"}}, {},
        {"thread": {"path": "/home/codex/archived_sessions/t.jsonl"}}]
      self.assertEqual(thread_cleanup.archive("j1")["status"], "archived")

  def test_legacy_resume_remembers_verified_owner_before_relabel(self) -> None:
    with patch("thread_cleanup.CodexRPC") as cls:
      rpc = cls.return_value.__enter__.return_value
      rpc.call.side_effect = [{"thread": {"originator": "claude_delegate", "path": "/x/archived_sessions/t"}}, {}]
      thread_cleanup.restore(rpc, "thread1", "s1")
      self.assertTrue(thread_cleanup.ownership({"originator": "codex_vscode"}, "s1", "thread1"))
      self.assertFalse(thread_cleanup.ownership({"originator": "codex_vscode"}, "other-session", "thread1"))

  def test_thread_lock_blocks_resume_registration(self) -> None:
    @thread_cleanup.guard_resume
    def resume(resume_job: str) -> str:
      return "unexpected"
    with thread_cleanup.guard("thread1"):
      with self.assertRaisesRegex(ValueError, "in progress"):
        resume("j1")


if __name__ == "__main__":
  unittest.main()

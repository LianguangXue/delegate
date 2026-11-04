"""Conversation archival preserves exact files and refuses live/unowned histories."""
from pathlib import Path
from unittest.mock import patch
import json
import os
import re
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"))
import cli_history
import storage as st
import thread_cleanup
import rewake


class HistoryTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-history-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.temp.name})
    self.env.start()
    home = st.root() / "backend-state" / "fixture"
    self.value = {"id": "jfixture", "session_id": "s", "thread_id": "fixture-thread",
                  "provider": {"kind": "anthropic"}, "backend_config_dir": str(home),
                  "cwd": self.temp.name, "status": "completed"}
    project = home / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", self.temp.name)
    project.mkdir(parents=True)
    self.history = project / "fixture-thread.jsonl"
    self.history.write_text('{"fixture":"conversation"}\n')
    self.child = self.history.with_suffix("") / "subagents" / "child.jsonl"
    self.child.parent.mkdir(parents=True)
    self.child.write_text("fixture-child")
    st.write("job-jfixture.json", self.value)
    st.save_session("s", {"mode": "off", "jobs": ["jfixture"]})

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def test_archive_restore_and_rearchive_exact_unit(self) -> None:
    receipt = thread_cleanup.archive("jfixture")
    self.assertEqual(receipt["status"], "archived")
    self.assertFalse(self.history.exists())
    self.assertFalse(self.child.exists())
    cli_history.archive("jfixture")  # Idempotent replay.
    cli_history.restore({**self.value, "resume_job": "jfixture"})
    self.assertEqual(self.history.read_text(), '{"fixture":"conversation"}\n')
    self.assertEqual(self.child.read_text(), "fixture-child")
    cli_history.archive("jfixture")
    self.assertFalse(self.history.exists())

  def test_active_sibling_and_backend_block_archive(self) -> None:
    st.update_job("jfixture", status="running")
    with self.assertRaisesRegex(ValueError, "still in use"):
      cli_history.archive("jfixture")
    with patch("cli_history.group_running", return_value=True):
      with self.assertRaisesRegex(ValueError, "still in use"):
        cli_history.archive("jfixture", finishing=True)
    self.assertTrue(self.history.exists())

  def test_partial_move_recovers_from_journal(self) -> None:
    rename = os.rename
    calls = 0
    def interrupted(source, target):
      nonlocal calls
      calls += 1
      if calls == 2:
        raise OSError("fixture interruption")
      rename(source, target)
    with patch("cli_history.os.rename", side_effect=interrupted):
      with self.assertRaisesRegex(OSError, "interruption"):
        cli_history.archive("jfixture")
    self.assertFalse(self.history.exists())
    cli_history.restore({**self.value, "resume_job": "jfixture"})
    self.assertTrue(self.history.exists())
    self.assertTrue(self.child.exists())

  def test_companion_created_after_resume_is_archived(self) -> None:
    self.child.unlink(); self.child.parent.rmdir(); self.child.parent.parent.rmdir()
    cli_history.archive("jfixture")
    cli_history.restore({**self.value, "resume_job": "jfixture"})
    self.child.parent.mkdir(parents=True)
    self.child.write_text("new companion")
    cli_history.archive("jfixture")
    self.assertFalse(self.child.exists())
    cli_history.restore({**self.value, "resume_job": "jfixture"})
    self.assertEqual(self.child.read_text(), "new companion")

  def test_restore_waits_for_dispatch_registration_lock(self) -> None:
    cli_history.archive("jfixture")
    lock = str(st.root() / "thread-fixture-thread.lock")
    proc = subprocess.Popen([sys.executable, "-c",
      "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); "
      "print('ready',flush=True); time.sleep(.3)", lock], stdout=subprocess.PIPE, text=True)
    try:
      self.assertEqual(proc.stdout.readline().strip(), "ready")
      cli_history.restore({**self.value, "resume_job": "jfixture"})
      self.assertTrue(self.history.exists())
    finally:
      proc.wait(timeout=3)
      proc.stdout.close()

  def test_legacy_delivery_archives_and_retries_pending_history(self) -> None:
    response = {**self.value, "result": "fixture"}
    payload = {"session_id": "s", "hook_event_name": "PostToolUse",
               "tool_name": "mcp__x__delegate_job",
               "tool_input": {"session_id": "s", "action": "result", "job_id": "jfixture"},
               "tool_response": response}
    with patch("thread_cleanup.archive", side_effect=ValueError("fixture busy")):
      rewake.observe_collection(payload)
    self.assertEqual(st.job("jfixture")["cli_session"]["status"], "pending")
    self.assertTrue(st.job("jfixture")["collected_at"])
    rewake.observe_collection(payload)
    self.assertEqual(st.job("jfixture")["cli_session"]["status"], "archived")
    self.assertFalse(self.history.exists())

  def test_pretool_restore_supports_legacy_mcp_and_blocks_active_siblings(self) -> None:
    st.update_job("jfixture", agent="fixture-role", source_cwd=self.temp.name)
    cfg = st.config()
    cfg["agents"]["fixture-role"] = {"provider": "claude", "enabled": True, "automatic": True}
    st.write("config.json", cfg)
    cli_history.archive("jfixture")
    payload = {"session_id": "s", "tool_name": "mcp__x__delegate_run",
               "tool_input": {"session_id": "s", "resume_job": "jfixture", "agent": "fixture-role",
                              "cwd": self.temp.name, "one_shot": True}}
    cli_history.prepare_resume({**payload, "session_id": "other"})
    self.assertFalse(self.history.exists())
    st.write("job-jactive.json", {**self.value, "id": "jactive", "status": "running"})
    st.save_session("s", {"mode": "off", "jobs": ["jfixture", "jactive"]})
    with self.assertRaisesRegex(ValueError, "still in use"):
      cli_history.prepare_resume(payload)
    st.update_job("jactive", status="completed")
    cli_history.prepare_resume(payload)
    self.assertTrue(self.history.exists())
    with self.assertRaisesRegex(ValueError, "Resume is prepared"):
      cli_history.archive("jfixture")
    st.write("job-jnext.json", {**self.value, "id": "jnext", "status": "completed"})
    state = st.session("s"); state["jobs"].append("jnext"); st.save_session("s", state)
    cli_history.archive("jnext")
    self.assertFalse(self.history.exists())

  def test_prepare_before_first_archive_also_reserves_history(self) -> None:
    cli_history.restore({**self.value, "resume_job": "jfixture"}, prepare=True)
    self.assertTrue(self.history.exists())
    with self.assertRaisesRegex(ValueError, "Resume is prepared"):
      cli_history.archive("jfixture")
    journal = st.read("cli-history-fixture-thread.json")
    journal["resume_prepared_at"] = 0
    st.write("cli-history-fixture-thread.json", journal)
    cli_history.archive("jfixture")
    self.assertFalse(self.history.exists())

  def test_collision_symlink_and_foreign_parent_preserve_files(self) -> None:
    cli_history.archive("jfixture")
    with self.assertRaisesRegex(ValueError, "another parent"):
      cli_history.restore({**self.value, "session_id": "other", "resume_job": "jfixture"})
    self.history.write_text("new-file")
    with self.assertRaisesRegex(ValueError, "Both history"):
      cli_history.restore({**self.value, "resume_job": "jfixture"})
    self.history.unlink()
    self.history.symlink_to(st.root() / "outside")
    with self.assertRaisesRegex(ValueError, "symlink"):
      cli_history.restore({**self.value, "resume_job": "jfixture"})

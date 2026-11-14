"""Regression tests for delegate v0.25.0 hardening, GC, identity separation, and routing enhancements."""
from pathlib import Path
from unittest.mock import patch
import copy
import json
import os
import stat
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import accounting
import budget
import delegate
import gc_store
import lifecycle
import optimizer
import persistent_tasks
import registration
import routing_policy
import runtime_image
import service
import storage as st
import worker


class HardeningV25Tests(unittest.TestCase):
  """Verify v0.25.0 runtime, GC, lifecycle, security, and routing behaviors in isolation."""

  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-v25-")
    self.root = Path(self.temp.name)
    self.state_dir = self.root / "state"
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.state_dir)})
    self.env.start()

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def test_runtime_image_version_and_readonly_permissions(self) -> None:
    self.assertEqual(runtime_image.VERSION, "0.25.0")
    image = runtime_image.freeze(SCRIPTS)
    image_dir = Path(image["path"])
    manifest = json.loads((image_dir / "runtime-image.json").read_text())
    self.assertEqual(manifest["version"], "0.25.0")
    self.assertEqual(stat.S_IMODE(image_dir.stat().st_mode), 0o500)
    self.assertEqual(stat.S_IMODE((image_dir / "runtime-image.json").stat().st_mode), 0o400)
    runtime_image._VERSIONS.pop(image["digest"], None)
    self.assertEqual(runtime_image.job_version({"runtime_image": image}), "0.25.0")

  def test_registration_wal_cleanup_pending_jobs_and_hook_sanitization(self) -> None:
    sid = "sess_v25_wal"
    task = accounting.begin(sid, label="v25 task")
    job1 = {
      "id": "j" + "1" * 16,
      "session_id": sid,
      "task_id": task["id"],
      "status": "queued",
      "created_at": time.time(),
      "provider": {"kind": "codex", "command": "codex"},
      "provider_name": "codex",
      "role": {"workspace": "read-only"},
      "model": "fixture",
      "effort": "low",
      "await_completion": True,
    }
    with st.locked():
      registration.commit_locked(job1)
    self.assertFalse((st.root() / ("dispatch-" + job1["id"] + ".json")).exists())
    self.assertEqual(st.read("dispatch-pending.json", []), [])
    self.assertEqual(st.session(sid)["pending_jobs"], [job1["id"]])

    # Simulate an interrupted registration WAL left in dispatch-pending.json.
    job2 = copy.deepcopy(job1)
    job2["id"] = "j" + "2" * 16
    with st.locked():
      st.write("dispatch-" + job2["id"] + ".json", {"job": job2})
      st.write("dispatch-pending.json", [job2["id"]])
      registration.recover_locked()
    self.assertFalse((st.root() / ("dispatch-" + job2["id"] + ".json")).exists())
    self.assertEqual(st.read("dispatch-pending.json", []), [])
    self.assertEqual(st.job(job2["id"])["status"], "failed")
    self.assertEqual(st.session(sid)["pending_jobs"], [job1["id"], job2["id"]])

    # Terminal + collected_at prunes from pending_jobs during delegate.hook().
    with st.locked():
      j2 = st.job(job2["id"])
      j2["collected_at"] = time.time()
      st.write("job-" + job2["id"] + ".json", j2)
    with patch("delivery.notice", return_value=""), patch("handoff.sync"):
      delegate.hook({"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Read"})
    self.assertEqual(st.session(sid)["pending_jobs"], [job1["id"]])

    # Path-traversal / malformed session_id is ignored safely in hook and statusline.
    self.assertEqual(delegate.hook({"session_id": "../bad", "hook_event_name": "SessionStart"}), {})
    status = delegate.statusline({"session_id": "../bad"})
    self.assertEqual(status, "Claude quota: unknown")

  def test_gc_store_protects_live_and_reclaims_expired_state(self) -> None:
    sid = "sess_gc_v25"
    old_ts = time.time() - 20 * 86400
    old_runtime_ts = time.time() - 2 * 86400

    # Create a closed non-benchmark task containing one expired settled job.
    expired_job = {
      "id": "j" + "a" * 16,
      "session_id": sid,
      "task_id": "t" + "a" * 16,
      "status": "completed",
      "await_completion": True,
      "collected_at": old_ts,
      "finished_at": old_ts,
      "created_at": old_ts,
      "provider": {"kind": "codex", "command": "codex"},
      "model": "fixture",
      "effort": "low",
    }
    open_task_job = {
      **expired_job,
      "id": "j" + "b" * 16,
      "task_id": "t" + "b" * 16,
    }
    uncollected_job = {
      **expired_job,
      "id": "j" + "c" * 16,
      "task_id": "t" + "a" * 16,
      "collected_at": None,
    }
    active_job = {
      **expired_job,
      "id": "j" + "d" * 16,
      "status": "running",
    }

    for rec in (expired_job, open_task_job, uncollected_job, active_job):
      p = st.root() / ("job-" + rec["id"] + ".json")
      st.write(p.name, rec)
      os.utime(p, (old_ts, old_ts))

    st.write("active.json", [active_job["id"]])
    st.save_session(sid, {
      "mode": "off",
      "agents": None,
      "strategy": "balanced",
      "jobs": [expired_job["id"], uncollected_job["id"], active_job["id"]],
      "pending_jobs": [expired_job["id"], uncollected_job["id"], active_job["id"]],
    })
    st.write("task-" + expired_job["task_id"] + ".json", {
      "id": expired_job["task_id"],
      "session_id": sid,
      "status": "closed",
      "case_key": "",
      "jobs": [expired_job["id"], uncollected_job["id"]],
    })
    st.write("task-" + open_task_job["task_id"] + ".json", {
      "id": open_task_job["task_id"],
      "session_id": sid,
      "status": "open",
      "case_key": "",
      "jobs": [open_task_job["id"]],
    })
    st.write("delivery-" + expired_job["id"] + ".json", {"job_id": expired_job["id"], "status": "acknowledged"})
    (st.root() / ("stderr-" + expired_job["id"] + ".txt")).write_text("old stderr")

    orphan_wal = st.root() / "dispatch-jorphan.json"
    orphan_wal.write_text("{}")
    os.utime(orphan_wal, (old_runtime_ts, old_runtime_ts))

    stale_lock = st.lock_path("stale_lock_v25")
    stale_lock.write_text("")
    os.utime(stale_lock, (old_runtime_ts, old_runtime_ts))

    old_runtime = st.root() / "runtimes" / ("0" * 64)
    old_runtime.mkdir(parents=True)
    (old_runtime / "runtime-image.json").write_text("{}")
    os.utime(old_runtime, (old_runtime_ts, old_runtime_ts))

    live_digest = "f" * 64
    live_runtime = st.root() / "runtimes" / live_digest
    live_runtime.mkdir(parents=True)
    (live_runtime / "runtime-image.json").write_text("{}")
    os.utime(live_runtime, (old_runtime_ts, old_runtime_ts))
    j_active = st.job(active_job["id"])
    j_active["runtime_image"] = {"digest": live_digest, "path": str(live_runtime)}
    st.write("job-" + active_job["id"] + ".json", j_active)

    with patch("gc_store._launcher_runtime_digest", return_value=None):
      dry = service.control(sid, action="gc")
      self.assertTrue(dry["dry_run"])
      self.assertEqual(dry["counts"]["jobs"], 1)
      self.assertEqual(dry["counts"]["deliveries"], 1)
      self.assertEqual(dry["counts"]["logs"], 1)
      self.assertEqual(dry["counts"]["wal"], 1)
      self.assertEqual(dry["counts"]["locks"], 1)
      self.assertEqual(dry["counts"]["runtimes"], 1)
      self.assertTrue((st.root() / ("job-" + expired_job["id"] + ".json")).exists())

      applied = gc_store.collect_garbage(apply=True, retention_days=14)

    self.assertFalse(applied["dry_run"])
    self.assertEqual(applied["counts"]["sessions_updated"], 1)
    self.assertEqual(applied["counts"]["tasks_updated"], 1)
    self.assertFalse((st.root() / ("job-" + expired_job["id"] + ".json")).exists())
    self.assertFalse((st.root() / ("delivery-" + expired_job["id"] + ".json")).exists())
    self.assertFalse((st.root() / ("stderr-" + expired_job["id"] + ".txt")).exists())
    self.assertFalse(orphan_wal.exists())
    self.assertFalse(stale_lock.exists())
    self.assertFalse(old_runtime.exists())
    # Protected artifacts remain intact.
    self.assertTrue((st.root() / ("job-" + open_task_job["id"] + ".json")).exists())
    self.assertTrue((st.root() / ("job-" + uncollected_job["id"] + ".json")).exists())
    self.assertTrue((st.root() / ("job-" + active_job["id"] + ".json")).exists())
    self.assertTrue(live_runtime.exists())
    self.assertNotIn(expired_job["id"], st.session(sid)["jobs"])
    self.assertNotIn(expired_job["id"], st.session(sid)["pending_jobs"])
    self.assertNotIn(expired_job["id"], st.read("task-" + expired_job["task_id"] + ".json")["jobs"])

  def test_lifecycle_helper_identity_separation_and_dead_orphan_cancel(self) -> None:
    jid = "j" + "5" * 16
    inf_id = {"pid": 999991, "start": "10", "boot": "boot-a"}
    hlp_id = {"pid": 999992, "start": "20", "boot": "boot-a"}
    st.write("job-" + jid + ".json", {
      "id": jid,
      "session_id": "s_life",
      "status": "orphaned",
      "created_at": time.time() - 60,
      "inference_identity": inf_id,
      "backend_identity": inf_id,
      "backend_starting": False,
    })

    # While inference_identity is alive, mark_helper must not clobber inference_identity/backend_identity.
    with patch("lifecycle.group_running", side_effect=lambda rec: rec == inf_id):
      lifecycle.mark_helper(jid, True, hlp_id)
    updated = st.job(jid)
    self.assertEqual(updated["inference_identity"], inf_id)
    self.assertEqual(updated["backend_identity"], inf_id)
    self.assertEqual(updated["helper_identity"], hlp_id)
    self.assertTrue(updated["helper_starting"])

    # When no groups are running and startup flags are false, cancel_orphan settles cleanly.
    st.update_job(jid, helper_starting=False, backend_starting=False)
    with patch("lifecycle.alive", return_value=False), patch("lifecycle.group_running", return_value=False):
      cancelled = lifecycle.cancel_orphan(jid)
    self.assertEqual(cancelled["status"], "cancelled")
    self.assertTrue(cancelled["cancel_requested"])
    self.assertTrue(cancelled["cancel_requested"] and cancelled.get("finished_at") is not None)

  def test_worker_packet_ancestor_cap_symlinks_and_claude_redaction(self) -> None:
    deep = self.root / "tree"
    cur = deep
    cur.mkdir()
    (cur / "AGENTS.md").write_text("TOP_OUTSIDE_16")
    for idx in range(20):
      cur = cur / f"d{idx}"
      cur.mkdir()
      if idx == 10:
        (cur / "AGENTS.md").write_text("WITHIN_16")
    secret_target = self.root / "secret_agents.md"
    secret_target.write_text("SYMLINK_SECRET")
    (cur / "AGENTS.md").symlink_to(secret_target)

    prompt = worker.packet({
      "source_cwd": str(cur),
      "cwd": str(cur),
      "timeout_seconds": 60,
      "role": {"workspace": "read-only", "instructions": "Inspect."},
      "provider": {"kind": "claude"},
      "task": "Do task",
      "acceptance": "Pass check",
      "context": "",
    })
    self.assertIn("WITHIN_16", prompt)
    self.assertNotIn("TOP_OUTSIDE_16", prompt)
    self.assertNotIn("SYMLINK_SECRET", prompt)

    fake = self.root / "fake-claude-leak"
    fake.write_text(
      "#!/usr/bin/env python3\n"
      "import sys\n"
      "sys.stderr.write('Authorization: Bearer super-secret-tok sk-AbCdEf1234567890\\n')\n"
      "print('not-json sk-SecretJsonToken999')\n"
      "sys.exit(1)\n"
    )
    fake.chmod(0o700)
    jid = "j" + "7" * 16
    job = {
      "id": jid,
      "session_id": "s_redact",
      "status": "running",
      "provider": {"kind": "claude", "command": str(fake)},
      "provider_name": "claude",
      "role": {"workspace": "read-only"},
      "model": "sonnet",
      "effort": "low",
      "cwd": str(self.root),
      "source_cwd": str(self.root),
      "timeout_seconds": 30,
      "cancel_requested": False,
    }
    st.write("job-" + jid + ".json", job)
    with self.assertRaises(RuntimeError) as ctx:
      worker.claude_worker(job, "hello")
    msg = str(ctx.exception)
    self.assertNotIn("super-secret-tok", msg)
    self.assertNotIn("sk-AbCdEf1234567890", msg)
    self.assertNotIn("sk-SecretJsonToken999", msg)
    stderr_text = (st.root() / ("stderr-" + jid + ".txt")).read_text()
    self.assertIn("Bearer [REDACTED]", stderr_text)
    self.assertNotIn("super-secret-tok", stderr_text)
    self.assertNotIn("sk-AbCdEf1234567890", stderr_text)

  def test_persistent_task_wait_bounds_and_timeout(self) -> None:
    sid, lid = "s_wait", "l" + "9" * 16
    st.write("loop-" + lid + ".json", {
      "id": lid,
      "session_id": sid,
      "status": "running",
      "event_seq": 0,
      "controller_identity": {"pid": os.getpid(), "start": "1", "boot": "b"},
      "current_job": None,
    })
    with self.assertRaisesRegex(ValueError, "1\\.\\.600"):
      persistent_tasks.wait({"session_id": sid, "loop_id": lid, "after": 0, "timeout_seconds": 601})

    ticks = iter([0.0, 2.0, 2.0, 2.0])
    with patch("persistent_tasks.alive", return_value=True), \
         patch("persistent_tasks.time.monotonic", side_effect=lambda: next(ticks)):
      result = persistent_tasks.wait({"session_id": sid, "loop_id": lid, "after": 0, "timeout_seconds": 1})
    self.assertEqual(result["status"], "timeout")

  def test_budget_parse_usage_handles_feb_29(self) -> None:
    parsed = budget.parse_usage("Current session: 42% used · resets Feb 29, 10:00am (UTC)")
    self.assertEqual(parsed["status"], "known")
    self.assertEqual(parsed["data"]["five_hour"]["used_percentage"], 42.0)

  def test_optimizer_model_tiers_and_routing_policy_default_provider(self) -> None:
    service.configure("model_tiers", "", {"custom-mini-edge": 2})
    self.assertEqual(optimizer.tier("custom-mini-edge"), 2)

    cfg = st.config()
    cfg["providers"]["p1"] = {"kind": "claude", "enabled": True, "command": "claude",
                              "models": ["sonnet"], "efforts": ["low", "medium", "high"], "is_default": True}
    cfg["providers"]["p2"] = {"kind": "claude", "enabled": True, "command": "claude",
                              "models": ["sonnet"], "efforts": ["low", "medium", "high"], "is_default": False}
    service.validate_config(cfg)

    cfg["providers"]["p2"]["is_default"] = True
    with self.assertRaisesRegex(ValueError, "At most one enabled provider"):
      service.validate_config(cfg)

    chosen, _ = routing_policy.select([
      {
        "action": "delegate",
        "provider": "p2",
        "provider_default": False,
        "capability_tier": 1,
        "preference_rank": 0,
        "expected_processed_tokens": 10000,
        "quota": {"remaining_percent": 80, "surplus_priority": 0.5},
      },
      {
        "action": "delegate",
        "provider": "p1",
        "provider_default": True,
        "capability_tier": 1,
        "preference_rank": 0,
        "expected_processed_tokens": 10000,
        "quota": {"remaining_percent": 80, "surplus_priority": 0.5},
      },
    ], desired_tier=1)
    self.assertEqual(chosen["provider"], "p1")


if __name__ == "__main__":
  unittest.main()

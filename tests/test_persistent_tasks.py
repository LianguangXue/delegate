"""Durable supervision must not replay paid work, bypass limits or poll through a model."""
from pathlib import Path
from unittest.mock import Mock, patch
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"))
import accounting as ac
import delegate
import lifecycle
import persistent_tasks as pt
import registration
import service
import storage as st


class PersistentTests(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory(prefix="delegate-persistent-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.tmp.name})
    self.env.start()
    self.parent = ac.begin("s", label="Parent coordination")
    self.spec = {"agent": "codex-reviewer", "model": "fixture-sol", "effort": "medium", "task": "Bounded step",
      "acceptance": "Provide measured step evidence", "rationale": "Supervised", "cwd": self.tmp.name}
    self.created = []

  def tearDown(self) -> None:
    self.env.stop()
    self.tmp.cleanup()

  def start(self, mode: str = "persistent", **kwargs) -> str:
    with patch("persistent_tasks.ensure_controller"):
      value = pt.manage("s", "start", mode=mode, run_spec=self.spec, one_shot=True, **kwargs)
    return value["id"]

  def actor(self, lid: str) -> None:
    pt.update(lid, controller_identity=lifecycle.identity(os.getpid()))

  def stopped(self, lid: str) -> None:
    pt.update(lid, controller_identity=None)

  def fake_dispatch(self, **args):
    binding = pt.context(args["session_id"])
    jid = "jfixture" + str(len(self.created) + 1)
    job = {"id": jid, "session_id": "s", "task_id": binding["task_id"], "agent": "codex-reviewer", "model": "fixture-sol",
      "effort": "medium", "provider_name": "codex", "provider": {"kind": "codex"}, "role": {"workspace": "read-only"},
      "status": "running", "created_at": time.time(), "timeout_seconds": 0, "review": "pending", "cwd": self.tmp.name,
      "source_cwd": self.tmp.name, "inference_state": "running", "work_item": args["work_item"],
      "loop_id": binding["loop_id"], "loop_cycle": binding["cycle"], "loop_attempt": binding["attempt"],
      "loop_launch_key": binding["key"], "cancel_requested": False, "thread_id": "thread-fixture"}
    st.write("job-"+jid+".json", job)
    ledger = ac.load(binding["task_id"])
    ledger["jobs"].append(jid)
    ac.save(ledger)
    state = st.session("s")
    state["jobs"].append(jid)
    st.save_session("s", state)
    self.created.append((jid, args))
    return service.summary(job)

  def complete(self, jid: str, status: str = "completed", tokens: int = 100) -> None:
    st.update_job(jid, status=status, finished_at=time.time(), result="Measured step result", worker_identity=None,
      usage_delta={"inputTokens": tokens, "outputTokens": 0, "cachedInputTokens": 0})

  def command(self, lid: str, action: str, **kwargs):
    self.stopped(lid)
    with patch("persistent_tasks.ensure_controller"):
      return pt.manage("s", action, lid, **kwargs)

  def test_supervised_step_needs_acceptance_then_advances_same_conversation(self) -> None:
    lid = self.start(max_cycles=2)
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      self.assertTrue(pt.tick(lid))
      first = pt.load(lid)["current_job"]
      self.complete(first)
      self.assertFalse(pt.tick(lid))
    self.assertEqual(pt.load(lid)["status"], "awaiting_review")
    self.assertEqual(st.session("s")["active_task"], self.parent["id"])
    with self.assertRaisesRegex(ValueError, "accepted step"):
      self.command(lid, "advance")
    service.jobs("s", "result", first)
    service.jobs("s", "verify", first, verdict="accepted", evidence="Measured result checked")
    self.command(lid, "advance", run_spec={"task": "Next bounded step"})
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      self.assertTrue(pt.tick(lid))
    self.assertEqual(len(self.created), 2)
    self.assertEqual(self.created[1][1]["resume_job"], first)
    self.assertNotEqual(self.created[0][1]["work_item"], self.created[1][1]["work_item"])
    self.assertEqual(pt.load(lid)["review"], "pending")

  def test_resume_does_not_bypass_parent_gated_advance(self) -> None:
    lid = self.start()
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
      self.complete(pt.load(lid)["current_job"])
      pt.tick(lid)
      self.command(lid, "resume")
      self.actor(lid)
      self.assertFalse(pt.tick(lid))
    self.assertEqual(len(self.created), 1)

  def test_trigger_skips_unchanged_data_and_cycle_cap_never_accepts_goal(self) -> None:
    manifest = Path(self.tmp.name) / "progress.json"
    manifest.write_text('{"step":1}')
    lid = self.start("loop", max_cycles=2, token_budget=1000, trigger_file="progress.json", interval_seconds=30)
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
      self.complete(pt.load(lid)["current_job"])
      self.assertTrue(pt.tick(lid))
      pt.update(lid, next_run_at=0)
      for _ in range(3):
        self.assertTrue(pt.tick(lid))
      self.assertEqual(len(self.created), 1)
      manifest.write_text('{"step":2}')
      pt.tick(lid)
      self.complete(pt.load(lid)["current_job"])
      self.assertFalse(pt.tick(lid))
    self.assertEqual(len(self.created), 2)
    self.assertEqual(pt.load(lid)["status"], "completed")
    self.assertEqual(pt.load(lid)["review"], "pending")

  def test_failed_round_is_not_replayed_by_resume_retry_keeps_work_item(self) -> None:
    lid = self.start("loop", token_budget=1000)
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
      self.complete(pt.load(lid)["current_job"], status="failed")
      self.assertFalse(pt.tick(lid))
      self.command(lid, "recover")
      self.actor(lid)
      self.assertFalse(pt.tick(lid))
      self.assertEqual(len(self.created), 1)
      self.command(lid, "retry")
      self.actor(lid)
      pt.tick(lid)
    self.assertEqual(self.created[0][1]["work_item"], self.created[1][1]["work_item"])
    self.assertEqual(pt.load(lid)["attempt"], 2)

  def test_success_rejected_then_retry_does_not_consume_a_second_logical_cycle(self) -> None:
    lid = self.start(max_cycles=2)
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
      first = pt.load(lid)["current_job"]
      self.complete(first)
      pt.tick(lid)
      service.jobs("s", "result", first)
      service.jobs("s", "verify", first, verdict="needs-work", evidence="A genuine failed check")
      self.command(lid, "retry")
      self.actor(lid)
      pt.tick(lid)
      second = pt.load(lid)["current_job"]
      self.complete(second)
      pt.tick(lid)
    self.assertEqual(pt.load(lid)["completed_cycles"], 1)
    self.assertEqual(pt.load(lid)["cycle"], 1)
    service.jobs("s", "result", second)
    service.jobs("s", "verify", second, verdict="accepted", evidence="Corrected step checked")
    self.command(lid, "advance")
    self.assertEqual(pt.load(lid)["cycle"], 2)

  def test_crash_after_child_registration_adopts_without_another_model_call(self) -> None:
    lid = self.start()
    self.actor(lid)
    def lost_response(**args):
      self.fake_dispatch(**args)
      raise RuntimeError("Controller died after dispatch registration")
    with patch("service.run", side_effect=lost_response):
      with self.assertRaises(RuntimeError):
        pt.tick(lid)
    self.assertIsNone(pt.load(lid)["current_job"])
    with patch("service.run") as dispatch:
      self.assertTrue(pt.tick(lid))
      dispatch.assert_not_called()
    self.assertEqual(pt.load(lid)["current_job"], self.created[0][0])

  def test_crash_adoption_preserves_trigger_and_resolved_role(self) -> None:
    (Path(self.tmp.name) / "progress.json").write_text('{"version":1}')
    lid = self.start("loop", token_budget=1000, trigger_file="progress.json")
    self.actor(lid)
    def lost_response(**args):
      self.fake_dispatch(**args)
      raise RuntimeError("Lost controller response")
    with patch("service.run", side_effect=lost_response), self.assertRaises(RuntimeError):
      pt.tick(lid)
    with patch("service.run") as dispatch:
      pt.tick(lid)
      self.assertEqual(pt.load(lid)["resolved_agent"], "codex-reviewer")
      self.complete(pt.load(lid)["current_job"])
      pt.tick(lid)
      pt.update(lid, next_run_at=0)
      pt.tick(lid)
      dispatch.assert_not_called()
    self.assertEqual(len(self.created), 1)

  def test_cancel_unlaunched_intent_can_finish_without_recovering_and_spending(self) -> None:
    lid = self.start()
    self.actor(lid)
    with patch("service.run", return_value={"action": "main"}):
      self.assertFalse(pt.tick(lid))
    self.assertTrue(pt.load(lid)["pending"])
    pt.update(lid, requested="cancel")
    self.assertFalse(pt.tick(lid))
    self.assertIsNone(pt.load(lid)["pending"])
    self.command(lid, "finish", verdict="blocked", evidence="No child ran; parent cancelled the handoff")
    self.assertEqual(pt.load(lid)["review"], "blocked")

  def test_off_pauses_future_calls_but_does_not_kill_running_child(self) -> None:
    lid = self.start("loop", token_budget=1000)
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
      jid = pt.load(lid)["current_job"]
      service.control("s", "off")
      self.assertTrue(pt.tick(lid))
      self.assertFalse(st.job(jid)["cancel_requested"])
      self.complete(jid)
      self.assertFalse(pt.tick(lid))
    self.assertEqual(pt.load(lid)["status"], "paused")
    self.assertEqual(len(self.created), 1)

  def test_budget_and_unknown_usage_block_new_rounds(self) -> None:
    lid = self.start("loop", max_cycles=5, token_budget=100)
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
      self.complete(pt.load(lid)["current_job"], tokens=100)
      self.assertFalse(pt.tick(lid))
    self.assertEqual(pt.load(lid)["status"], "blocked")
    self.assertEqual(len(self.created), 1)
    self.assertEqual(pt.describe(pt.load(lid))["usage"]["tokens"]["processed_tokens"], 100)

  def test_missing_usage_is_not_assumed_free(self) -> None:
    lid = self.start("loop", token_budget=1000)
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
      st.update_job(pt.load(lid)["current_job"], status="completed", finished_at=time.time())
      self.assertFalse(pt.tick(lid))
    self.assertEqual(pt.load(lid)["status"], "blocked")
    self.assertEqual(pt.usage(pt.load(lid))["unknown_attempts"], 1)

  def test_automatic_loop_does_not_enable_manual_only_roles(self) -> None:
    cfg = st.config()
    cfg["agents"]["codex-reviewer"]["automatic"] = False
    st.write("config.json", cfg)
    with self.assertRaisesRegex(ValueError, "manual-only"):
      self.start("loop", token_budget=1000)

  def test_stop_receipt_schedules_no_model_and_is_not_acceptance(self) -> None:
    (Path(self.tmp.name) / "DONE.json").write_text('{"done":true}')
    lid = self.start("loop", token_budget=100, stop_file="DONE.json")
    self.actor(lid)
    with patch("service.run") as dispatch:
      self.assertFalse(pt.tick(lid))
      dispatch.assert_not_called()
    self.assertEqual(pt.load(lid)["status"], "stopped")
    self.assertEqual(pt.load(lid)["review"], "pending")

  def test_workspace_stop_receipt_is_owned_and_stops_only_future_calls(self) -> None:
    lid = self.start("loop", max_cycles=3, token_budget=1000, stop_file="workspace:DONE.json")
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
      jid = pt.load(lid)["current_job"]
      workspace = Path(self.tmp.name) / "worker"
      workspace.mkdir()
      st.update_job(jid, cwd=str(workspace))
      (workspace / "DONE.json").write_text('{"done":true}')
      self.assertTrue(pt.tick(lid))
      self.complete(jid)
      self.assertFalse(pt.tick(lid))
    self.assertEqual(len(self.created), 1)
    self.assertEqual(pt.load(lid)["status"], "stopped")

  def test_invalid_specs_and_symlink_manifests_do_not_start_controller(self) -> None:
    with patch("persistent_tasks.ensure_controller") as start:
      with self.assertRaisesRegex(ValueError, "token_budget"):
        pt.manage("s", "start", mode="loop", run_spec=self.spec, one_shot=True)
      with self.assertRaisesRegex(ValueError, "commit"):
        pt.manage("s", "start", run_spec={**self.spec, "allow_commit": True}, one_shot=True)
      start.assert_not_called()
    outside = Path(self.tmp.name) / "target.json"
    outside.write_text('{"done":true}')
    (Path(self.tmp.name) / "link.json").symlink_to(outside)
    with self.assertRaises(OSError):
      self.start(stop_file="link.json")

  def test_real_dispatch_is_pinned_to_controller_ledger_and_child_ownership(self) -> None:
    lid = self.start()
    self.actor(lid)
    catalog = {"models": [{"model": "fixture-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]}
    with patch("service.catalog", return_value=catalog), patch("service.quota", return_value={"status": "unknown"}), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      self.assertTrue(pt.tick(lid))
    loop = pt.load(lid)
    job = st.job(loop["current_job"])
    self.assertEqual(job["task_id"], loop["task_id"])
    self.assertEqual(job["loop_id"], lid)
    self.assertEqual(ac.load(self.parent["id"])["jobs"], [])
    self.assertEqual(st.session("s")["active_task"], self.parent["id"])
    self.assertFalse(ac.report(ac.load(self.parent["id"]))["measurement_complete"])

  def test_ownership_and_limits_are_rechecked_at_admission(self) -> None:
    lid = self.start("loop", token_budget=1000)
    self.actor(lid)
    pt.update(lid, pending={"key": "launch"})
    loop = pt.load(lid)
    with pt.launch_binding(loop):
      binding = pt.context("s")
      fake = {"session_id": "s", "task_id": loop["task_id"], "agent": "codex-reviewer"}
      with st.locked():
        pt.validate_launch_locked(binding, fake)
      pt.update(lid, requested="pause")
      with st.locked(), self.assertRaises(ValueError):
        pt.validate_launch_locked(binding, fake)
    with self.assertRaisesRegex(ValueError, "another session"):
      pt.manage("other", "status", lid)

  def test_budget_tightening_is_rechecked_after_slow_model_selection(self) -> None:
    lid = self.start("loop", token_budget=1000)
    self.actor(lid)
    pt.update(lid, pending={"key": "delayed"})
    loop = pt.load(lid)
    with pt.launch_binding(loop):
      self.fake_dispatch(session_id="s", work_item=lid+"-c1")
      self.complete(self.created[0][0], tokens=50)
      with patch("persistent_tasks.ensure_controller"):
        pt.manage("s", "limits", lid, token_budget=20)
      self.assertEqual(ac.load(loop["task_id"])["token_budget"], 20)
      with st.locked(), self.assertRaisesRegex(ValueError, "budget"):
        pt.validate_launch_locked(pt.context("s"), {"session_id": "s", "task_id": loop["task_id"], "agent": "codex-reviewer"})

  def test_registered_partial_wal_is_repaired_and_adopted_once(self) -> None:
    lid = self.start()
    self.actor(lid)
    pt.update(lid, pending={"key": "wal"})
    loop = pt.load(lid)
    job = {"id": "jwal", "session_id": "s", "task_id": loop["task_id"], "loop_id": lid, "loop_launch_key": "wal",
      "loop_cycle": 1, "loop_attempt": 1, "status": "queued", "provider": {"kind": "codex"}}
    st.write("dispatch-jwal.json", {"job": job})
    st.write("dispatch-pending.json", ["jwal"])
    with patch("service.run") as dispatch:
      self.assertFalse(pt.tick(lid))
      dispatch.assert_not_called()
    self.assertEqual(pt.load(lid)["current_job"], "jwal")
    self.assertEqual(st.job("jwal")["inference_state"], "not_started")

  def test_late_superseded_worker_cannot_start_a_new_model_request(self) -> None:
    lid = self.start()
    self.actor(lid)
    pt.update(lid, pending={"key": "first"})
    with pt.launch_binding(pt.load(lid)):
      receipt = self.fake_dispatch(session_id="s", work_item=lid+"-c1")
    pt.check_child_launch(st.job(receipt["id"]))
    pt.update(lid, pending={"key": "second"})
    with self.assertRaises(InterruptedError):
      pt.check_child_launch(st.job(receipt["id"]))

  def test_event_ack_is_monotonic_and_notice_is_not_collection(self) -> None:
    lid = self.start()
    pt.event(lid, "awaiting_review", "A step needs review")
    self.assertIn("no goal", pt.notice(pt.load(lid)))
    self.assertEqual(pt.notice(pt.load(lid)), "")
    pt.update(lid, ack_seq=2)
    pt.update(lid, ack_seq=1)
    self.assertEqual(pt.load(lid)["ack_seq"], 2)
    self.assertEqual(pt.load(lid)["review"], "pending")
    self.assertEqual(pt.returned_loop({"content": [{"text": json.dumps({"id": lid, "status": "ready"})}]})["id"], lid)
    self.assertFalse(pt.watch({"session_id": "s", "tool_name": "mcp__x__delegate_loop",
      "tool_input": {"session_id": "s", "loop_id": lid, "action": "watch"}, "tool_response": {"isError": True}}))

  def test_lost_controller_waiter_requests_recovery_without_inference(self) -> None:
    lid = self.start()
    with patch("service.run") as dispatch:
      notice = pt.wait({"session_id": "s", "loop_id": lid, "after": 0})
      dispatch.assert_not_called()
    self.assertEqual(notice["status"], "controller_lost")

  def test_inflight_token_threshold_notifies_once_without_pretending_hard_cap(self) -> None:
    lid = self.start("loop", token_budget=100)
    self.actor(lid)
    with patch("service.run", side_effect=self.fake_dispatch):
      pt.tick(lid)
    jid = pt.load(lid)["current_job"]
    st.update_job(jid, usage_delta={"inputTokens": 150, "outputTokens": 0, "cachedInputTokens": 0})
    pt.tick(lid)
    seq = pt.load(lid)["event_seq"]
    self.assertIn("not a hard", pt.load(lid)["note"])
    pt.tick(lid)
    self.assertEqual(pt.load(lid)["event_seq"], seq)
    self.assertFalse(st.job(jid)["cancel_requested"])


if __name__ == "__main__":
  unittest.main()

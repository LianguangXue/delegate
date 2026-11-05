"""Task accounting invariants and quota-aware dispatch decisions, without network calls."""
from pathlib import Path
from unittest.mock import patch, Mock, Mock
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"))
import accounting as ac
import optimizer as opt
import service
import storage as st
import delegate
import registration


def message(mid: str, fresh: int, cached: int = 0, output: int = 1) -> dict:
  return {"type": "assistant", "message": {"id": mid, "model": "fixture-model", "usage": {
    "input_tokens": fresh, "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0,
    "output_tokens": output}}}


class MeterTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-meter-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.temp.name})
    self.env.start()
    self.path = Path(self.temp.name) / "transcript.jsonl"
    self.path.write_text("")
    ac.hook({"session_id": "s", "hook_event_name": "UserPromptSubmit", "transcript_path": str(self.path)})

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def append(self, row: dict) -> None:
    with self.path.open("a") as stream:
      stream.write(json.dumps(row) + "\n")

  def collect(self, event: str = "PostToolUse", **extra: object) -> None:
    ac.hook({"session_id": "s", "hook_event_name": event, "transcript_path": str(self.path), **extra})

  def test_stream_fragments_and_repeated_hook_reads_are_not_double_counted(self) -> None:
    task = ac.begin("s")
    self.append(message("m1", 100, 400, 3))
    self.append(message("m1", 100, 400, 9))
    self.collect(); self.collect()
    r = ac.report(ac.load(task["id"]))
    self.assertEqual(r["total"]["processed_tokens"], 509)
    self.assertEqual(r["total"]["uncached_plus_output_tokens"], 109)

  def test_final_response_is_captured_after_finish(self) -> None:
    task = ac.begin("s")
    self.append(message("planning", 10))
    self.collect()
    ac.task_tool("s", "finish", verdict="accepted", evidence="fixture result checked")
    self.append(message("final", 20, output=4))
    self.collect("Stop")
    r = ac.report(ac.load(task["id"]))
    self.assertEqual(r["total"]["processed_tokens"], 35)
    self.assertTrue(r["measurement_complete"])
    self.assertIsNone(st.session("s").get("active_task"))

  def test_native_subagent_usage_is_included_and_pending_blocks_close(self) -> None:
    task = ac.begin("s")
    self.append(message("main", 10))
    self.collect("SubagentStart", agent_id="child")
    self.collect("Stop")
    self.assertEqual(ac.load(task["id"])["status"], "open")
    child = Path(self.temp.name) / "child.jsonl"
    child.write_text(json.dumps(message("native", 30, 40, 5)) + "\n")
    self.collect("SubagentStop", agent_id="child", agent_transcript_path=str(child))
    ac.task_tool("s", "finish", verdict="accepted", evidence="fixture")
    self.collect("Stop")
    r = ac.report(ac.load(task["id"]))
    self.assertEqual(r["total"]["processed_tokens"], 86)
    self.assertEqual(r["parent"]["processed_tokens"], 11)

  def test_codex_delta_excludes_prior_thread_cost_and_cached_subset(self) -> None:
    job = {"provider": {"kind": "codex"}, "usage": {"total": {"totalTokens": 100000}},
           "usage_delta": {"inputTokens": 1000, "cachedInputTokens": 900, "outputTokens": 25}}
    usage = ac.totals([ac.job_usage(job)])
    self.assertEqual(usage["processed_tokens"], 1025)
    self.assertEqual(usage["uncached_plus_output_tokens"], 125)
    job["usage_delta"]["cacheWriteInputTokens"] = 50
    self.assertEqual(ac.job_usage(job)["uncached_input"], 50)
    self.assertEqual(ac.totals([ac.job_usage(job)])["processed_tokens"], 1025)
    job["usage_delta"]["inputTokens"] = -1
    self.assertIsNone(ac.job_usage(job))

  def test_empty_native_transcript_is_unknown_not_zero_cost(self) -> None:
    task = ac.begin("s"); self.append(message("main", 10))
    child = Path(self.temp.name) / "empty-native.jsonl"; child.write_text("")
    self.collect("SubagentStop", agent_id="child", agent_transcript_path=str(child))
    self.collect("Stop")
    result = ac.report(ac.load(task["id"]))
    self.assertFalse(result["measurement_complete"])
    self.assertIn("native subagent usage not observed", result["issues"])

  def test_api_failure_remains_explicit_in_coverage(self) -> None:
    task = ac.begin("s"); self.append(message("main", 10)); self.collect("StopFailure")
    result = ac.report(ac.load(task["id"]))
    self.assertEqual(result["verdict"], "failed")
    self.assertFalse(result["measurement_complete"])

  def test_comparison_rejects_incomplete_and_reports_negative_savings(self) -> None:
    for arm, count in [("baseline", 100), ("optimized", 140)]:
      task = ac.begin("s", case_key="frozen-case", arm=arm)
      self.append(message(arm, count, output=0))
      ac.task_tool("s", "finish", verdict="accepted", evidence="same expected output checked")
      self.collect("Stop")
      self.collect("UserPromptSubmit")
    comparison = ac.compare("frozen-case")
    self.assertEqual(comparison["total_token_reduction_percent"], -40)
    self.assertIsNone(ac.compare("missing-case")["total_token_reduction_percent"])

  def test_hook_never_creates_task_in_off_mode(self) -> None:
    self.collect()
    self.assertIsNone(st.session("s").get("active_task"))

  def test_transcripts_are_not_copied_into_ledger(self) -> None:
    task = ac.begin("s")
    row = message("m", 1); row["message"]["content"] = [{"text": "PRIVATE_CONTENT_DO_NOT_COPY"}]
    self.append(row); self.collect()
    self.assertNotIn("PRIVATE_CONTENT_DO_NOT_COPY", (st.root() / ("task-" + task["id"] + ".json")).read_text())

  def test_read_bound_cannot_claim_complete_and_closed_report_can_catch_up(self) -> None:
    task = ac.begin("s")
    self.append(message("first", 10))
    first_size = self.path.stat().st_size
    self.append(message("second", 1000))
    task = ac.load(task["id"])
    task["offset"], task["parent_complete"] = ac.scan(task, str(self.path), "parent", 0, first_size)
    task.update(status="closed", end_offset=self.path.stat().st_size, verdict="accepted")
    self.assertFalse(ac.report(task)["measurement_complete"])
    self.append(message("next-task", 99999))
    ac.collect(task)
    self.assertTrue(ac.report(task)["measurement_complete"])
    self.assertEqual(ac.report(task)["total"]["processed_tokens"], 1012)

  def test_missing_usage_stays_incomplete_until_same_message_is_complete(self) -> None:
    task = ac.begin("s")
    self.append({"type": "assistant", "message": {"id": "pending", "model": "fixture-model"}})
    self.append(message("other", 10))
    self.collect()
    self.assertTrue(ac.load(task["id"])["unmetered_messages"])
    self.append(message("pending", 30))
    self.collect()
    self.assertFalse(ac.load(task["id"])["unmetered_messages"])

  def test_frozen_tail_truncation_never_becomes_complete(self) -> None:
    task = ac.begin("s")
    self.append(message("first", 10)); first = self.path.stat().st_size
    self.append(message("second", 100)); end = self.path.stat().st_size
    task["offset"], task["parent_complete"] = ac.scan(task, str(self.path), "parent", 0, first)
    task.update(status="closed", end_offset=end)
    with self.path.open("r+b") as stream:
      stream.truncate(first + 5)
    ac.collect(task)
    self.assertFalse(ac.report(task)["measurement_complete"])
    self.assertIn("parent frozen transcript tail was truncated", task["issues"])

  def test_pending_review_failures_are_retained_in_calibration(self) -> None:
    task = ac.begin("s", category="review", complexity="simple")
    self.append(message("main", 10)); self.collect()
    job = {"id": "failure", "agent": "a", "provider_name": "codex", "provider": {"kind": "codex"},
           "model": "m", "effort": "low", "review": "pending", "status": "failed",
           "usage_delta": {"inputTokens": 20, "outputTokens": 2}}
    st.write("job-failure.json", job)
    task = ac.load(task["id"]); task["jobs"] = ["failure"]; ac.save(task)
    self.collect("Stop")
    row = st.read("optimization-samples.json")[task["id"]]["rows"][0]
    self.assertFalse(row["accepted"])
    self.assertTrue(row["quality_known"])
    self.assertEqual(row["tokens"], 22)


class PlannerTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-plan-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.temp.name})
    self.env.start()
    service.control("s", "on", agents=["codex-reviewer"])
    self.task = ac.begin("s", category="review", complexity="simple")
    self.catalog = patch("service.catalog", return_value={"models": [
      {"model": "fixture-luna", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}]},
      {"model": "fixture-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
      {"model": "fixture-astra", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]})
    self.catalog.start()
    self.quota = patch("service.quota", return_value={"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 10}}}})
    self.quota.start()

  def tearDown(self) -> None:
    self.quota.stop(); self.catalog.stop(); self.env.stop(); self.temp.cleanup()

  def test_trivial_work_stays_in_main_without_catalog_or_quota_calls(self) -> None:
    ac.task_tool("s", "update", complexity="trivial")
    self.assertEqual(opt.plan("s")["action"], "main")
    service.catalog.assert_not_called(); service.quota.assert_not_called()

  def test_expensive_handoff_does_not_win_merely_because_quota_is_unknown(self) -> None:
    p = opt.plan("s", estimated_main_tokens=3000)
    self.assertEqual(p["action"], "main")

  def test_known_low_claude_quota_prefers_allowed_external_model(self) -> None:
    st.write("claude-quota-s.json", {"status": "known", "observed_at": time.time(),
      "data": {"five_hour": {"used_percentage": 98}}})
    p = opt.plan("s", estimated_main_tokens=20000)
    self.assertEqual((p["agent"], p["model"], p["effort"]), ("codex-reviewer", "fixture-luna", "low"))

  def test_quota_reset_urgency_is_explicit_not_equivalent_credit_price(self) -> None:
    def sample(hours: float) -> dict:
      return {"status": "known", "data": {"rateLimits": {"primary": {
        "usedPercent": 10, "resetsAt": time.time() + hours * 3600}}}}
    self.assertGreater(opt.pressure(sample(1), "codex")["surplus_priority"], opt.pressure(sample(20), "codex")["surplus_priority"])

  def test_reset_priority_uses_limiting_window_not_unrelated_early_reset(self) -> None:
    sample = {"status": "known", "data": {"rateLimits": {
      "primary": {"usedPercent": 5, "resetsAt": time.time() + 3600},
      "secondary": {"usedPercent": 70, "resetsAt": time.time() + 86400 * 5}}}}
    value = opt.pressure(sample, "codex")
    self.assertEqual(value["remaining_percent"], 30)
    self.assertGreater(value["reset_in_hours"], 119)

  def test_combined_auto_run_chooses_main_without_spawning(self) -> None:
    with patch("service.subprocess.Popen") as spawn:
      result = service.run("s", "auto", "auto", "auto", "tiny task", "exact answer", "save overhead", self.temp.name,
                           complexity="trivial", category="lookup", wait_seconds=50)
      self.assertEqual(result["action"], "main")
      spawn.assert_not_called()

  def test_auto_run_never_enables_an_off_session(self) -> None:
    service.control("s", "off")
    with self.assertRaisesRegex(ValueError, "opt-in"):
      service.run("s", "auto", "auto", "auto", "task", "check", "reason", self.temp.name)
    self.assertEqual(st.session("s")["mode"], "off")

  def test_plan_binding_rejects_different_model_before_spawning(self) -> None:
    service.control("s", "on", strategy="preserve-claude")
    p = opt.plan("s")
    with patch("service.subprocess.Popen") as spawn:
      with self.assertRaisesRegex(ValueError, "does not match"):
        service.run("s", "codex-reviewer", "fixture-sol", "medium", "task", "check", "rationale", self.temp.name,
                    plan_id=p["id"], work_item="main")
      spawn.assert_not_called()

  def test_accepted_work_is_reused_and_failed_attempts_are_bounded(self) -> None:
    job = {"id": "old", "status": "completed", "review": "accepted", "work_item": "main"}
    st.write("job-old.json", job)
    task = ac.load(self.task["id"]); task["jobs"] = ["old"]; ac.save(task)
    self.assertEqual(opt.work_state(task, "main")["action"], "replan")
    job["review"] = "needs-work"; st.write("job-old.json", job)
    task["max_attempts"] = 1
    self.assertEqual(opt.work_state(task, "main")["action"], "replan")

  def test_subtasks_choose_different_models_without_reclassifying_parent(self) -> None:
    service.control("s", "on", strategy="preserve-claude")
    simple = opt.plan("s", work_item="lookup", complexity="simple")
    complex_job = opt.plan("s", work_item="architecture", complexity="complex")
    self.assertEqual((simple["model"], simple["effort"]), ("fixture-luna", "low"))
    self.assertEqual((complex_job["model"], complex_job["effort"]), ("fixture-astra", "high"))
    self.assertEqual(ac.load(self.task["id"])["complexity"], "simple")

  def test_complex_work_rejects_fast_only_and_low_effort_frontier(self) -> None:
    service.catalog.return_value = {"models": [
      {"model": "fixture-luna", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
      {"model": "fixture-astra", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]}
    p = opt.plan("s", complexity="complex")
    self.assertEqual(p["action"], "replan")

  def test_frontier_parent_hands_substantial_simple_work_to_fast_model(self) -> None:
    task = ac.load(self.task["id"])
    task["requests"]["parent"] = {"source": "parent", "model": "claude-opus", "usage": ac.normalize({"input_tokens": 1, "output_tokens": 0})}
    ac.save(task)
    p = opt.plan("s", complexity="simple", estimated_main_tokens=12000)
    self.assertEqual(p["model"], "fixture-luna")
    p = opt.plan("s", complexity="simple", estimated_main_tokens=2000)
    self.assertEqual(p["action"], "main")

  def test_workhorse_parent_does_not_underbid_complex_frontier_requirement(self) -> None:
    task = ac.load(self.task["id"])
    task["requests"]["parent"] = {"source": "parent", "model": "claude-sonnet", "usage": ac.normalize({"input_tokens": 1, "output_tokens": 0})}
    ac.save(task)
    self.assertEqual(opt.plan("s", complexity="complex")["model"], "fixture-astra")

  def test_followup_auto_dispatch_keeps_its_own_classification(self) -> None:
    service.control("s", "on", strategy="preserve-claude")
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      first = service.run("s", "auto", "auto", "auto", "lookup", "check", "reason", self.temp.name,
                          work_item="lookup", complexity="simple", category="lookup")
      st.update_job(first["id"], status="completed", review="accepted", finished_at=time.time())
      second = service.run("s", "auto", "auto", "auto", "design", "check", "reason", self.temp.name,
                           work_item="design", complexity="complex", category="architecture")
    self.assertEqual((second["model"], second["effort"], second["complexity"]),
                     ("fixture-astra", "high", "complex"))
    self.assertEqual(st.job(second["id"])["category"], "architecture")
    self.assertEqual(ac.load(self.task["id"])["complexity"], "simple")

  def test_failed_validation_escalates_model_and_preserves_retry_effort(self) -> None:
    service.control("s", "on", strategy="preserve-claude")
    job = {"id": "old", "agent": "codex-reviewer", "model": "fixture-luna", "effort": "low",
           "status": "completed", "review": "needs-work", "work_item": "main", "provider_name": "codex",
           "provider": {"kind": "codex"}, "complexity": "simple"}
    st.write("job-old.json", job)
    task = ac.load(self.task["id"]); task["jobs"] = ["old"]; ac.save(task)
    p = opt.plan("s")
    self.assertEqual((p["model"], p["effort"]), ("fixture-sol", "medium"))

  def test_first_child_classification_does_not_overwrite_declared_parent(self) -> None:
    ac.task_tool("s", "update", category="parent-design", complexity="complex")
    service.control("s", "on", strategy="preserve-claude")
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      job = service.run("s", "auto", "auto", "auto", "lookup", "check", "reason", self.temp.name,
                        work_item="lookup", complexity="simple", category="lookup")
    self.assertEqual(job["complexity"], "simple")
    root = ac.load(self.task["id"])
    self.assertEqual((root["category"], root["complexity"]), ("parent-design", "complex"))

  def test_frontier_parent_unknown_effort_cannot_underbid_high_effort_worker(self) -> None:
    task = ac.load(self.task["id"])
    task["requests"]["parent"] = {"source": "parent", "model": "claude-opus",
                                  "usage": ac.normalize({"input_tokens": 1, "output_tokens": 0})}
    ac.save(task)
    p = opt.plan("s", complexity="complex")
    self.assertEqual((p["action"], p["model"], p["effort"]), ("delegate", "fixture-astra", "high"))

  def test_known_model_limit_does_not_select_that_model(self) -> None:
    service.control("s", "on", strategy="preserve-claude")
    service.quota.return_value = {"status": "limited", "model": "fixture-luna"}
    self.assertNotEqual(opt.plan("s")["model"], "fixture-luna")

  def test_equal_cost_models_share_quota_and_keep_catalog_preference(self) -> None:
    service.control("s", "on", strategy="preserve-claude")
    service.catalog.return_value = {"models": [
      {"model": name, "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}
      for name in ("latest-luna", "older-luna")]}
    count = 0
    def advancing_clock_pressure(*args):
      nonlocal count
      count += 1
      return {"remaining_percent": 90, "surplus_priority": count * .01}
    with patch("optimizer.pressure", side_effect=advancing_clock_pressure):
      p = opt.plan("s")
    self.assertEqual(p["model"], "latest-luna")
    self.assertEqual(p["candidates"][0]["quota"], p["candidates"][1]["quota"])

  def test_empirical_samples_follow_child_classification(self) -> None:
    history = {"t": {"category": "parent", "complexity": "complex", "parent_share": 100,
      "rows": [{"category": "lookup", "complexity": "simple", "agent": "a", "model": "m",
                "effort": "low", "tokens": 123, "accepted": True}]}}
    o = opt.observations({"category": "lookup", "complexity": "simple"}, "a", "m", "low", history)
    self.assertEqual(o["attempts"], 1)
    self.assertEqual(opt.observations({"category": "parent", "complexity": "complex"}, "a", "m", "low", history)["attempts"], 0)

  def test_empirical_cost_includes_failed_attempts(self) -> None:
    history = {"t": {"category": "review", "complexity": "simple", "parent_share": 2000, "rows": [
      {"agent": "a", "model": "m", "effort": "low", "tokens": 1000, "accepted": True},
      {"agent": "a", "model": "m", "effort": "low", "tokens": 3000, "accepted": False}]}}
    o = opt.observations(self.task, "a", "m", "low", history)
    self.assertEqual(o["mean_attempt_tokens"], 2000)
    self.assertEqual(o["smoothed_success_rate"], .5)

  def test_dispatch_refreshes_parent_usage_before_soft_budget_check(self) -> None:
    path = Path(self.temp.name) / "parent.jsonl"
    path.write_text(json.dumps(message("m", 1000)) + "\n")
    task = ac.load(self.task["id"]); task.update(transcript_path=str(path), token_budget=500); ac.save(task)
    job = {"id": "new", "work_item": "main"}
    with st.locked():
      with self.assertRaisesRegex(ValueError, "budget exhausted"):
        opt.validate_locked(task, job)

  def test_interrupted_registration_recovers_without_dangling_refs(self) -> None:
    job = {"id": "new", "session_id": "s", "task_id": self.task["id"], "provider": {"kind": "codex"},
           "status": "queued", "created_at": time.time(), "plan_id": None}
    real_write = st.write
    def fail_task(name: str, value: object) -> None:
      if name == "task-" + self.task["id"] + ".json":
        raise OSError("fixture interrupted write")
      real_write(name, value)
    with st.locked(), patch("storage.write", side_effect=fail_task):
      with self.assertRaises(OSError):
        registration.commit_locked(job)
    self.assertEqual(st.session("s")["jobs"], ["new"])
    self.assertEqual(st.job("new")["status"], "queued")
    with st.locked():
      registration.recover_locked()
    self.assertEqual(ac.load(self.task["id"])["jobs"], ["new"])
    self.assertEqual(st.job("new")["status"], "failed")
    self.assertEqual(st.read("dispatch-pending.json"), [])


if __name__ == "__main__":
  unittest.main()

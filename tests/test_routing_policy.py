"""Per-subtask reasoning signals select fast/workhorse/frontier lanes without another model call."""
from pathlib import Path
from unittest.mock import Mock, patch
import copy
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"))
import accounting as ac
import delegate
import optimizer as opt
import routing_policy as policy
import service
import storage as st

FAST = {"kind": "mechanical", "scope": "bounded", "uncertainty": "low", "risk": "low", "validation": "inspection"}
DAILY = {"kind": "implementation", "scope": "bounded", "uncertainty": "medium", "risk": "medium", "validation": "tests"}
HARD = {"kind": "architecture", "scope": "system", "uncertainty": "high", "risk": "high", "validation": "inspection"}


class RoutingPolicyTests(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory(prefix="delegate-routing-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.tmp.name})
    self.env.start()
    self.cfg = copy.deepcopy(st.DEFAULT_CONFIG)
    self.cfg["providers"]["claude"]["enabled"] = False
    self.cfg["providers"]["company"] = {"kind": "anthropic", "enabled": True,
      "models": ["deepseek-flash", "deepseek-v4-pro"], "efforts": ["low", "medium", "high", "max"], "command": "fixture"}
    self.cfg["providers"]["official"] = copy.deepcopy(self.cfg["providers"]["company"])
    for name, provider, automatic in (("company", "company", True), ("official", "official", False)):
      for role, workspace in (("reviewer", "read-only"), ("builder", "worktree")):
        self.cfg["agents"][name+"-"+role] = {"provider": provider, "enabled": True,
          "automatic": automatic, "workspace": workspace, "instructions": "Fixture"}
    st.write("config.json", self.cfg)
    service.control("s", "on", strategy="preserve-claude")
    self.task = ac.begin("s", category="project", complexity="complex")
    self.codex = {"models": [{"model": m, "supportedReasoningEfforts": [{"reasoningEffort": e}
      for e in ("low", "medium", "high", "xhigh", "max", "ultra")]} for m in (
        "gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra", "gpt-6-luna")]}
    def catalog(provider: str, **kwargs):
      if provider == "official":
        raise AssertionError("Manual-only account must not be discovered")
      return self.codex if provider == "codex" else self.cfg["providers"][provider]
    self.discovery = patch("service.catalog", side_effect=catalog)
    self.quota = patch("service.quota", return_value={"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 10}}}})
    self.discovery.start()
    self.quota.start()

  def tearDown(self) -> None:
    self.quota.stop()
    self.discovery.stop()
    self.env.stop()
    self.tmp.cleanup()

  def test_mixed_subtasks_choose_fast_daily_hard_without_relabeling_parent(self) -> None:
    fast = opt.plan("s", "run-known-check", operation="write", task_profile={**FAST, "validation": "tests"})
    daily = opt.plan("s", "implementation", operation="write", task_profile=DAILY)
    hard = opt.plan("s", "design", task_profile=HARD)
    self.assertEqual((fast["model"], fast["effort"], fast["complexity"]), ("gpt-6.1-terra", "low", "simple"))
    self.assertEqual((daily["model"], daily["effort"], daily["complexity"]), ("gpt-6.1-sol", "medium", "standard"))
    self.assertEqual((hard["model"], hard["effort"], hard["complexity"]), ("gpt-6-astra", "high", "complex"))
    self.assertEqual(ac.load(self.task["id"])["complexity"], "complex")

  def test_file_only_fast_task_can_use_deepseek_without_probing_manual_account(self) -> None:
    result = opt.plan("s", task_profile={**FAST, "kind": "documentation"})
    self.assertEqual((result["provider"], result["model"], result["effort"]), ("company", "deepseek-flash", "low"))
    self.assertFalse(result["assessment"]["requires_shell"])

  def test_preferred_model_is_not_lost_after_first_four_catalog_rows(self) -> None:
    rows = self.codex["models"]
    terra = next(r for r in rows if "terra" in r["model"])
    self.codex["models"] = [{**terra, "model": f"fixture-luna-{i}"} for i in range(5)] + [terra]
    result = opt.plan("s", task_profile={**FAST, "validation": "tests"})
    self.assertEqual(result["model"], "gpt-6.1-terra")

  def test_retry_inherits_profile_and_tool_requirement_without_new_analysis_call(self) -> None:
    args = dict(session_id="s", agent="auto", model="auto", effort="auto", task="Execute known check",
      acceptance="Exact output", rationale="Bounded", cwd=self.tmp.name, work_item="check", operation="write")
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      first = service.run(**args, task_profile={**FAST, "validation": "tests"})
    st.update_job(first["id"], status="failed", inference_state="not_started", finished_at=time.time())
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      second = service.run(**args)
    self.assertEqual((second["provider_name"], second["model"], second["effort"]), ("codex", "gpt-6.1-terra", "low"))
    self.assertTrue(second["task_assessment"]["requires_shell"])
    st.update_job(second["id"], status="failed", inference_state="not_started", finished_at=time.time())
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      third = service.run(**{**args, "agent": "codex-builder"})
    self.assertTrue(third["task_assessment"]["requires_shell"])

  def test_changed_profile_cannot_reuse_an_accepted_artifact_without_revalidation(self) -> None:
    job = {"id": "old", "task_profile": policy.normalize(FAST), "complexity": "simple", "work_item": "main",
      "model": "gpt-6.1-terra", "effort": "low", "provider": {"kind": "codex"}, "provider_name": "codex",
      "agent": "codex-reviewer", "status": "completed", "review": "accepted", "inference_state": "attempted"}
    st.write("job-old.json", job)
    self.task["jobs"] = ["old"]
    ac.save(self.task)
    with patch("optimizer.work_state", return_value={"action": "reuse", "job_id": "old"}):
      result = opt.plan("s", task_profile=HARD)
    self.assertEqual(result["action"], "replan")
    self.assertIn("assessment changed", result["reason"])

  def test_low_caller_label_cannot_weaken_high_risk_profile(self) -> None:
    result = opt.plan("s", complexity="simple", task_profile=HARD)
    self.assertEqual((result["complexity"], result["model"]), ("complex", "gpt-6-astra"))
    self.assertTrue(any("Raised" in reason for reason in result["assessment"]["signals"]))

  def test_fixed_role_auto_uses_same_assessment_and_preserves_explicit_model_effort(self) -> None:
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      result = service.run("s", "codex-builder", "auto", "auto", "Run a known checker", "Exact output", "Bounded",
        self.tmp.name, work_item="fast", task_profile={**FAST, "validation": "tests"})
    self.assertEqual((result["model"], result["effort"]), ("gpt-6.1-terra", "low"))
    self.assertEqual(result["task_assessment"]["complexity"], "simple")
    st.update_job(result["id"], status="completed", finished_at=time.time())
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      explicit = service.run("s", "codex-reviewer", "gpt-6.1-terra", "max", "Inspect", "Evidence", "User choice",
        self.tmp.name, work_item="explicit", task_profile=HARD)
    self.assertEqual((explicit["model"], explicit["effort"]), ("gpt-6.1-terra", "max"))

  def test_shell_requirement_rejects_fixed_file_tool_role_before_discovery(self) -> None:
    with patch("service.catalog") as discovery:
      with self.assertRaisesRegex(ValueError, "file tools only"):
        service.run("s", "company-builder", "auto", "auto", "Run tests", "Output", "Why", self.tmp.name, task_profile=DAILY)
      discovery.assert_not_called()

  def test_auto_never_chooses_max_ultra_hidden_or_unsupported_effort(self) -> None:
    self.codex["models"] = [{"model": "gpt-6-astra", "supportedReasoningEfforts": [{"reasoningEffort": "ultra"}]}]
    service.control("s", "on", agents=["codex-reviewer"], strategy="preserve-claude")
    self.assertEqual(opt.plan("s", task_profile=HARD)["action"], "replan")
    self.codex["models"][0].update(hidden=True, supportedReasoningEfforts=[{"reasoningEffort": "high"}])
    self.assertEqual(opt.plan("s", task_profile=HARD)["action"], "replan")

  def test_quota_exhaustion_does_not_route_shell_work_to_file_tools(self) -> None:
    with patch("service.quota", return_value={"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 100}}}}):
      result = opt.plan("s", operation="write", task_profile=DAILY)
    self.assertEqual(result["action"], "replan")
    self.assertTrue(result["assessment"]["requires_shell"])

  def test_trivial_clear_profile_stays_main_without_model_or_quota_discovery(self) -> None:
    with patch("service.catalog") as discovery, patch("service.quota") as quota:
      result = opt.plan("s", complexity="trivial", task_profile={**FAST, "kind": "lookup"})
    self.assertEqual(result["action"], "main")
    discovery.assert_not_called()
    quota.assert_not_called()

  def test_profile_bound_plan_inherits_profile_but_rejects_changed_factors(self) -> None:
    plan = opt.plan("s", work_item="planned", operation="write", task_profile=DAILY)
    args = dict(session_id="s", agent=plan["agent"], model=plan["model"], effort=plan["effort"], task="Implement",
      acceptance="Tests", rationale="Planned", cwd=self.tmp.name, work_item="planned", plan_id=plan["id"])
    with self.assertRaisesRegex(ValueError, "differs from its plan"):
      service.run(**args, task_profile=HARD)
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      result = service.run(**args)
    self.assertEqual(result["task_assessment"], plan["assessment"])

  def test_new_plan_profile_takes_precedence_over_failed_attempt_profile(self) -> None:
    old = {"id": "old", "task_profile": policy.normalize(FAST), "complexity": "simple", "work_item": "planned",
      "model": "gpt-6.1-terra", "effort": "low", "provider": {"kind": "codex"}, "provider_name": "codex",
      "agent": "codex-reviewer", "status": "failed", "review": "pending", "inference_state": "not_started"}
    st.write("job-old.json", old)
    self.task["jobs"] = ["old"]
    ac.save(self.task)
    plan = opt.plan("s", work_item="planned", task_profile=HARD)
    with patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      result = service.run("s", plan["agent"], plan["model"], plan["effort"], "Review architecture", "Evidence", "New plan",
        self.tmp.name, work_item="planned", plan_id=plan["id"])
    self.assertEqual(result["task_assessment"], plan["assessment"])

  def test_transport_error_keeps_effort_but_failed_acceptance_escalates(self) -> None:
    role = self.cfg["agents"]["codex-reviewer"]
    previous = {"model": "gpt-6-astra", "effort": "high", "review": "pending"}
    self.assertEqual(opt.role_pair(role, {"kind": "codex"}, self.codex, "auto", "auto", "complex", "codex", previous)[1], "high")
    previous["review"] = "needs-work"
    self.assertEqual(opt.role_pair(role, {"kind": "codex"}, self.codex, "auto", "auto", "complex", "codex", previous)[1], "xhigh")

  def test_invalid_profile_and_schema_cannot_hide_missing_risk_factors(self) -> None:
    for profile in ({"kind": "lookup"}, {**FAST, "risk": "unknown"}, {**FAST, "requires_shell": "yes"}, {**FAST, "model": "astra"}):
      with self.assertRaises(ValueError):
        policy.normalize(profile)
    tools = {t["name"]: t for t in delegate.tool_list()}
    for name in ("delegate_run", "delegate_plan"):
      self.assertIn("task_profile", tools[name]["inputSchema"]["properties"])


if __name__ == "__main__":
  unittest.main()

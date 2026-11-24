"""Quota efficiency, routing precision, loop-breaking, and payload reduction tests."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import accounting as ac
import attempts as at
import delegate
import handoff
import optimizer as opt
import result_delivery
import routing_policy as policy
import service
import storage as st
import worker
import workspaces


class QuotaEfficiencyTests(unittest.TestCase):
  """Verify quota optimization, preflight guards, routing precision, and context reductions."""

  def setUp(self) -> None:
    """Create isolated delegate state and test catalogs."""
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-quota-eff-")
    self.root = Path(self.temp.name)
    self.state_dir = self.root / "state"
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.state_dir)})
    self.env.start()
    self.cfg = copy.deepcopy(st.DEFAULT_CONFIG)
    self.cfg["providers"]["codex"] = {"kind": "codex", "enabled": True, "command": "codex"}
    self.cfg["providers"]["claude"]["enabled"] = False
    self.cfg["providers"]["company"] = {
      "kind": "anthropic",
      "enabled": True,
      "command": "claude",
      "base_url": "https://example.invalid/anthropic",
      "api_key_env": "TEST_COMPANY_KEY",
      "models": ["deepseek-flash", "deepseek-v4-pro"],
      "efforts": ["low", "medium", "high", "max"],
    }
    self.cfg["agents"]["codex-builder"] = {
      "provider": "codex",
      "enabled": True,
      "automatic": True,
      "workspace": "worktree",
      "instructions": "Implement requested changes.",
    }
    self.cfg["agents"]["company-reviewer"] = {
      "provider": "company",
      "enabled": True,
      "automatic": True,
      "workspace": "read-only",
      "instructions": "Review requested files.",
    }
    self.cfg["agents"]["company-builder"] = {
      "provider": "company",
      "enabled": True,
      "automatic": True,
      "workspace": "worktree",
      "instructions": "Implement changes.",
    }
    st.write("config.json", self.cfg)
    service.control("s1", "on", agents=[
      "codex-reviewer", "codex-builder", "company-reviewer", "company-builder",
    ], strategy="preserve-claude")
    self.task = ac.begin("s1", category="implementation", complexity="standard")
    self.codex_models: dict[str, Any] = {"models": [
      {"model": "gpt-6.1-terra", "description": "Fast clear execution.",
       "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}]},
      {"model": "gpt-5.0-sol", "description": "Legacy previous generation coding model.",
       "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
      {"model": "gpt-6.1-sol", "description": "Daily software engineering workhorse.",
       "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
      {"model": "gpt-6.1-astra", "description": "Frontier deep reasoning model.",
       "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
    ]}
    self.company_models: dict[str, Any] = {
      "models": ["deepseek-flash", "deepseek-v4-pro"],
      "efforts": ["low", "medium", "high", "max"],
    }

  def tearDown(self) -> None:
    """Restore environment and remove temporary files."""
    self.env.stop()
    self.temp.cleanup()

  def _init_repo(self) -> Path:
    """Create a minimal committed Git repo under ``self.root``."""
    repo = self.root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    worker.git(str(repo), "add", "README.md")
    worker.git(str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
               "commit", "-qm", "init")
    return repo

  def _catalog(self, name: str, refresh: bool = False) -> dict[str, Any]:
    """Return deterministic fixture catalogs per provider."""
    if name == "codex":
      return self.codex_models
    return self.company_models

  def _quota(self, name: str, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
    """Return healthy quota for Codex and API-key status for Anthropic bridges."""
    if name == "codex":
      return {"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 10}}}}
    return {"status": "api_key_only", "data": {"is_available": True}}

  def test_preflight_blocks_dirty_repo_and_untracked_required_files_before_spawn(self) -> None:
    """Reject dirty write repos and missing-HEAD ``required_files`` before spawning a worker."""
    repo = self._init_repo()
    (repo / "draft.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "other.txt").write_text("y = 2\n", encoding="utf-8")
    with patch("service.catalog", side_effect=self._catalog), \
         patch("service.quota", side_effect=self._quota), \
         patch("service.subprocess.Popen") as spawn:
      with self.assertRaisesRegex(ValueError, "clean source repository"):
        service.run(
          "s1", "codex-builder", "gpt-6.1-sol", "medium", "Edit", "Done", "Need fix",
          str(repo), operation="write", permissions="workspace-shell",
        )
      spawn.assert_not_called()

      with self.assertRaisesRegex(ValueError, "not in HEAD|source_inputs"):
        workspaces.preflight_source(
          str(repo), source_inputs_list=["other.txt"], required_files=["draft.py"], snapshot_required_files=False,
        )

      preflight = workspaces.preflight_source(
        str(repo), source_inputs_list=["draft.py"], required_files=["draft.py"], snapshot_required_files=True,
      )
      self.assertTrue(preflight["git_repo"])

  def test_zero_token_capacity_and_infra_failures_do_not_consume_attempts(self) -> None:
    """Classify 0-token provider capacity/infra failures as ``not_started`` and compute ``recovery_advice``."""
    job = {
      "id": "jcap1", "session_id": "s1", "agent": "company-reviewer",
      "provider_name": "company", "provider": self.cfg["providers"]["company"],
      "model": "deepseek-flash", "effort": "low", "cwd": str(self.root), "source_cwd": str(self.root),
      "task": "Review", "acceptance": "Report", "context": "", "timeout_seconds": 60,
      "role": self.cfg["agents"]["company-reviewer"], "permissions": "read-only",
      "background": True, "await_completion": True,
    }
    st.write("job-jcap1.json", job)
    proc = Mock(returncode=1)
    proc.pid = os.getpid()
    proc.poll.return_value = 1

    with patch.dict(os.environ, {"TEST_COMPANY_KEY": "secret-val"}), \
         patch("worker.subprocess.Popen", return_value=proc), \
         patch("worker.identity", return_value={"pid": 1, "start_time": 1}), \
         patch("worker.stop_owned"):
      original_temp = tempfile.TemporaryFile

      def fake_temp(*args: Any, **kwargs: Any) -> Any:
        f = original_temp(*args, **kwargs)
        if kwargs.get("mode") == "w+" and not getattr(fake_temp, "_done", False):
          fake_temp._done = True
          f.write(json.dumps({
            "is_error": True,
            "api_error": "usage_limit_reached",
            "result": "quota exceeded",
            "usage": {"input_tokens": 0, "output_tokens": 0},
          }))
          f.seek(0)
        return f

      with patch("worker.tempfile.TemporaryFile", side_effect=fake_temp):
        outcome = worker.claude_worker(job, "prompt")
    self.assertEqual(outcome["status"], "failed")
    self.assertEqual(outcome["inference_state"], "not_started")
    self.assertEqual(outcome["failure_kind"], "capacity")
    self.assertFalse(at.model_attempted(outcome))

    st.update_job("jcap1", **outcome)
    advice = at.recovery_advice(st.job("jcap1"))
    self.assertIsNotNone(advice)
    assert advice is not None
    self.assertEqual(advice["action"], "switch_or_wait")
    self.assertEqual(service.summary(st.job("jcap1"))["recovery_advice"]["action"], "switch_or_wait")
    self.assertEqual(result_delivery.receipt(st.job("jcap1"))["recovery_advice"]["action"], "switch_or_wait")

    codex_cap_job = {
      "id": "jcodexcap1", "session_id": "s1", "agent": "codex-reviewer",
      "provider_name": "codex", "provider": {"kind": "codex"},
      "model": "gpt-6.1-sol", "effort": "medium", "status": "failed",
      "turn_id": "turn-0tok", "inference_state": "not_started", "failure_kind": "capacity",
      "usage_delta": {"inputTokens": 0, "outputTokens": 0}, "review": "pending",
    }
    self.assertFalse(at.model_attempted(codex_cap_job))
    self.assertEqual(ac.totals([ac.job_usage(codex_cap_job)])["processed_tokens"], 0)
    self.assertEqual(at.recovery_advice(codex_cap_job)["action"], "switch_or_wait")

    infra_job = {"id": "jinfra1", "session_id": "s1", "status": "failed",
                 "inference_state": "not_started", "failure_kind": "infrastructure", "review": "pending"}
    self.assertEqual(at.recovery_advice(infra_job)["action"], "fix_contract")

    timed_job = {"id": "jto1", "session_id": "s1", "status": "timed_out", "thread_id": "thr-1", "review": "pending"}
    self.assertEqual(at.recovery_advice(timed_job)["action"], "resume_or_split")
    review_job = {"id": "jrev1", "session_id": "s1", "status": "completed", "thread_id": "thr-2",
                  "review": "needs-work"}
    self.assertEqual(at.recovery_advice(review_job)["action"], "resume")

  def test_work_state_loop_breakers_halt_identical_errors_and_scratch_timeouts(self) -> None:
    """Return ``replan`` after two identical failures or repeated unresumed timeouts."""
    t_err = ac.begin("s1", category="implementation", complexity="standard")
    st.write("job-e1.json", {"id": "e1", "work_item": "w-err", "status": "failed",
                             "error": "ModuleNotFoundError: missing dep", "turn_id": "t1"})
    st.write("job-e2.json", {"id": "e2", "work_item": "w-err", "status": "failed",
                             "error": "ModuleNotFoundError: missing dep", "turn_id": "t2"})
    t_err["jobs"] = ["e1", "e2"]
    ac.save(t_err)
    decision = opt.work_state(t_err, "w-err")
    self.assertIsNotNone(decision)
    assert decision is not None
    self.assertEqual(decision["action"], "replan")
    self.assertIn("Repeated identical failure", decision["reason"])

    t_dict = ac.begin("s1", category="implementation", complexity="standard")
    codex_err = {"codexErrorInfo": "invalidRequest", "message": "bad param"}
    st.write("job-d1.json", {"id": "d1", "work_item": "w-dict", "status": "failed",
                             "error": dict(codex_err), "turn_id": "t1"})
    st.write("job-d2.json", {"id": "d2", "work_item": "w-dict", "status": "failed",
                             "error": dict(codex_err), "turn_id": "t2"})
    t_dict["jobs"] = ["d1", "d2"]
    ac.save(t_dict)
    dict_decision = opt.work_state(t_dict, "w-dict")
    self.assertIsNotNone(dict_decision)
    assert dict_decision is not None
    self.assertEqual(dict_decision["action"], "replan")
    self.assertIn("Repeated identical failure", dict_decision["reason"])

    t_to = ac.begin("s1", category="implementation", complexity="standard")
    st.write("job-to1.json", {"id": "to1", "work_item": "w-to", "status": "timed_out",
                              "timeout_seconds": 120, "turn_id": "t1"})
    st.write("job-to2.json", {"id": "to2", "work_item": "w-to", "status": "timed_out",
                              "timeout_seconds": 120, "turn_id": "t2"})
    t_to["jobs"] = ["to1", "to2"]
    ac.save(t_to)
    to_decision = opt.work_state(t_to, "w-to")
    self.assertIsNotNone(to_decision)
    assert to_decision is not None
    self.assertEqual(to_decision["action"], "replan")
    self.assertIn("Repeated scratch timeout", to_decision["reason"])

  def test_routing_precision_avoids_over_and_under_provisioning(self) -> None:
    """Select fast lane for bounded low-risk reads, Sol for standard/high-reasoning, Astra only when needed."""
    clear_review = policy.assess({
      "kind": "review", "scope": "bounded", "uncertainty": "low", "risk": "low", "validation": "inspection",
    })
    self.assertEqual((clear_review["complexity"], clear_review["recommended_effort"]), ("simple", "low"))

    low_risk_lookup = policy.assess({
      "kind": "lookup", "scope": "bounded", "uncertainty": "high", "risk": "low", "validation": "inspection",
    })
    self.assertEqual((low_risk_lookup["complexity"], low_risk_lookup["recommended_effort"]), ("standard", "medium"))

    deep_sol = policy.assess({
      "kind": "implementation", "scope": "bounded", "uncertainty": "medium", "risk": "medium",
      "validation": "tests", "reasoning": "high",
    })
    self.assertEqual((deep_sol["complexity"], deep_sol["recommended_effort"]), ("standard", "high"))

    cross_debug = policy.assess({
      "kind": "debugging", "scope": "cross-module", "uncertainty": "medium", "risk": "medium",
      "validation": "tests",
    })
    self.assertEqual((cross_debug["complexity"], cross_debug["recommended_effort"]), ("standard", "high"))

    inherited = policy.assess(None, fallback="complex")
    self.assertEqual(inherited["complexity"], "complex")
    self.assertTrue(any("task_profile" in s for s in inherited["signals"]))

    with patch("service.catalog", side_effect=self._catalog), \
         patch("service.quota", side_effect=self._quota):
      fast_plan = opt.plan("s1", work_item="ro-check", operation="read", task_profile={
        "kind": "research", "scope": "bounded", "uncertainty": "low", "risk": "low", "validation": "inspection",
      }, permissions="read-only", estimated_main_tokens=15000)
      self.assertEqual((fast_plan["complexity"], fast_plan["model"], fast_plan["effort"]),
                       ("simple", "deepseek-flash", "low"))

      std_plan = opt.plan("s1", work_item="std-impl", operation="write", task_profile={
        "kind": "implementation", "scope": "bounded", "uncertainty": "medium", "risk": "medium",
        "validation": "tests",
      }, permissions="workspace-shell", cwd=str(self.root), estimated_main_tokens=12000)
      self.assertEqual((std_plan["model"], std_plan["effort"]), ("gpt-6.1-sol", "medium"))

      high_sol = opt.plan("s1", work_item="deep-std", operation="write", task_profile={
        "kind": "implementation", "scope": "bounded", "uncertainty": "medium", "risk": "medium",
        "validation": "replay", "reasoning": "high",
      }, permissions="workspace-shell", cwd=str(self.root), estimated_main_tokens=12000)
      self.assertEqual((high_sol["model"], high_sol["effort"]), ("gpt-6.1-sol", "high"))

  def test_hook_dedup_schema_reduction_compact_jobs_and_handoff_fields(self) -> None:
    """Deduplicate repeat prompt hooks, slim tools/list and job payloads, and propagate handoff metadata."""
    h1 = delegate.hook({"session_id": "s1", "hook_event_name": "UserPromptSubmit"})
    ctx1 = h1["hookSpecificOutput"]["additionalContext"]
    self.assertIn("Before routing, inspect referenced briefs", ctx1)

    h2 = delegate.hook({"session_id": "s1", "hook_event_name": "UserPromptSubmit"})
    ctx2 = h2["hookSpecificOutput"]["additionalContext"]
    self.assertLess(len(ctx2), len(ctx1))
    self.assertLess(len(ctx2), 260)
    self.assertNotIn("Before routing", ctx2)

    tools_bytes = len(json.dumps(delegate.tool_list(), ensure_ascii=False).encode("utf-8"))
    self.assertLess(tools_bytes, 24000)

    job = {
      "id": "jrun1", "session_id": "s1", "task_id": self.task["id"], "work_item": "w1",
      "created_at": time.time(),
      "agent": "codex-builder", "provider_name": "codex", "provider": {"kind": "codex"},
      "model": "gpt-6.1-sol", "effort": "medium", "permissions": "workspace-shell",
      "status": "running", "review": "pending", "result": "", "error": None,
      "rationale": "x" * 600, "acceptance": "y" * 600,
      "task_assessment": {"complexity": "standard"}, "task_profile": {"kind": "implementation"},
      "execution_environment": {"profile": "sandboxed", "requirements": {}},
      "workspace_setup": {"source_root": "/src"},
      "usage": {"total": {"inputTokens": 1200, "outputTokens": 300}},
      "usage_delta": {"inputTokens": 1200, "outputTokens": 300},
      "pid": os.getpid(), "started_at": time.time(), "timeout_seconds": 300,
    }
    st.write("job-jrun1.json", job)
    sess = st.session("s1")
    sess["jobs"] = ["jrun1"]
    st.save_session("s1", sess)

    compact_list = service.jobs("s1", action="list")
    full_list = service.jobs("s1", action="list", detail=True)
    self.assertLess(len(json.dumps(compact_list)), len(json.dumps(full_list)))
    self.assertEqual(compact_list[0]["permissions"], "workspace-shell")

    with patch("service.reconcile", return_value=st.job("jrun1")):
      res_wait = service.jobs("s1", action="result", job_id="jrun1", wait_seconds=1)
    self.assertIn("anti_polling_notice", res_wait)

    done_job = {**job, "status": "completed", "result": "Changed app.py line 10; pytest OK", "pid": None}
    st.write("job-jrun1.json", done_job)
    res_compact = service.jobs("s1", action="result", job_id="jrun1", detail=False)
    self.assertNotIn("rationale", res_compact)
    self.assertNotIn("execution_environment", res_compact)
    self.assertIn("observed_tokens", res_compact)

    prompt = worker.packet({**done_job, "cwd": str(self.root), "source_cwd": str(self.root),
                            "role": self.cfg["agents"]["codex-builder"], "task": "Do work",
                            "context": "", "runtime_image": {"version": "0.26.6"}})
    self.assertIn(worker.OUTPUT_CONTRACT, prompt)

    am_root = self.root / ".agents"
    am_root.mkdir()
    sess = st.session("s1")
    sess["am_binding"] = {"task_id": "t-am", "root": str(am_root)}
    st.save_session("s1", sess)
    st.update_job("jrun1", status="failed", failure_kind="infrastructure", permissions="workspace-shell")
    with patch("handoff.subprocess.run") as run_mock:
      run_mock.return_value = Mock(stdout=json.dumps({"source_id": "unused", "am_task_id": "t-am", "revision": 99}))
      handoff._sync("s1")
    outbox = st.read("handoff-s1.json")
    exported_job = outbox["packet"]["jobs"][0]
    self.assertEqual(exported_job["permissions"], "workspace-shell")
    self.assertEqual(exported_job["failure_kind"], "infrastructure")

  def test_deepseek_defaults_to_flash_and_astra_stays_complex_only(self) -> None:
    """Keep DeepSeek defaulting to flash across tiers and shield Codex astra from routine work."""
    cfg = copy.deepcopy(st.config())
    cfg["providers"]["company"]["default_model"] = "deepseek-flash"
    cfg["agents"]["company-builder"]["default_model"] = "deepseek-flash"
    cfg["model_policy"] = {"mode": "sol-first", "quality_escalation": True}
    service.validate_config(cfg)
    st.write("config.json", cfg)

    bad_cfg = copy.deepcopy(cfg)
    bad_cfg["providers"]["company"]["default_model"] = "missing-model"
    with self.assertRaisesRegex(ValueError, "default_model"):
      service.validate_config(bad_cfg)

    with patch("service._catalog", return_value=self.company_models):
      cat = service.catalog(agent_name="company-builder")
      company_info = next(p for p in cat["configured_providers"] if p["name"] == "company")
      self.assertEqual(company_info.get("default_model"), "deepseek-flash")

    role = cfg["agents"]["company-builder"]
    provider = cfg["providers"]["company"]
    for comp in ("simple", "standard", "complex"):
      chosen_model, _ = opt.role_pair(role, provider, self.company_models, "auto", "auto", comp, "company")
      self.assertEqual(chosen_model, "deepseek-flash")
    escalated_model, escalated_effort = opt.role_pair(
      role, provider, self.company_models, "auto", "auto", "complex", "company", escalate=True,
    )
    self.assertEqual((escalated_model, escalated_effort), ("deepseek-v4-pro", "high"))

    with patch("service.catalog", side_effect=self._catalog), \
         patch("service.quota", side_effect=self._quota):
      routine_high_risk_review = opt.plan("s1", work_item="rev-risk", operation="read", task_profile={
        "kind": "review", "scope": "cross-module", "uncertainty": "medium", "risk": "high",
        "validation": "tests", "requires_shell": True,
      }, cwd=str(self.root), permissions="workspace-shell")
      self.assertEqual(
        (routine_high_risk_review["complexity"], routine_high_risk_review["model"],
         routine_high_risk_review["effort"]),
        ("standard", "gpt-6.1-sol", "high"),
      )

      research_replay = opt.plan("s1", work_item="res-replay", operation="write", task_profile={
        "kind": "research", "scope": "cross-module", "uncertainty": "high", "risk": "medium",
        "validation": "replay", "requires_shell": True,
      }, cwd=str(self.root), permissions="workspace-shell")
      self.assertEqual(
        (research_replay["complexity"], research_replay["model"], research_replay["effort"]),
        ("standard", "gpt-6.1-sol", "high"),
      )

      system_med = opt.plan("s1", work_item="sys-med", operation="write", task_profile={
        "kind": "implementation", "scope": "system", "uncertainty": "medium", "risk": "medium",
        "validation": "replay", "requires_shell": True,
      }, cwd=str(self.root), permissions="workspace-shell")
      self.assertEqual(
        (system_med["complexity"], system_med["model"], system_med["effort"]),
        ("standard", "gpt-6.1-sol", "high"),
      )

      arch = opt.plan("s1", work_item="arch-hard", operation="write", task_profile={
        "kind": "architecture", "scope": "system", "uncertainty": "high", "risk": "high",
        "validation": "tests", "requires_shell": True,
      }, cwd=str(self.root), permissions="workspace-shell", escalate=True)
      self.assertEqual((arch["complexity"], arch["model"], arch["effort"]), ("complex", "gpt-6.1-astra", "high"))

      # Non-DeepSeek fast default_model must never promote terra or haiku to complex tier.
      codex_role_def = {**cfg["agents"]["codex-builder"], "default_model": "gpt-6.1-terra"}
      codex_prov_def = {**cfg["providers"]["codex"], "default_model": "gpt-6.1-terra"}
      with self.assertRaisesRegex(ValueError, "No allowed"):
        opt.role_pair(
          codex_role_def, codex_prov_def, self.codex_models, "auto", "auto", "complex", "codex",
        )
      claude_cat = {"models": ["haiku", "sonnet"], "efforts": ["low", "medium", "high"]}
      claude_prov_def = {"kind": "claude", "default_model": "haiku", "models": ["haiku", "sonnet"]}
      with self.assertRaisesRegex(ValueError, "No allowed"):
        opt.role_pair(
          {"provider": "claude", "default_model": "haiku"},
          claude_prov_def, claude_cat, "auto", "auto", "complex", "claude",
        )

      # plan() and service.run() must stay consistent when needs-work lacks evidence on a standard task.
      service.control("s1", "on", agents=["codex-builder"], strategy="preserve-claude")
      prior_sol = {
        "id": "jsol-noev", "session_id": "s1", "work_item": "std-retry", "agent": "codex-builder",
        "provider_name": "codex", "provider": {"kind": "codex"}, "model": "gpt-6.1-sol",
        "effort": "medium", "status": "completed", "review": "needs-work", "review_evidence": "",
        "turn_id": "t-prev",
      }
      st.write("job-jsol-noev.json", prior_sol)
      task_rec = ac.load(self.task["id"])
      task_rec["jobs"].append("jsol-noev")
      ac.save(task_rec)
      retry_plan = opt.plan("s1", work_item="std-retry", operation="write", task_profile={
        "kind": "implementation", "scope": "bounded", "uncertainty": "medium", "risk": "medium",
        "validation": "tests", "requires_shell": True,
      }, cwd=str(self.root), permissions="workspace-shell", escalate=True)
      self.assertEqual(retry_plan["action"], "replan")
      self.assertIn("strictly for complex", retry_plan["errors"].get("codex:gpt-6.1-astra", ""))


if __name__ == "__main__":
  unittest.main()

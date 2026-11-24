"""Tests for the zero-token Khazix-inspired advisory dispatch engine and policy gates."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import accounting as ac
import advisory
import dispatch_policy
import optimizer as opt
import routing_policy as policy
import service
import storage as st


def _approx_tokens(payload: dict[str, object]) -> int:
  """Estimate upper-bound token count via JSON lexical/punctuation splitting."""
  raw = json.dumps(payload, ensure_ascii=False)
  return max(len(raw) // 4, len(re.findall(r"[A-Za-z0-9_]+|[^\w\s]", raw)))


class AdvisoryDispatchTests(unittest.TestCase):
  """Verify zero-token advisory routing, contracts, inline gates, and loop-breakers."""

  def setUp(self) -> None:
    """Create isolated delegate state with 4-provider test configuration."""
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-advisory-")
    self.root = Path(self.temp.name)
    self.state_dir = self.root / "state"
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.state_dir)}, clear=False)
    self.env.start()
    for key in ("DELEGATE_CALLER_HOST", "ANTIGRAVITY_CONVERSATION_ID", "CODEX_THREAD_ID", "DSH_SESSION_ID"):
      os.environ.pop(key, None)

    self.cfg = copy.deepcopy(st.DEFAULT_CONFIG)
    self.cfg["providers"]["codex"] = {"kind": "codex", "enabled": True, "command": "codex"}
    self.cfg["providers"]["claude"] = {
      "kind": "claude",
      "enabled": True,
      "command": "claude",
      "models": ["haiku", "sonnet", "opus"],
      "efforts": ["low", "medium", "high"],
    }
    self.cfg["providers"]["antigravity"] = {
      "kind": "antigravity",
      "enabled": True,
      "command": "agy",
      "models": [],
      "efforts": ["low", "medium", "high", "xhigh", "max"],
    }
    self.cfg["providers"]["deepseek-company"] = {
      "kind": "anthropic",
      "enabled": True,
      "command": "claude",
      "base_url": "https://example.invalid/anthropic",
      "api_key_env": "TEST_DS_KEY",
      "default_model": "deepseek-flash",
      "models": ["deepseek-flash", "deepseek-v4-pro"],
      "efforts": ["low", "medium", "high", "max"],
    }
    self.cfg["agents"]["agy-advisor"] = {
      "provider": "antigravity",
      "enabled": True,
      "automatic": False,
      "workspace": "read-only",
      "instructions": "Provide independent second opinion and natural UX copy polish.",
    }
    self.cfg["agents"]["agy-builder"] = {
      "provider": "antigravity",
      "enabled": True,
      "automatic": False,
      "workspace": "worktree",
      "instructions": "Implement changes in worktree.",
    }
    self.cfg["agents"]["deepseek-company-reviewer"] = {
      "provider": "deepseek-company",
      "enabled": True,
      "automatic": True,
      "workspace": "read-only",
      "instructions": "Review bounded files.",
    }
    self.cfg["agents"]["deepseek-company-builder"] = {
      "provider": "deepseek-company",
      "enabled": True,
      "automatic": True,
      "workspace": "worktree",
      "instructions": "Apply bounded mechanical edits.",
    }
    st.write("config.json", self.cfg)
    st.write("catalog-codex.json", {
      "provider": "codex",
      "observed_at": 9999999999.0,
      "models": [
        {"model": "gpt-6.2-sol", "supportedReasoningEfforts": [
          {"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh")
        ]},
        {"model": "gpt-6.1-sol", "supportedReasoningEfforts": [
          {"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh")
        ]},
        {"model": "gpt-6.1-terra", "supportedReasoningEfforts": [
          {"reasoningEffort": e} for e in ("low", "medium")
        ]},
        {"model": "gpt-6.2-astra", "supportedReasoningEfforts": [
          {"reasoningEffort": e} for e in ("high", "xhigh")
        ]},
      ],
    })

  def tearDown(self) -> None:
    """Clean up temporary state directory and environment patch."""
    self.env.stop()
    self.temp.cleanup()

  def test_workflows_and_sensitive_scope_precision(self) -> None:
    """Verify WORKFLOWS tuple, sensitive path/task detection, and zero false positives."""
    self.assertEqual(
      advisory.WORKFLOWS,
      (
        "auto",
        "dual-plan",
        "frontend",
        "copy-ux",
        "backend-module",
        "gate-review",
        "bulk-cheap",
        "browser-research",
      ),
    )
    for required_tok in (
      "auth", "login", "token", "secret", "cred", "permission", "acl",
      "billing", "payment", "money", "feishu", "webhook", "publish", "prod-config",
    ):
      self.assertIn(required_tok, advisory.SENSITIVE_PATH_TOKENS)

    for sensitive_path in (
      "src/auth/session.py",
      "app/login_handler.ts",
      "pkg/token/refresh.go",
      "config/secret_loader.py",
      "infra/cred_store.py",
      "rbac/permission_check.py",
      "security/acl.py",
      "services/billing/invoice.py",
      "services/payment/stripe.py",
      "core/money_math.py",
      "integrations/feishu/card.py",
      "api/webhook_receiver.py",
      "scripts/publish_release.py",
      "deploy/prod-config.yaml",
    ):
      self.assertTrue(advisory.is_sensitive_scope([sensitive_path]), msg=sensitive_path)

    self.assertTrue(advisory.is_sensitive_scope([], "Check 算钱 rounding and 飞书对外发消息 logic"))
    self.assertTrue(advisory.is_sensitive_scope([], "Audit OAuth access token rotation and RBAC permissions"))

    # False-positive guards on routine code paths and LLM token terminology.
    self.assertFalse(advisory.is_sensitive_scope(
      ["plugins/delegate/scripts/advisory.py", "src/author.py", "ui/display_payload.tsx"],
      "Optimize zero-token advisory payload size under 120 tokens",
    ))

  def test_inline_break_even_gate_prevents_overhead_inversion(self) -> None:
    """Keep <=40-line edits, trivial fixes, tight UI<->API coupling, and review fixes inline."""
    small = advisory.recommend(
      "Update retry backoff constant in worker module",
      caller_host="claude",
      workflow="backend-module",
      estimated_lines=28,
    )
    self.assertEqual(small["action"], "keep_inline")
    self.assertIsNone(small["agent"])

    boundary = advisory.recommend(
      "Adjust parser helper validation",
      caller_host="claude",
      workflow="auto",
      estimated_lines=40,
    )
    self.assertEqual(boundary["action"], "keep_inline")

    substantial = advisory.recommend(
      "Implement standalone order-book matching engine module",
      caller_host="claude",
      workflow="backend-module",
      estimated_lines=120,
    )
    self.assertEqual(substantial["action"], "delegate")
    self.assertEqual(substantial["agent"], "codex-builder")

    coupled = advisory.recommend(
      "Wire checkout page form state and API endpoint validation back-and-forth (和页面来回对接口)",
      caller_host="claude",
      workflow="auto",
      estimated_lines=90,
    )
    self.assertEqual(coupled["action"], "keep_inline")

    coupled_files = advisory.recommend(
      "Add filter controls and query parameter handling",
      caller_host="claude",
      workflow="auto",
      required_files=["web/FilterPanel.tsx", "server/routes.py"],
      estimated_lines=85,
    )
    self.assertEqual(coupled_files["action"], "keep_inline")

    review_fix = advisory.recommend(
      "Fix PR review comments from round 1 (修审查发现的问题)",
      caller_host="claude",
      workflow="auto",
      estimated_lines=65,
    )
    self.assertEqual(review_fix["action"], "keep_inline")

    typo = advisory.recommend("Fix a typo in status label (只改几个字)", caller_host="claude", workflow="copy-ux")
    self.assertEqual(typo["action"], "keep_inline")

  def test_four_host_complementary_routing_and_self_avoidance(self) -> None:
    """Verify complementary routing and host self-avoidance across Claude, Codex, AGY, and DSH."""
    # 1. dual-plan: blind second opinion -> agy-advisor, except antigravity -> claude-reviewer.
    for host in ("claude", "codex", "dsh"):
      rec = advisory.recommend(
        "Design multi-region event store architecture and retention schema",
        caller_host=host,
        workflow="dual-plan",
      )
      self.assertEqual((rec["action"], rec["agent"], rec["blind_context"]), ("dual_opinion", "agy-advisor", True))
    agy_plan = advisory.recommend(
      "Design multi-region event store architecture and retention schema",
      caller_host="antigravity",
      workflow="dual-plan",
    )
    self.assertEqual((agy_plan["action"], agy_plan["agent"], agy_plan["blind_context"]),
                     ("dual_opinion", "claude-reviewer", True))

    # Fallback when claude-reviewer is disabled on antigravity -> codex-reviewer.
    no_claude_agents = copy.deepcopy(self.cfg["agents"])
    no_claude_agents["claude-reviewer"]["enabled"] = False
    agy_fallback = advisory.recommend(
      "Design distributed cache architecture",
      caller_host="agy",
      workflow="dual-plan",
      configured_agents=no_claude_agents,
    )
    self.assertEqual(agy_fallback["agent"], "codex-reviewer")

    # 2. copy-ux: agy-advisor (blind_context=False), or claude-reviewer when caller is antigravity.
    copy_from_claude = advisory.recommend(
      "Polish onboarding empty-state copy and interaction wording",
      caller_host="claude",
      workflow="copy-ux",
    )
    self.assertEqual(
      (copy_from_claude["action"], copy_from_claude["agent"], copy_from_claude["blind_context"]),
      ("delegate", "agy-advisor", False),
    )
    copy_from_agy = advisory.recommend(
      "Polish onboarding empty-state copy and interaction wording",
      caller_host="antigravity",
      workflow="copy-ux",
    )
    self.assertEqual((copy_from_agy["action"], copy_from_agy["agent"]), ("delegate", "claude-reviewer"))

    # 3. frontend: inline on claude; claude-builder on codex/antigravity/dsh.
    fe_claude = advisory.recommend(
      "Build responsive landing page hero visual animation and theme layout",
      caller_host="claude",
      workflow="frontend",
      estimated_lines=180,
    )
    self.assertEqual(fe_claude["action"], "keep_inline")
    for host in ("codex", "antigravity", "dsh"):
      fe_other = advisory.recommend(
        "Build responsive landing page hero visual animation and theme layout",
        caller_host=host,
        workflow="frontend",
        estimated_lines=180,
      )
      self.assertEqual((fe_other["action"], fe_other["agent"]), ("delegate", "claude-builder"))

    # 4. backend-module: inline on codex; codex-builder on claude/antigravity/dsh.
    be_codex = advisory.recommend(
      "Implement isolated parquet compaction algorithm",
      caller_host="codex",
      workflow="backend-module",
      estimated_lines=200,
    )
    self.assertEqual(be_codex["action"], "keep_inline")
    be_claude = advisory.recommend(
      "Implement isolated parquet compaction algorithm",
      caller_host="claude",
      workflow="backend-module",
      estimated_lines=200,
    )
    self.assertEqual((be_claude["action"], be_claude["agent"], be_claude["model"]),
                     ("delegate", "codex-builder", "gpt-6.2-sol"))

    # 5. bulk-cheap: inline on dsh; deepseek-flash default when enabled; haiku explorer fallback.
    bulk_dsh = advisory.recommend(
      "Sync docstrings and changelog entries across modules",
      caller_host="dsh",
      workflow="bulk-cheap",
      estimated_lines=100,
    )
    self.assertEqual(bulk_dsh["action"], "keep_inline")
    bulk_read = advisory.recommend(
      "Explore codebase documentation and summarize module layout",
      caller_host="claude",
      workflow="bulk-cheap",
    )
    self.assertEqual(
      (bulk_read["action"], bulk_read["agent"], bulk_read["model"], bulk_read["effort"]),
      ("delegate", "deepseek-company-reviewer", "deepseek-flash", "low"),
    )
    no_ds_agents = {k: v for k, v in self.cfg["agents"].items() if "deepseek" not in k}
    bulk_haiku = advisory.recommend(
      "Explore codebase documentation and summarize module layout",
      caller_host="codex",
      workflow="bulk-cheap",
      configured_agents=no_ds_agents,
    )
    self.assertEqual((bulk_haiku["agent"], bulk_haiku["model"]), ("claude-explorer", "haiku"))

    # 6. browser-research: inline on codex; codex-reviewer on claude/antigravity/dsh.
    br_codex = advisory.recommend(
      "Run multi-site Playwright research across logged-in dashboards",
      caller_host="codex",
      workflow="browser-research",
    )
    self.assertEqual(br_codex["action"], "keep_inline")
    br_claude = advisory.recommend(
      "Run multi-site Playwright research across logged-in dashboards",
      caller_host="claude",
      workflow="browser-research",
    )
    self.assertEqual((br_claude["action"], br_claude["agent"], br_claude["model"]),
                     ("delegate", "codex-reviewer", "gpt-6.2-sol"))

  def test_gate_review_sol_fallback_sensitive_xhigh_and_two_round_cap(self) -> None:
    """Verify Sol 6.2->6.1 fallback, sensitive-only xhigh, and 2-round circuit breaker."""
    normal_rev = advisory.recommend(
      "Pre-merge review of parser refactor diff",
      caller_host="claude",
      workflow="gate-review",
      required_files=["src/parser/ast_nodes.py"],
      review_round=0,
    )
    self.assertEqual(
      (normal_rev["action"], normal_rev["agent"], normal_rev["model"], normal_rev["effort"]),
      ("delegate", "codex-reviewer", "gpt-6.2-sol", "high"),
    )
    self.assertTrue(normal_rev["review_gate"])
    self.assertFalse(normal_rev["sensitive_scope"])
    self.assertEqual(normal_rev["max_review_rounds"], 2)

    sensitive_rev = advisory.recommend(
      "Pre-merge review of billing webhook signature check",
      caller_host="antigravity",
      workflow="gate-review",
      required_files=["services/billing/webhook.py"],
      review_round=1,
    )
    self.assertEqual(
      (sensitive_rev["action"], sensitive_rev["agent"], sensitive_rev["model"], sensitive_rev["effort"]),
      ("delegate", "codex-reviewer", "gpt-6.2-sol", "xhigh"),
    )
    self.assertTrue(sensitive_rev["sensitive_scope"])

    # Fallback to gpt-6.1-sol when 6.2 is absent from catalog.
    st.write("catalog-codex.json", {
      "provider": "codex",
      "observed_at": 9999999999.0,
      "models": [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [
        {"reasoningEffort": "high"}, {"reasoningEffort": "xhigh"},
      ]}],
    })
    fallback_rev = advisory.recommend(
      "Pre-merge review of auth login flow",
      caller_host="claude",
      workflow="gate-review",
      required_files=["src/auth/login.py"],
    )
    self.assertEqual((fallback_rev["model"], fallback_rev["effort"]), ("gpt-6.1-sol", "xhigh"))

    # Round >= 2 circuit breaker stops endless nitpick loops.
    halted = advisory.recommend(
      "Pre-merge review round 3",
      caller_host="claude",
      workflow="gate-review",
      review_round=2,
    )
    self.assertEqual(halted["action"], "escalate_to_user")
    self.assertEqual(halted["max_review_rounds"], 2)
    self.assertIsNone(halted["agent"])

  def test_zero_token_contracts_and_serialized_payload_budget(self) -> None:
    """Verify 0-char default contract, bounded blind/gate snippets, and <=120-token recommendations."""
    self.assertEqual(advisory.contract_snippet(), "")
    self.assertEqual(advisory.contract_snippet("backend-module", blind_context=False, review_gate=False), "")

    blind = advisory.contract_snippet("dual-plan", blind_context=True)
    self.assertLessEqual(len(blind), 240)
    self.assertIn("[speculative]", blind)
    self.assertIn("independent", blind.lower())

    gate = advisory.contract_snippet("gate-review", review_gate=True)
    self.assertLessEqual(len(gate), 280)
    self.assertIn("BLOCKING", gate)
    self.assertIn("ADVISORY", gate)
    self.assertIn("trigger path", gate)

    required_keys = {
      "action", "workflow", "agent", "model", "effort",
      "blind_context", "review_gate", "max_review_rounds", "sensitive_scope", "reason",
    }
    for wf in advisory.WORKFLOWS:
      for host in ("claude", "codex", "antigravity", "dsh"):
        rec = advisory.recommend(
          "Evaluate payment settlement architecture and implementation",
          caller_host=host,
          workflow=wf,
          required_files=["services/payment/settlement.py"],
          estimated_lines=120,
        )
        self.assertEqual(set(rec.keys()), required_keys)
        self.assertLessEqual(_approx_tokens(rec), 120, msg=f"{host}/{wf}: {rec}")

  def test_optimizer_plan_and_work_state_integration_with_sol_first(self) -> None:
    """Verify antigravity preferences, sol-first Astra guard, sensitive Sol xhigh, and 2-round cap."""
    self.assertIn("antigravity", policy.PROVIDER_PREFERENCES)
    self.assertEqual(policy.family("gemini-4-neon-high"), "gemini-neon")
    self.assertEqual(policy.family("gemini-3.8-flash-low"), "gemini-flash")
    self.assertEqual(opt.tier("gemini-4-neon-high"), 2)
    self.assertEqual(opt.tier("gemini-3.8-flash-low"), 0)

    # Enable sol-first and verify Astra stays gated on complex backend-module while Sol xhigh works on review.
    cfg = copy.deepcopy(st.config())
    cfg["model_policy"] = {"mode": "sol-first", "quality_escalation": True}
    st.write("config.json", cfg)

    be_simple = advisory.recommend(
      "Add simple fixture formatter helper",
      caller_host="claude",
      workflow="backend-module",
      task_profile={
        "kind": "implementation", "scope": "bounded", "uncertainty": "low", "risk": "low", "validation": "tests",
      },
      estimated_lines=60,
    )
    self.assertEqual((be_simple["model"], be_simple["effort"]), ("gpt-6.1-terra", "low"))

    be_complex_sol_first = advisory.recommend(
      "Design lock-free distributed transaction log",
      caller_host="claude",
      workflow="backend-module",
      task_profile={
        "kind": "architecture", "scope": "system", "uncertainty": "high", "risk": "high", "validation": "tests",
      },
      estimated_lines=250,
    )
    self.assertEqual((be_complex_sol_first["model"], be_complex_sol_first["effort"]), ("gpt-6.2-sol", "high"))

    # Under capability-first, genuine complex backend-module can recommend Astra.
    cfg["model_policy"] = {"mode": "capability-first", "quality_escalation": True}
    st.write("config.json", cfg)
    be_complex_cap = advisory.recommend(
      "Design lock-free distributed transaction log",
      caller_host="claude",
      workflow="backend-module",
      task_profile={
        "kind": "architecture", "scope": "system", "uncertainty": "high", "risk": "high", "validation": "tests",
      },
      estimated_lines=250,
    )
    self.assertEqual((be_complex_cap["model"], be_complex_cap["effort"]), ("gpt-6.2-astra", "high"))

    # Re-enable sol-first and run optimizer.plan for sensitive gate-review -> gpt-6.2-sol xhigh!
    cfg["model_policy"] = {"mode": "sol-first", "quality_escalation": True}
    st.write("config.json", cfg)
    auth_dir = self.root / "repo" / "auth"
    auth_dir.mkdir(parents=True)
    auth_file = auth_dir / "login.py"
    auth_file.write_text("def check_token() -> bool:\n  return True\n", encoding="utf-8")

    service.control("s-adv", "on", agents=["codex-reviewer"], strategy="preserve-claude")
    task = ac.begin("s-adv", category="review", complexity="standard")
    quota_ok = {"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 15}}}}
    with patch("service.quota", return_value=quota_ok):
      plan_res = opt.plan(
        "s-adv",
        work_item="pr-gate",
        operation="read",
        cwd=str(self.root / "repo"),
        required_files=["auth/login.py"],
        workflow="gate-review",
        review_gate=True,
        task_profile={
          "kind": "review", "scope": "cross-module", "uncertainty": "medium", "risk": "high",
          "validation": "inspection",
        },
      )
    self.assertEqual((plan_res["action"], plan_res["agent"], plan_res["model"], plan_res["effort"]),
                     ("delegate", "codex-reviewer", "gpt-6.2-sol", "xhigh"))
    self.assertIn("advisory", plan_res)
    self.assertEqual(plan_res["advisory"]["effort"], "xhigh")
    self.assertTrue(dispatch_policy.allow_xhigh_sol("gpt-6.2-sol", review_gate=True, sensitive_scope=True))
    self.assertEqual(
      dispatch_policy.reason(
        "gpt-6.2-sol", {}, {}, cfg=cfg, complexity="standard",
        effort="xhigh", review_gate=True, sensitive_scope=True,
      ),
      "",
    )

    # Verify work_state() and plan() halt after 2 needs-work review rounds!
    for idx in (1, 2):
      jid = f"jrev{idx}"
      st.write(f"job-{jid}.json", {
        "id": jid,
        "session_id": "s-adv",
        "work_item": "pr-gate",
        "agent": "codex-reviewer",
        "provider_name": "codex",
        "provider": {"kind": "codex"},
        "model": "gpt-6.2-sol",
        "effort": "xhigh",
        "status": "completed",
        "review": "needs-work",
        "review_evidence": f"Round {idx} finding",
        "turn_id": f"t-{idx}",
      })
      task["jobs"].append(jid)
    ac.save(task)

    ws = opt.work_state(task, "pr-gate")
    self.assertIsNotNone(ws)
    assert ws is not None
    self.assertEqual(ws["action"], "escalate_to_user")
    self.assertEqual(ws["review_round"], 2)
    self.assertEqual(ws["max_review_rounds"], 2)

    with patch("service.quota", return_value=quota_ok):
      capped_plan = opt.plan("s-adv", work_item="pr-gate", workflow="gate-review", review_gate=True)
    self.assertEqual(capped_plan["action"], "escalate_to_user")
    self.assertEqual(capped_plan["max_review_rounds"], 2)
    self.assertEqual(capped_plan["advisory"]["action"], "escalate_to_user")

  def test_qualified_gate_review_phrases_and_builder_repair_attempts_preserved(self) -> None:
    """Infer gate-review with 0-2 intervening words and keep builder max_attempts separate."""
    for phrase in (
      "pre-merge auth review",
      "pre-merge billing webhook audit",
      "security access token review",
      "review payment settlement diff",
    ):
      rec = advisory.recommend(phrase, caller_host="claude", workflow="auto")
      self.assertEqual(rec["workflow"], "gate-review", msg=phrase)
      self.assertEqual(rec["agent"], "codex-reviewer", msg=phrase)
      self.assertTrue(rec["review_gate"], msg=phrase)
      self.assertTrue(rec["sensitive_scope"], msg=phrase)
      self.assertEqual(rec["effort"], "xhigh", msg=phrase)

    fix_comment = advisory.recommend("Fix pre-merge auth review comments", caller_host="claude", workflow="auto")
    self.assertEqual(fix_comment["action"], "keep_inline")

    # Ordinary worktree builder needs-work rounds must honor task["max_attempts"] (e.g. 4), not cap at 2.
    build_task = ac.begin("s-build", category="implementation", complexity="standard")
    build_task["max_attempts"] = 4
    for idx in (1, 2):
      jid = f"jbuild{idx}"
      st.write(f"job-{jid}.json", {
        "id": jid,
        "session_id": "s-build",
        "work_item": "impl-mod",
        "agent": "codex-builder",
        "role": {"workspace": "worktree"},
        "provider_name": "codex",
        "provider": {"kind": "codex"},
        "model": "gpt-6.2-sol",
        "effort": "medium",
        "status": "completed",
        "review": "needs-work",
        "review_evidence": f"Test failure {idx}",
        "turn_id": f"tb-{idx}",
      })
      build_task["jobs"].append(jid)
    ac.save(build_task)
    self.assertIsNone(opt.work_state(build_task, "impl-mod"))

    for idx in (3, 4):
      jid = f"jbuild{idx}"
      st.write(f"job-{jid}.json", {
        "id": jid,
        "session_id": "s-build",
        "work_item": "impl-mod",
        "agent": "codex-builder",
        "role": {"workspace": "worktree"},
        "provider_name": "codex",
        "provider": {"kind": "codex"},
        "model": "gpt-6.2-sol",
        "effort": "high",
        "status": "completed",
        "review": "needs-work",
        "review_evidence": f"Test failure {idx}",
        "turn_id": f"tb-{idx}",
      })
      build_task["jobs"].append(jid)
    ac.save(build_task)
    limit_state = opt.work_state(build_task, "impl-mod")
    self.assertIsNotNone(limit_state)
    assert limit_state is not None
    self.assertEqual(limit_state["action"], "replan")


if __name__ == "__main__":
  unittest.main()

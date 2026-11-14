"""Quota and evidence must be considered before soft family preferences."""
from unittest.mock import patch
import time
import unittest

import test_routing_policy as fixtures
from test_routing_policy import HARD, DAILY
import routing_policy as policy
import optimizer as opt
import service
import storage as st


class OrchestrationTests(unittest.TestCase):
  setUp = fixtures.RoutingPolicyTests.setUp
  tearDown = fixtures.RoutingPolicyTests.tearDown

  def test_healthy_alternative_can_beat_family_preference_without_reset_data(self) -> None:
    def quota(provider: str, **kwargs):
      return {"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 90}}}} if provider == "codex" else {
        "status": "known", "data": {"five_hour": {"used_percentage": 20}}}
    with patch("service.quota", side_effect=quota):
      result = opt.plan("s", task_profile=HARD)
    self.assertEqual((result["provider"], result["model"], result["effort"]), ("company", "deepseek-v4-pro", "high"))
    self.assertEqual(result["selection_policy"]["deferred_low_headroom"], ["codex"])
    self.assertTrue(any(c.get("model") == "gpt-6-astra" for c in result["candidates"]))

  def test_unknown_alternative_quota_does_not_claim_surplus(self) -> None:
    with patch("service.quota", side_effect=lambda provider, **kw: {
        "status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 90}}}} if provider == "codex" else {"status": "unknown"}):
      result = opt.plan("s", task_profile=HARD)
    self.assertEqual(result["model"], "gpt-6-astra")
    self.assertEqual(result["selection_policy"]["deferred_low_headroom"], [])

  def test_all_known_percent_providers_respect_reserve(self) -> None:
    service.control("s", "on", agents=["company-reviewer"])
    with patch("service.quota", return_value={"status": "known", "data": {"five_hour": {"used_percentage": 99}}}):
      result = opt.plan("s", task_profile=HARD)
    self.assertEqual(result["action"], "replan")
    self.assertIn("reserve", result["errors"]["company"])

  def test_surplus_cannot_expand_role_pool_or_bypass_shell_requirement(self) -> None:
    service.control("s", "on", agents=["codex-reviewer"], strategy="preserve-claude")
    with patch("service.quota", return_value={"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 90}}}}):
      result = opt.plan("s", task_profile={**HARD, "validation": "tests"})
    self.assertEqual(result["provider"], "codex")

  def test_fixed_role_dispatch_rechecks_known_reserve_before_credentials_or_launch(self) -> None:
    with patch("service.quota", return_value={"status": "known", "data": {"five_hour": {"used_percentage": 99}}}), \
         patch("service.credentials.environment") as credentials, patch("service.subprocess.Popen") as launch:
      with self.assertRaisesRegex(ValueError, "configured reserve"):
        service.run("s", "company-reviewer", "deepseek-v4-pro", "high", "Read", "Evidence", "Check",
          self.tmp.name, task_profile=HARD)
      credentials.assert_not_called()
      launch.assert_not_called()

  def test_orchestration_distinguishes_unknowns_and_risk_without_starting_workers(self) -> None:
    with patch("service.subprocess.Popen") as launch:
      hard = opt.plan("s", task_profile=HARD)
      daily = opt.plan("s", task_profile=DAILY)
      launch.assert_not_called()
    self.assertEqual(hard["assessment"]["orchestration"]["pattern"], "resolve-then-execute")
    self.assertIn("Independent", hard["assessment"]["orchestration"]["review"])
    self.assertEqual(daily["assessment"]["orchestration"]["pattern"], "bounded-owner")

  def test_scoped_low_risk_fix_can_use_fast_lane_only_with_explicit_tests(self) -> None:
    scoped = {**DAILY, "uncertainty": "low", "risk": "low"}
    fast = opt.plan("s", operation="write", task_profile=scoped)
    self.assertEqual((fast["model"], fast["effort"]), ("gpt-6.1-terra", "low"))
    self.assertTrue(fast["assessment"]["requires_shell"])
    unchecked = opt.plan("s", operation="write", task_profile={**scoped, "validation": "inspection"})
    self.assertEqual(unchecked["complexity"], "standard")
    risky = opt.plan("s", operation="write", task_profile={**scoped, "risk": "high"})
    self.assertEqual(risky["complexity"], "complex")

  def test_empirical_cheapest_fifth_model_is_not_lost_by_catalog_shortlist(self) -> None:
    self.codex["models"] = [{"model": 'fixture-sol-'+str(i), 'supportedReasoningEfforts': [{'reasoningEffort':'medium'}]}
      for i in range(6)]
    service.control('s', 'on', agents=['codex-reviewer'], strategy='preserve-claude')
    def observations(task, agent, model, effort, history):
      return {'attempts':5, 'accepted':5, 'unreviewed':0, 'cost_samples':5,
        'smoothed_success_rate':.85, 'mean_attempt_tokens':1000 if model.endswith('-5') else 10000,
        'mean_parent_share':100}
    with patch('optimizer.observations',side_effect=observations):
      result = opt.plan('s', category='implementation', task_profile={**DAILY,'validation':'inspection'})
    self.assertEqual(result['model'], 'fixture-sol-5')
    self.assertEqual(result['selection_policy']['eligible_candidates'], 6)


class CandidateSelectionTests(unittest.TestCase):
  def candidate(self, model: str, tokens: int, remaining: float | None, tier: int = 2) -> dict:
    return {"action": "delegate", "model": model, "provider": model, "capability_tier": tier,
      "preference_rank": policy.preference(model, "complex"), "expected_processed_tokens": tokens,
      "quota": {"remaining_percent": remaining, "surplus_priority": 0}}

  def test_expected_work_outside_band_overrides_preferred_family(self) -> None:
    expensive = self.candidate("astra", 200000, 80)
    efficient = self.candidate("deepseek-pro", 100000, 80)
    chosen, why = policy.select([expensive, efficient], 2)
    self.assertIs(chosen, efficient)
    self.assertEqual(why["within_cost_band"], 1)

  def test_surplus_does_not_buy_unbounded_extra_work(self) -> None:
    scarce = self.candidate("astra", 100000, 10)
    ample = self.candidate("deepseek-pro", 120000, 90)
    chosen, _ = policy.select([scarce, ample], 2)
    self.assertIs(chosen, scarce)

  def test_same_model_accounts_use_remaining_quota_even_without_reset(self) -> None:
    scarce = self.candidate("astra", 100000, 45)
    ample = self.candidate("astra", 100000, 80)
    chosen, _ = policy.select([scarce, ample], 2)
    self.assertIs(chosen, ample)

  def test_reset_priority_uses_claude_spend_limit_when_it_is_limiting(self) -> None:
    sample = {"status": "known", "data": {"five_hour": {"used_percentage": 5, "resets_at": time.time()+60},
      "spend_limit": {"used_percentage": 80, "resets_at": time.time()+86400}}}
    pressure = opt.pressure(sample, "claude")
    self.assertEqual(pressure["remaining_percent"], 20)
    self.assertGreater(pressure["reset_in_hours"], 23)


if __name__ == '__main__':
  unittest.main()

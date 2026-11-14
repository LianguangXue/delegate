"""Reasoned per-subtask signals and explicit model-family preferences, without classifier inference."""
from typing import Any

LEVELS = {"trivial": 0, "simple": 1, "standard": 2, "complex": 3}
KINDS = {"lookup", "documentation", "mechanical", "implementation", "review", "debugging", "architecture", "research"}
SCOPES = {"bounded", "cross-module", "system"}
RATINGS = {"low", "medium", "high"}
VALIDATIONS = {"inspection", "tests", "replay"}
MAX_AUTO_EFFORT = "xhigh"
SELECTION_POLICY = {"cost_band_percent": 15, "low_headroom_percent": 20, "healthy_headroom_percent": 40,
  "order": "capability/permissions/reserve, expected-work band, known quota pressure, tier/family preference, reset/headroom"}
PREFERENCES = {
  "simple": [["terra", "deepseek-flash"], ["luna", "haiku"], ["sol", "sonnet"], ["astra", "opus", "fable"]],
  "standard": [["sol"], ["sonnet"], ["astra", "opus", "fable", "deepseek-pro"]],
  "complex": [["astra"], ["opus", "fable"], ["deepseek-pro"]],
}


def family(model: str) -> str:
  """Identify routing families without substituting a requested model identifier."""
  name = model.lower()
  if "deepseek" in name:
    return "deepseek-flash" if "flash" in name else "deepseek-pro" if "pro" in name else "other"
  for known in ("terra", "astra", "sol", "luna", "haiku", "sonnet", "opus", "fable"):
    if known in name:
      return known
  return "other"


def preference(model: str, complexity: str) -> int:
  """Rank user workflow preferences within eligible capability tiers, not benchmarked speed."""
  groups = PREFERENCES.get(complexity, PREFERENCES["simple"])
  return next((i for i, group in enumerate(groups) if family(model) in group), len(groups))


def select(candidates: list[dict[str, Any]], desired_tier: int) -> tuple[dict[str, Any], dict[str, Any]]:
  """Choose among eligible routes without letting family preference hide cost or quota alternatives."""
  best = min(c["expected_processed_tokens"] for c in candidates)
  ceiling = best * (1 + SELECTION_POLICY["cost_band_percent"] / 100)
  close = [c for c in candidates if c["expected_processed_tokens"] <= ceiling]
  healthy = any(c["quota"].get("remaining_percent") is not None
    and c["quota"]["remaining_percent"] >= SELECTION_POLICY["healthy_headroom_percent"] for c in close)
  deferred = []
  if healthy:
    deferred = [c for c in close if c["quota"].get("remaining_percent") is not None
      and c["quota"]["remaining_percent"] <= SELECTION_POLICY["low_headroom_percent"]]
    close = [c for c in close if c not in deferred]
  def rank(candidate: dict[str, Any]) -> tuple:
    quota = candidate["quota"]
    tier = candidate["capability_tier"]
    remaining = quota.get("remaining_percent")
    return (abs((tier if tier is not None else desired_tier) - desired_tier),
      candidate.get("preference_rank", 0), -quota.get("surplus_priority", 0),
      -(remaining if remaining is not None else -1), candidate["expected_processed_tokens"])
  chosen = min(close, key=rank)
  return chosen, {**SELECTION_POLICY, "eligible_candidates": len(candidates), "within_cost_band": len(close) + len(deferred),
    "deferred_low_headroom": sorted({c.get("provider", "main") for c in deferred}),
    "minimum_expected_tokens": best, "expected_token_ceiling": round(ceiling),
    "quota_units": "remaining percentages are headroom signals, not comparable paid credits or token prices"}


def orchestration(profile: dict[str, Any] | None) -> dict[str, Any]:
  """Give the parent a bounded workflow recommendation without starting additional agents."""
  uncertain = bool(profile and (profile["uncertainty"] == "high" or profile["kind"] == "architecture"))
  risky = bool(profile and profile["risk"] == "high")
  return {"pattern": "resolve-then-execute" if uncertain else "bounded-owner",
    "first_step": "Resolve the unknowns and freeze interfaces/acceptance before assigning implementation"
      if uncertain else "Give one owner a complete bounded unit with inputs, outputs and acceptance",
    "review": "Independent targeted review of the risky change plus deterministic validation" if risky else
      "Parent checks artifacts and deterministic evidence; add a reviewer only for unresolved uncertainty",
    "parallelism": "Only independent ready tasks with disjoint writes; use configured concurrency limits",
    "handoff": "Pass decisions, exact input/artifact paths and unresolved questions, not the full transcript",
    "continuation": "Resume the existing owner for local corrections; replan missing inputs or infrastructure failures before model escalation",
    "waiting": "Use the managed waiter; no model polling or duplicate implementation in the parent",
    "execution": "Parent-applied guidance; this recommendation does not launch a workflow or alter permissions"}


def normalize(profile: dict[str, Any] | None) -> dict[str, Any] | None:
  """Validate the parent's own task assessment; profiles cannot enable accounts or tools."""
  if profile is None:
    return None
  choices = {"kind": KINDS, "scope": SCOPES, "uncertainty": RATINGS, "risk": RATINGS, "validation": VALIDATIONS}
  if not isinstance(profile, dict) or set(profile) - set(choices) - {"requires_shell", "reason"}:
    raise ValueError("task_profile accepts kind/scope/uncertainty/risk/validation, optional requires_shell/reason")
  for key, allowed in choices.items():
    if not isinstance(profile.get(key), str) or profile[key] not in allowed:
      raise ValueError("task_profile." + key + " must be one of: " + ", ".join(sorted(allowed)))
  if type(profile.get("requires_shell", False)) is not bool:
    raise ValueError("task_profile.requires_shell must be boolean")
  reason = profile.get("reason", "")
  if not isinstance(reason, str) or len(reason) > 600:
    raise ValueError("task_profile.reason must be text up to600 characters")
  return {**profile, "requires_shell": profile.get("requires_shell", False), "reason": reason}


def assess(profile: dict[str, Any] | None, requested: str = "", fallback: str = "standard") -> dict[str, Any]:
  """Resolve a per-item floor from explicit semantic factors, never task length or keyword counts."""
  profile = normalize(profile)
  if requested == "auto":
    requested = ""
  if requested and requested not in LEVELS:
    raise ValueError("complexity must be auto/trivial/simple/standard/complex")
  if profile is None:
    chosen = requested or fallback
    return {"complexity": chosen, "profile": None, "requires_shell": False,
      "source": "caller" if requested else "inherited", "signals": ["No structured assessment; retain the declared/inherited difficulty"],
      "orchestration": orchestration(None)}
  signals = []
  checked_fix = (profile["kind"] == "implementation" and profile["scope"] == "bounded"
    and profile["uncertainty"] == "low" and profile["risk"] == "low" and profile["validation"] == "tests")
  if profile["risk"] == "high" or profile["uncertainty"] == "high" or profile["scope"] == "system" or profile["kind"] == "architecture":
    floor = "complex"
    signals.append("High risk/uncertainty, system scope or architecture requires frontier reasoning")
  elif (profile["risk"] == "medium" or profile["uncertainty"] == "medium" or profile["scope"] == "cross-module"
        or (profile["kind"] in {"implementation", "review", "debugging", "research"} and not checked_fix)):
    floor = "standard"
    signals.append("Non-mechanical work, cross-module scope or remaining uncertainty requires a workhorse")
  else:
    floor = "simple"
    signals.append("Bounded, clear and low-risk work can use the fast lane")
    if checked_fix:
      signals.append("Scoped implementation has explicit test validation and low uncertainty/risk")
  chosen = max((floor, requested), key=lambda value: LEVELS.get(value, -1))
  if requested == "trivial" and floor == "simple":
    chosen = "trivial"  # Keep explicitly tiny, low-risk work in the parent.
  if requested and LEVELS[requested] < LEVELS[floor] and chosen != "trivial":
    signals.append("Raised the supplied complexity to the assessment's correctness floor")
  shell = profile["requires_shell"] or profile["validation"] in {"tests", "replay"}
  return {"complexity": chosen, "profile": profile, "requires_shell": shell,
          "source": "task_profile", "signals": signals, "orchestration": orchestration(profile)}


def input_schema() -> dict[str, Any]:
  """Expose assessment fields at the tool boundary so Claude can reason once in its current turn."""
  choices = {"kind": KINDS, "scope": SCOPES, "uncertainty": RATINGS, "risk": RATINGS, "validation": VALIDATIONS}
  return {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
    "required": list(choices), "properties": {**{k: {"type": "string", "enum": sorted(v)} for k, v in choices.items()},
      "requires_shell": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 600}}}]}

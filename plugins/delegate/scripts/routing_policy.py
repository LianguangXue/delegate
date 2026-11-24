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
  "simple": [
    ["terra", "deepseek-flash"],
    ["luna", "haiku", "gemini-flash"],
    ["sol", "sonnet", "gemini-pro"],
    ["astra", "opus", "fable", "gemini-neon", "deepseek-pro"],
  ],
  "standard": [
    ["sol"],
    ["deepseek-flash", "sonnet", "gemini-pro"],
    ["astra", "opus", "fable", "gemini-neon", "deepseek-pro"],
  ],
  "complex": [
    ["astra"],
    ["opus", "fable", "gemini-neon"],
    ["deepseek-flash"],
    ["deepseek-pro"],
  ],
}
PROVIDER_PREFERENCES: dict[str, dict[str, Any]] = {
  "claude": {
    "strengths": ["orchestration", "frontend", "visual-design", "synthesis"],
    "preferred_workflows": ["frontend"],
  },
  "codex": {
    "strengths": ["backend-module", "algorithms", "debugging", "gate-review", "browser-research"],
    "preferred_workflows": ["backend-module", "gate-review", "browser-research"],
  },
  "antigravity": {
    "strengths": ["dual-plan", "copy-ux", "brainstorming", "human-readable-copy"],
    "preferred_workflows": ["dual-plan", "copy-ux"],
    "families": ["gemini-neon", "gemini-pro", "gemini-flash"],
  },
  "deepseek": {
    "strengths": ["bulk-cheap", "documentation", "mechanical", "bounded-exploration"],
    "preferred_workflows": ["bulk-cheap"],
    "default_model": "deepseek-flash",
  },
}


def family(model: str) -> str:
  """Identify routing families without substituting a requested model identifier.

  Args:
    model: Model identifier string.

  Returns:
    Canonical routing family label.
  """
  name = model.lower()
  if "deepseek" in name:
    return "deepseek-flash" if "flash" in name else "deepseek-pro" if "pro" in name else "other"
  if "gemini" in name:
    if "flash" in name:
      return "gemini-flash"
    if "neon" in name or "hydrogen" in name or "orchestrator" in name:
      return "gemini-neon"
    return "gemini-pro"
  for known in ("terra", "astra", "sol", "luna", "haiku", "sonnet", "opus", "fable"):
    if known in name:
      return known
  return "other"


def preference(model: str, complexity: str) -> int:
  """Rank user workflow preferences within eligible capability tiers, not benchmarked speed."""
  groups = PREFERENCES.get(complexity, PREFERENCES["simple"])
  return next((i for i, group in enumerate(groups) if family(model) in group), len(groups))


def rank_key(candidate: dict[str, Any], desired_tier: int) -> tuple:
  """Compute deterministic selection ordering within the expected-work band.

  Args:
    candidate: Candidate route dictionary produced during planning.
    desired_tier: Target minimum capability tier (0=fast, 1=workhorse, 2=frontier).

  Returns:
    Tuple key ordered from lowest tier overshoot to expected token volume.
  """
  quota = candidate["quota"]
  tier = candidate["capability_tier"]
  remaining = quota.get("remaining_percent")
  return (
    abs((tier if tier is not None else desired_tier) - desired_tier),
    candidate.get("preference_rank", 0),
    candidate.get("generation_rank", 0),
    not candidate.get("provider_default", False),
    candidate.get("default_model_rank", 1),
    -quota.get("surplus_priority", 0),
    -(remaining if remaining is not None else -1),
    candidate["expected_processed_tokens"],
  )


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
  chosen = min(close, key=lambda c: rank_key(c, desired_tier))
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
  allowed_extra = {"requires_shell", "reason", "reasoning"}
  if not isinstance(profile, dict) or set(profile) - set(choices) - allowed_extra:
    raise ValueError(
      "task_profile accepts kind/scope/uncertainty/risk/validation, optional reasoning/requires_shell/reason"
    )
  for key, allowed in choices.items():
    if not isinstance(profile.get(key), str) or profile[key] not in allowed:
      raise ValueError("task_profile." + key + " must be one of: " + ", ".join(sorted(allowed)))
  if "reasoning" in profile and profile["reasoning"] is not None:
    if not isinstance(profile["reasoning"], str) or profile["reasoning"] not in RATINGS:
      raise ValueError("task_profile.reasoning must be one of: low, medium, high")
  if type(profile.get("requires_shell", False)) is not bool:
    raise ValueError("task_profile.requires_shell must be boolean")
  reason = profile.get("reason", "")
  if not isinstance(reason, str) or len(reason) > 600:
    raise ValueError("task_profile.reason must be text up to 600 characters")
  normalized = {**profile, "requires_shell": profile.get("requires_shell", False), "reason": reason}
  if normalized.get("reasoning") is None:
    normalized.pop("reasoning", None)
  return normalized


def assess(
  profile: dict[str, Any] | None,
  requested: str = "",
  fallback: str = "standard",
  *,
  review_gate: bool = False,
  sensitive_scope: bool = False,
) -> dict[str, Any]:
  """Resolve a per-item floor and recommended effort from explicit semantic factors.

  Args:
    profile: Optional normalized subtask assessment dictionary.
    requested: Optional explicit minimum complexity label.
    fallback: Inherited complexity when ``profile`` and ``requested`` are omitted.
    review_gate: Whether pre-merge 2-tier gate review was requested.
    sensitive_scope: Whether target files/task touch auth, money, external msg, or prod config.

  Returns:
    Assessment dictionary with resolved ``complexity``, ``recommended_effort``, and ``signals``.

  Raises:
    ValueError: If ``profile``, ``requested``, or boolean flags are invalid.
  """
  if type(review_gate) is not bool or type(sensitive_scope) is not bool:
    raise ValueError("review_gate and sensitive_scope must be booleans")
  profile = normalize(profile)
  if requested == "auto":
    requested = ""
  if requested and requested not in LEVELS:
    raise ValueError("complexity must be auto/trivial/simple/standard/complex")
  if profile is None:
    chosen = requested or fallback
    signals = ["No structured assessment; retain the declared/inherited difficulty"]
    if not requested and chosen == "complex":
      signals.append(
        "Inherited parent complexity=complex because task_profile was omitted; "
        "supply per-subtask task_profile to avoid frontier over-provisioning"
      )
    if review_gate and LEVELS[chosen] < LEVELS["standard"]:
      chosen = "standard"
      signals.append("Raised gate-review floor to standard workhorse")
    effort_map = {"trivial": "low", "simple": "low", "standard": "medium", "complex": "high"}
    rec_effort = effort_map[chosen]
    if review_gate:
      rec_effort = "xhigh" if sensitive_scope else "high"
      signals.append(
        "Sensitive-scope gate review uses xhigh reasoning on Sol"
        if sensitive_scope else "Pre-merge gate review uses high reasoning effort"
      )
    return {
      "complexity": chosen, "profile": None, "requires_shell": False,
      "recommended_effort": rec_effort,
      "source": "caller" if requested else "inherited",
      "signals": signals,
      "orchestration": orchestration(None),
    }
  signals = []
  reasoning = profile.get("reasoning")
  low_risk_lookup = (
    profile["kind"] in {"lookup", "documentation", "mechanical"}
    and profile["risk"] == "low"
    and profile["scope"] != "system"
    and reasoning != "high"
  )
  checked_fix = (
    profile["kind"] in {"implementation", "debugging"}
    and profile["scope"] == "bounded"
    and profile["uncertainty"] == "low"
    and profile["risk"] == "low"
    and profile["validation"] == "tests"
    and reasoning != "high"
  )
  bounded_clear_read = (
    profile["kind"] in {"lookup", "documentation", "mechanical", "research", "review"}
    and profile["scope"] == "bounded"
    and profile["uncertainty"] == "low"
    and profile["risk"] == "low"
    and profile["validation"] != "replay"
    and reasoning != "high"
  )
  complex_required = (
    profile["kind"] == "architecture"
    or (profile["risk"] == "high" and profile["uncertainty"] == "high")
    or (profile["scope"] == "system" and (profile["risk"] == "high" or profile["uncertainty"] == "high"))
    or (profile["risk"] == "high" and profile["validation"] == "inspection" and not review_gate)
  )
  if complex_required:
    floor = "complex"
    signals.append(
      "Architecture, system-wide high risk/uncertainty, or unverified high risk requires frontier reasoning"
    )
  elif (review_gate or reasoning == "high" or profile["risk"] in {"medium", "high"}
        or profile["uncertainty"] in {"medium", "high"}
        or profile["scope"] in {"cross-module", "system"}
        or (not checked_fix and not bounded_clear_read)):
    floor = "standard"
    signals.append(
      "Non-mechanical work, cross-module scope, reasoning depth or remaining uncertainty requires a workhorse"
    )
  else:
    floor = "simple"
    signals.append("Bounded, clear and low-risk work can use the fast lane")
    if checked_fix:
      signals.append("Scoped fix has explicit test validation and low uncertainty/risk")
  chosen = max((floor, requested), key=lambda value: LEVELS.get(value, -1))
  if requested == "trivial" and floor == "simple" and not review_gate:
    chosen = "trivial"  # Keep explicitly tiny, low-risk work in the parent.
  if requested and LEVELS[requested] < LEVELS[floor] and chosen != "trivial":
    signals.append("Raised the supplied complexity to the assessment's correctness floor")
  if chosen == "trivial":
    rec_effort = "low"
  elif chosen == "simple":
    rec_effort = "medium" if reasoning == "medium" else "low"
  elif chosen == "standard":
    deep_standard = (
      review_gate
      or reasoning == "high"
      or profile["validation"] == "replay"
      or profile["risk"] == "high"
      or (profile["uncertainty"] == "high" and not low_risk_lookup)
      or profile["scope"] == "system"
      or (profile["kind"] in {"debugging", "review"}
          and (profile["scope"] == "cross-module"
               or (profile["uncertainty"] == "medium" and profile["risk"] == "medium")))
      or (profile["scope"] == "cross-module"
          and profile["uncertainty"] == "medium" and profile["risk"] == "medium")
    )
    rec_effort = "high" if deep_standard else "medium"
    if deep_standard:
      signals.append("Raised workhorse effort to high for deep reasoning/replay/cross-module verification")
  else:
    rec_effort = "high"
  if sensitive_scope and (review_gate or profile["kind"] == "review"):
    rec_effort = "xhigh"
    signals.append("Sensitive-scope review raises effort to xhigh while preserving Sol workhorse lane")
  shell = profile["requires_shell"] or profile["validation"] in {"tests", "replay"}
  return {
    "complexity": chosen, "profile": profile, "requires_shell": shell,
    "recommended_effort": rec_effort,
    "source": "task_profile", "signals": signals, "orchestration": orchestration(profile),
  }


def input_schema() -> dict[str, Any]:
  """Expose assessment fields at the tool boundary so Claude can reason once in its current turn."""
  choices = {"kind": KINDS, "scope": SCOPES, "uncertainty": RATINGS, "risk": RATINGS, "validation": VALIDATIONS}
  hints = {
    "scope": "bounded, cross-module, or system (system only for repo/pipeline-wide changes).",
    "uncertainty": "low, medium, or high unknowns.",
    "risk": "low, medium, or high regression risk (reserve high+high or unverified high for Astra).",
  }
  props = {
    k: {
      "type": "string",
      "enum": sorted(v),
      **({"description": hints[k]} if k in hints else {}),
    }
    for k, v in choices.items()
  }
  return {
    "anyOf": [
      {"type": "null"},
      {
        "type": "object",
        "additionalProperties": False,
        "required": list(choices),
        "properties": {
          **props,
          "reasoning": {"type": "string", "enum": sorted(RATINGS)},
          "requires_shell": {"type": "boolean"},
          "reason": {"type": "string", "maxLength": 600},
        },
      },
    ],
  }

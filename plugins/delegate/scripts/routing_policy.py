"""Reasoned per-subtask signals and explicit model-family preferences, without classifier inference."""
from typing import Any

LEVELS = {"trivial": 0, "simple": 1, "standard": 2, "complex": 3}
KINDS = {"lookup", "documentation", "mechanical", "implementation", "review", "debugging", "architecture", "research"}
SCOPES = {"bounded", "cross-module", "system"}
RATINGS = {"low", "medium", "high"}
VALIDATIONS = {"inspection", "tests", "replay"}
MAX_AUTO_EFFORT = "xhigh"
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
      "source": "caller" if requested else "inherited", "signals": ["No structured assessment; retain the declared/inherited difficulty"]}
  signals = []
  if profile["risk"] == "high" or profile["uncertainty"] == "high" or profile["scope"] == "system" or profile["kind"] == "architecture":
    floor = "complex"
    signals.append("High risk/uncertainty, system scope or architecture requires frontier reasoning")
  elif (profile["risk"] == "medium" or profile["uncertainty"] == "medium" or profile["scope"] == "cross-module"
        or profile["kind"] in {"implementation", "review", "debugging", "research"}):
    floor = "standard"
    signals.append("Non-mechanical work, cross-module scope or remaining uncertainty requires a workhorse")
  else:
    floor = "simple"
    signals.append("Bounded, clear and low-risk work can use the fast lane")
  chosen = max((floor, requested), key=lambda value: LEVELS.get(value, -1))
  if requested == "trivial" and floor == "simple":
    chosen = "trivial"  # Keep explicitly tiny, low-risk work in the parent.
  if requested and LEVELS[requested] < LEVELS[floor] and chosen != "trivial":
    signals.append("Raised the supplied complexity to the assessment's correctness floor")
  shell = profile["requires_shell"] or profile["validation"] in {"tests", "replay"}
  return {"complexity": chosen, "profile": profile, "requires_shell": shell,
          "source": "task_profile", "signals": signals}


def input_schema() -> dict[str, Any]:
  """Expose assessment fields at the tool boundary so Claude can reason once in its current turn."""
  choices = {"kind": KINDS, "scope": SCOPES, "uncertainty": RATINGS, "risk": RATINGS, "validation": VALIDATIONS}
  return {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
    "required": list(choices), "properties": {**{k: {"type": "string", "enum": sorted(v)} for k, v in choices.items()},
      "requires_shell": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 600}}}]}

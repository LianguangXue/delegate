"""Distinguish proven pre-inference failures from model attempts and unknown usage."""
from typing import Any
import ast
import re


def positive_usage(job: dict[str, Any]) -> bool:
  """Let observed usage override any stale not-started marker."""
  raw = job.get("usage_delta")
  if not isinstance(raw, dict) or not {"inputTokens", "outputTokens"} <= raw.keys():
    raw = job.get("usage") or {}
    raw = raw.get("total", raw) if isinstance(raw, dict) else {}
  keys = {"inputTokens", "outputTokens", "totalTokens", "cachedInputTokens", "cacheWriteInputTokens",
          "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
  return any(type(value) in {int, float} and value > 0 for key, value in raw.items() if key in keys)


def legacy_preflight(job: dict[str, Any]) -> bool:
  """Recognize narrowly evidenced old startup errors, never absence of usage alone."""
  if job.get("status") != "failed" or job.get("turn_id") or job.get("execution_started_at") or positive_usage(job):
    return False
  error = job.get("error")
  if not isinstance(error, str):
    return False
  if error.startswith(("Legacy parent has no frozen runtime identity;", "Server/worker runtime mismatch;")):
    return True
  match = re.fullmatch(r"Command '(.+)' timed out after [0-9.]+ seconds", error)
  if match:
    try:
      command = ast.literal_eval(match[1])
      return (isinstance(command, list) and len(command) >= 8 and command[:1] == ["git"]
              and command[1] == "-C" and command[3:5] == ["worktree", "add"])
    except (ValueError, SyntaxError):
      pass
  return False


def model_attempted(job: dict[str, Any]) -> bool:
  """Count uncertain requests conservatively; exempt only proven not-started work."""
  if positive_usage(job):
    return True
  state = job.get("inference_state")
  if state == "not_started" and job.get("failure_kind") in {"capacity", "infrastructure"}:
    return False
  if job.get("turn_id"):
    return True
  if state in {"attempted", "running"}:
    return True
  if state == "not_started":
    return False
  if legacy_preflight(job):
    return False
  return True


def describe(job: dict[str, Any]) -> dict[str, Any]:
  """Expose the counting decision without silently treating unknown usage as zero."""
  counted = model_attempted(job)
  return {"counts_toward_model_attempt_limit": counted,
          "inference_state": job.get("inference_state", "unknown" if counted else "not_started"),
          "failure_kind": job.get("failure_kind") or ("infrastructure" if not counted and job.get("status") == "failed" else None)}


def recovery_advice(job: dict[str, Any]) -> dict[str, Any] | None:
  """Suggest the lowest-cost recovery action for terminal failures or needs-work reviews.

  Args:
    job: Job record dictionary.

  Returns:
    Compact recovery guidance dictionary, or None when no follow-up is indicated.
  """
  status = job.get("status")
  review = job.get("review")
  if status == "timed_out" and job.get("thread_id"):
    return {
      "action": "resume_or_split",
      "resume_job": job.get("id"),
      "note": "Resume with higher timeout_seconds to reuse thread/worktree progress, or split the task",
    }
  if status == "completed" and review == "needs-work" and job.get("thread_id"):
    return {
      "action": "resume",
      "resume_job": job.get("id"),
      "note": "Use resume_job with targeted feedback instead of restarting from scratch",
    }
  if status == "failed" and not model_attempted(job):
    if job.get("failure_kind") == "capacity":
      return {
        "action": "switch_or_wait",
        "note": (
          "Provider reported capacity/quota limit before inference (0 tokens); "
          "wait for cooldown or choose another allowed provider"
        ),
      }
    return {
      "action": "fix_contract",
      "note": "Pre-inference failure (0 model tokens); fix workspace, inputs, or permissions before retrying",
    }
  if status == "failed" and job.get("thread_id") and model_attempted(job):
    return {
      "action": "resume",
      "resume_job": job.get("id"),
      "note": "Inspect error/logs and resume_job if recoverable without repeating full setup",
    }
  return None

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
  if positive_usage(job) or job.get("turn_id"):
    return True
  state = job.get("inference_state")
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

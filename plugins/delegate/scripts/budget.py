"""Local quota evidence and a session-scoped delegation recommendation."""
from typing import Any
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

import storage as st
from lifecycle import identity, stop_owned

DEFAULTS = {"claude_low_remaining_percent": 20, "claude_critical_remaining_percent": 5,
            "prefer_external_when_unknown": True}


def read_claude(session_id: str = "") -> dict[str, Any]:
  """Return only fresh session-matched evidence; never infer quota from context tokens."""
  candidates = [st.read("claude-usage.json")]
  scoped = None
  if session_id:
    scoped = st.read("claude-quota-" + st.key(session_id) + ".json")
    candidates.append(scoped)
  candidate = st.read("claude-quota.json")
  if candidate and scoped is None and (not session_id or candidate.get("session_id") in {None, session_id}):
    candidates.append(candidate)
  values = []
  for value in candidates:
    if value is not None:
      if time.time() - value.get("observed_at", 0) > value.get("ttl_seconds", 300):
        value = dict(value, status="stale")
      values.append(value)
  if not values:
    return {"status": "unknown", "reason": "No Claude quota sample; statusline may not expose this account"}
  known = [v for v in values if v.get("status") in {"known", "limited"}]
  return max(known or values, key=lambda v: v.get("observed_at", 0))


def parse_usage(output: str) -> dict[str, Any]:
  """Parse the installed CLI's native /usage report; unexpected formats stay unknown."""
  windows = {}
  for label, name in (("Current session", "five_hour"), ("Current week (all models)", "seven_day")):
    match = re.search(r"^" + re.escape(label) + r":\s*(\d+(?:\.\d+)?)% used(?:\s*· resets (.+))?$", output, re.M)
    if not match or not valid_percent(float(match[1])):
      continue
    window: dict[str, Any] = {"used_percentage": float(match[1])}
    if match[2]:
      window["reset_description"] = match[2]
      date = re.fullmatch(r"(.+) \(([^)]+)\)", match[2])
      if date:
        try:
          zone = ZoneInfo(date[2])
          now = datetime.now(zone)
          for fmt in ("%Y %b %d, %I:%M%p", "%Y %b %d, %I%p"):
            for year in (now.year, now.year + 1):
              try:
                reset = datetime.strptime(f"{year} {date[1]}", fmt).replace(tzinfo=zone)
              except ValueError:
                continue
              if reset >= now - timedelta(days=180):
                window["resets_at"] = reset.timestamp()
                break
            if "resets_at" in window:
              break
        except (KeyError, ValueError):
          pass
    windows[name] = window
  return {"status": "known" if windows else "unknown", "observed_at": time.time(), "ttl_seconds": 120,
          "source": "Claude CLI /usage", "scope": "default Claude subscription account", "data": windows,
          "reason": None if windows else "Native /usage output did not contain recognized quota windows"}


def native_usage(command: str, env: dict[str, str]) -> tuple[int, str]:
  """Bound a native usage query and clean its entire process group without pipe waits."""
  with tempfile.TemporaryFile(mode="w+") as output, tempfile.TemporaryFile(mode="w+") as error:
    proc = subprocess.Popen([command, "-p", "--no-session-persistence", "--settings", '{"disableAllHooks":true}',
      "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--", "/usage"], cwd="/tmp", env=env,
      stdin=subprocess.DEVNULL, stdout=output, stderr=error, text=True, start_new_session=True)
    process_identity = identity(proc.pid, include_zombies=True)
    try:
      code = proc.wait(timeout=12)
      output.seek(0)
      return code, output.read(65536)
    finally:
      stop_owned(proc, process_identity)


def refresh_cli(force: bool = False) -> dict[str, Any]:
  """Read the native usage command with no inference, hooks, plugins or session persistence."""
  with st.coordination_files("claude-usage-refresh") as streams:
    try:
      st.try_coordination_lock(streams)
    except BlockingIOError:
      return read_claude()
    cached = st.read("claude-usage.json")
    if cached and not force and time.time() - cached.get("observed_at", 0) < 60:
      return cached
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    for name in ("CLAUDECODE", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                 "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_API_TYPE", "CLAUDE_CODE_HEADERS", "CLAUDE_CODE_EFFORT_LEVEL"):
      env.pop(name, None)
    env.update(DELEGATE_WORKER="1", CLAUDE_CODE_SAFE_MODE="1")
    command = st.config()["providers"].get("claude", {}).get("command", "claude")
    try:
      code, output = native_usage(command, env)
      value = parse_usage(output) if code == 0 else {
        "status": "unknown", "source": "Claude CLI /usage", "observed_at": time.time(),
        "reason": "Native /usage command failed (exit " + str(code) + ")"}
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
      value = {"status": "unknown", "source": "Claude CLI /usage", "observed_at": time.time(),
               "reason": type(exc).__name__ + " while querying native /usage"}
    st.write("claude-usage.json", value)
    return value


def schedule_refresh() -> None:
  """Refresh at most once per minute in a bounded helper, never blocking a Claude hook."""
  with st.locked():
    last = st.read("claude-usage-refresh-request.json", {}).get("at", 0)
    cached = st.read("claude-usage.json", {}).get("observed_at", 0)
    if time.time() - max(last, cached) < 60:
      return
    st.write("claude-usage-refresh-request.json", {"at": time.time()})
  subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "refresh"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def capture(payload: dict[str, Any]) -> dict[str, Any]:
  """Capture documented statusline fields, preserving a fresh explicit manual fallback."""
  sid = payload.get("session_id")
  if not sid:
    return {"status": "unknown", "reason": "Statusline omitted session_id"}
  st.key(sid)
  rate = payload.get("rate_limits")
  usable = isinstance(rate, dict) and any(
    isinstance(rate.get(name), dict) and valid_percent(rate[name].get("used_percentage"))
    for name in ("five_hour", "seven_day", "spend_limit"))
  value = {"status": "known" if usable else "unknown", "observed_at": time.time(), "session_id": sid,
           "source": "Claude statusline", "data": rate if isinstance(rate, dict) else None,
           "reason": None if usable else "Claude supplied no usable rate_limits; context usage is not quota"}
  old = read_claude(sid)
  if not usable and old.get("source") == "user-reported" and old.get("status") == "known":
    return old
  st.write("claude-quota-" + sid + ".json", value)
  st.write("claude-quota.json", value)
  return value


def valid_percent(value: Any) -> bool:
  """Reject strings, booleans, NaN and out-of-range percentages."""
  return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100


def remaining(sample: dict[str, Any], provider: str) -> float | None:
  """Compute the tightest reported live window without converting percentage to tokens."""
  if sample.get("status") == "limited" and not sample.get("model"):
    return 0
  if sample.get("status") != "known":
    return None
  data = sample.get("data") or {}
  if provider == "codex":
    bucket = data.get("rateLimits") or {}
    if data.get("ordinaryUsageAllowed") is False or bucket.get("rateLimitReachedType") or bucket.get("spendControlReached"):
      return 0
    windows = [bucket.get(k) for k in ("primary", "secondary")]
    percent, reset = "usedPercent", "resetsAt"
  else:
    windows = [data.get(k) for k in ("five_hour", "seven_day", "spend_limit")]
    percent, reset = "used_percentage", "resets_at"
  values = []
  for window in windows:
    if not isinstance(window, dict) or not valid_percent(window.get(percent)):
      continue
    resets_at = window.get(reset)
    if (type(resets_at) in (int, float) and resets_at <= time.time()
        and sample.get("observed_at", 0) <= resets_at):
      return None
    values.append(100 - window[percent])
  return min(values) if values else None


def recommendation(session_id: str) -> dict[str, Any]:
  """Recommend delegation from cached evidence only; hooks must not start network queries."""
  state = st.session(session_id)
  cfg = st.config()
  policy = {**DEFAULTS, **cfg.get("routing", {})}
  claude = read_claude(session_id)
  codex = st.read("quota-codex.json", {"status": "unknown"})
  if time.time() - codex.get("observed_at", 0) > 300:
    codex = dict(codex, status="stale" if codex.get("observed_at") else "unknown")
  cr, xr = remaining(claude, "claude"), remaining(codex, "codex")
  pool = state.get("agents")
  external = []
  for name, agent in cfg["agents"].items():
    provider = cfg["providers"][agent["provider"]]
    if not (agent["enabled"] and agent["automatic"] and provider["enabled"]):
      continue
    if pool is not None and name not in pool:
      continue
    if provider["kind"] == "claude":
      continue
    if provider["kind"] == "anthropic":
      sample = st.read("quota-" + st.key(agent["provider"]) + ".json", {})
      if (sample.get("status") == "known" and time.time() - sample.get("observed_at", 0) < 300
          and sample.get("data", {}).get("is_available") is False):
        continue
    if provider["kind"] == "codex" and xr is not None and xr <= cfg["quota_reserve_percent"]:
      continue
    external.append(name)
  reason = "Normal capacity; delegate when task size and specialization justify startup overhead."
  mode = "balanced"
  if state["mode"] != "auto":
    mode, reason = "disabled", "Session delegation is off/manual; no automatic dispatch."
  elif not external:
    mode, reason = "no-external-capacity", "No allowed external role with available quota; do not expand the pool."
  elif cr is not None and cr <= policy["claude_critical_remaining_percent"]:
    mode, reason = "handoff", "Claude reserve is critical. Hand off complete bounded stages; keep only coordination and acceptance in Claude."
  elif cr is not None and cr <= policy["claude_low_remaining_percent"]:
    mode, reason = "external-preferred", "Claude quota is low. Delegate most independent implementation, investigation and validation work."
  elif state["strategy"] == "preserve-claude":
    mode, reason = "external-preferred", "User selected preserve-claude; delegate substantial independent work."
  elif cr is None and policy["prefer_external_when_unknown"]:
    mode, reason = "external-preferred", "Claude quota is unknown/stale. Conservatively preserve main-session capacity, without claiming it is exhausted."
  elif state["strategy"] == "balanced" and cr is not None and xr is not None and xr - cr >= 20:
    mode, reason = "external-preferred", "Claude has less relative headroom than Codex; prefer suitable external roles."
  return {"mode": mode, "reason": reason, "claude_remaining_percent": cr, "codex_remaining_percent": xr,
          "claude_quota": claude, "codex_quota_status": codex.get("status"), "external_agents": external,
          "limits": policy, "scope": "current-session", "enforcement": "routing advice; explicit user selections prevail"}


def report(session_id: str, claude_remaining_percent: float | None = None,
           ttl_seconds: int = 900) -> dict[str, Any]:
  """Read routing, or record a user-provided /usage value when first-party fields are absent."""
  if claude_remaining_percent is not None:
    if not valid_percent(claude_remaining_percent) or type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 3600:
      raise ValueError("User-reported remaining must be 0..100; TTL must be 60..3600 seconds")
    st.write("claude-quota-" + st.key(session_id) + ".json", {
      "status": "known", "session_id": session_id, "source": "user-reported", "observed_at": time.time(),
      "ttl_seconds": ttl_seconds, "data": {"five_hour": {"used_percentage": 100 - claude_remaining_percent}}})
  return recommendation(session_id)


def notice(session_id: str) -> tuple[str, str]:
  """Provide concise, deduplicatable routing context for Claude's next decision."""
  value = recommendation(session_id)
  signature = json.dumps({k: value[k] for k in ("mode", "claude_remaining_percent", "codex_remaining_percent", "external_agents")})
  text = ("Quota-aware delegation: " + signature + ". " + value["reason"] +
          " Query delegate_budget for evidence. When external-preferred/handoff, delegate whole independent stages, "
          "not only reviews; avoid doing the same work twice in main. Preserve acceptance checks. "
          "Use available concurrency only for independent tasks, choose model and effort for expected total cost, "
          "and batch related subtasks to amortize context overhead. Do not create Claude-native subagents to save Claude quota: "
          "they consume the same account. Continue to honor the session pool and default-off boundary.")
  return signature, text


if __name__ == "__main__":
  os.umask(0o077)
  refresh_cli()

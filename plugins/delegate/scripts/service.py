"""Session controls, provider catalog, and accountable background task dispatch."""
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import copy
import json
import hashlib
import os
import subprocess
import sys
import time
import uuid

import storage as st
from codex_rpc import CodexRPC, models
from lifecycle import ACTIVE, identity, reconcile, cancel_orphan, group_running
import budget
import thread_cleanup
import credentials
import accounting
import optimizer
import registration
import commits
import runtime



def catalog(provider_name: str = "codex", refresh: bool = False) -> dict[str, Any]:
  """Return supported models/efforts, caching successful Codex discovery for five minutes."""
  provider = st.config()["providers"].get(provider_name)
  if not provider:
    raise ValueError("Unknown provider")
  if provider["kind"] == "anthropic" and provider.get("models_url"):
    credentials.environment(provider)
    cache_name = "catalog-" + st.key(provider_name) + ".json"
    cached = st.read(cache_name)
    if not refresh and cached and time.time() - cached["observed_at"] < 300:
      return cached
    try:
      response = credentials.get_json(provider, provider["models_url"])
      rows = [r for r in response.get("data", []) if r.get("id") in provider.get("models", [])]
      if not rows:
        raise ValueError("Provider catalog does not advertise any configured model")
      value = {"provider": provider_name, "source": "provider models endpoint", "observed_at": time.time(),
               "enabled": provider["enabled"], "credential_present": True, "models": [r["id"] for r in rows],
               "efforts": provider.get("efforts", []), "model_details": rows}
    except ValueError as exc:
      value = {"provider": provider_name, "source": "configured allowlist; discovery unavailable",
               "observed_at": time.time(), "enabled": provider["enabled"], "credential_present": True,
               "models": provider.get("models", []), "efforts": provider.get("efforts", []),
               "discovery_error": str(exc), "note": "Metadata errors do not establish inference failure"}
    st.write(cache_name, value)
    return value
  if provider["kind"] != "codex":
    return {"provider": provider_name, "source": "configured allowlist; inference availability checked separately",
            "enabled": provider["enabled"], "models": provider.get("models", []),
            "efforts": provider.get("efforts", []),
            "credential_present": credentials.present(provider)
            if provider["kind"] == "anthropic" else None}
  cache_name = "catalog-" + st.key(provider_name) + ".json"
  cached = st.read(cache_name)
  if not refresh and cached and time.time() - cached["observed_at"] < 300:
    return cached
  value = {"provider": provider_name, "observed_at": time.time(), "source": "codex model/list",
           "models": models(provider["command"])}
  st.write(cache_name, value)
  return value


def model_cooldown(provider_name: str, model: str) -> dict[str, Any] | None:
  """Keep transient model capacity separate from account quota refreshes."""
  value = st.read("cooldown-" + st.key(provider_name) + "-" + hashlib.sha256(model.encode()).hexdigest() + ".json")
  return value if value and value.get("expires_at", 0) > time.time() else None


def quota(provider_name: str = "codex", refresh: bool = False, session_id: str = "") -> dict[str, Any]:
  """Read quota without inference; unknown values never become zero usage."""
  provider = st.config()["providers"].get(provider_name)
  if not provider:
    raise ValueError("Unknown provider")
  if provider["kind"] == "claude":
    if refresh or budget.read_claude(session_id).get("status") in {"unknown", "stale"}:
      budget.refresh_cli(force=refresh)
    return budget.read_claude(session_id)
  cache_name = "quota-" + st.key(provider_name) + ".json"
  cached = st.read(cache_name)
  if not refresh and cached and time.time() - cached["observed_at"] < 60:
    return cached
  try:
    if provider["kind"] == "codex":
      with CodexRPC(provider["command"]) as rpc:
        account = rpc.call("account/read", {"refreshToken": False}).get("account")
        if not account or account.get("type") != "chatgpt":
          raise ValueError("Codex must be logged in with ChatGPT; API fallback is disabled")
        raw = rpc.call("account/rateLimits/read")
        raw.pop("accountId", None)
    elif provider["kind"] == "anthropic" and provider.get("balance_url"):
      raw = credentials.get_json(provider, provider["balance_url"])
    else:
      raise ValueError("This provider has no configured balance endpoint")
    value = {"status": "known", "observed_at": time.time(), "provider": provider_name, "data": raw}
  except Exception as exc:
    value = {"status": "unknown", "observed_at": time.time(), "provider": provider_name,
             "reason": str(exc)}
  st.write(cache_name, value)
  return value


def control(session_id: str, action: str = "status", agents: list[str] | None = None,
            strategy: str | None = None) -> dict[str, Any]:
  """Change only one session; off never cancels existing tasks."""
  if action not in {"status", "on", "off", "manual"}:
    raise ValueError("action must be status/on/off/manual")
  if strategy is not None and strategy not in {"balanced", "preserve-claude", "preserve-parent", "quality-first"}:
    raise ValueError("Unknown strategy")
  cfg = st.config()
  if agents is not None and any(name not in cfg["agents"] for name in agents):
    raise ValueError("Unknown agent in allowed pool")
  with st.locked():
    state = st.session(session_id)
    if action != "status":
      state["mode"] = "auto" if action == "on" else action
      if agents is not None:
        state["agents"] = agents
      if strategy is not None:
        state["strategy"] = strategy
      st.save_session(session_id, state)
  return {"session_id": session_id, **state, "configured_agents": cfg["agents"],
          "routing": ({"mode":"host-aware planning", "parent_provider":state.get("parent_provider") or "unknown"}
            if state.get("host_kind") in {"codex", "dsh"} else budget.recommendation(session_id)),
          "configuration": str(st.root() / "config.json"), "default_for_new_sessions": cfg.get("default_mode", "off")}


def validate_config(cfg: dict[str, Any]) -> None:
  """Validate config before committing any change; never store credential values."""
  mode = cfg.get("default_mode", "off")
  if not isinstance(mode, str) or mode not in {"off", "manual", "auto"}:
    raise ValueError("default_mode must be off/manual/auto")
  for field, upper in [("max_concurrency", 16), ("max_session_jobs", 1000), ("max_timeout_seconds", 14400)]:
    if type(cfg.get(field)) is not int or not 1 <= cfg[field] <= upper:
      raise ValueError("Invalid " + field)
  if not 0 <= cfg.get("quota_reserve_percent", -1) < 100:
    raise ValueError("Invalid quota reserve")
  routing = {**budget.DEFAULTS, **cfg.get("routing", {})}
  if set(routing) - set(budget.DEFAULTS) or type(routing["prefer_external_when_unknown"]) is not bool:
    raise ValueError("Invalid routing policy")
  low, critical = routing["claude_low_remaining_percent"], routing["claude_critical_remaining_percent"]
  if not budget.valid_percent(low) or not budget.valid_percent(critical) or not critical <= low:
    raise ValueError("Invalid Claude routing thresholds")
  for name, provider in cfg["providers"].items():
    st.key(name)
    allowed = {"kind", "enabled", "command", "models", "efforts", "base_url", "api_key_env", "balance_url",
               "models_url", "bridge_account", "bridge_state_dir"}
    if set(provider) - allowed:
      raise ValueError("Unknown provider fields; secrets must use api_key_env")
    if provider.get("kind") not in {"codex", "claude", "anthropic"}:
      raise ValueError("Supported provider kinds: codex, claude, anthropic")
    if type(provider.get("enabled")) is not bool or not isinstance(provider.get("command"), str):
      raise ValueError("Provider needs enabled and command")
    for field in ("models", "efforts"):
      if field in provider and (not isinstance(provider[field], list)
                                or any(not isinstance(item, str) for item in provider[field])):
        raise ValueError(field + " must be a string list")
    if provider["kind"] == "anthropic":
      if provider.get("bridge_account"):
        if provider.get("api_key_env") or not provider.get("bridge_state_dir"):
          raise ValueError("Bridge authentication must have a state directory and cannot mix auth sources")
        credentials.bridge(provider)
      else:
        env_name = provider.get("api_key_env", "")
        st.key(env_name)
        if not env_name.isidentifier():
          raise ValueError("Invalid credential environment variable name")
      for field in ("base_url", "balance_url", "models_url"):
        if field in provider:
          url = urlparse(provider[field])
          schemes = {"https", "http"} if provider.get("bridge_account") else {"https"}
          if url.scheme not in schemes or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError(field + " must be an HTTPS URL without embedded credentials")
          base = urlparse(provider["base_url"])
          if field != "base_url" and (url.scheme, url.netloc) != (base.scheme, base.netloc):
            raise ValueError("Metadata endpoint must use the provider's pinned origin")
      if not provider.get("base_url"):
        raise ValueError("base_url is required")
    if provider["kind"] != "codex" and provider["enabled"]:
      if not provider.get("models") or not provider.get("efforts"):
        raise ValueError("Enabled provider needs model and effort allowlists")
      if set(provider["efforts"]) - {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported Claude CLI effort")
  for name, agent in cfg["agents"].items():
    st.key(name)
    if set(agent) - {"provider", "enabled", "automatic", "workspace", "instructions", "models", "efforts"}:
      raise ValueError("Unknown agent field")
    if agent.get("provider") not in cfg["providers"]:
      raise ValueError("Unknown agent provider")
    if agent.get("workspace") not in {"read-only", "worktree"}:
      raise ValueError("Workspace must be read-only or worktree")
    if any(type(agent.get(field)) is not bool for field in ("enabled", "automatic")):
      raise ValueError("Agent enabled/automatic must be boolean")
    if not isinstance(agent.get("instructions"), str):
      raise ValueError("Agent instructions must be a string")
    for field in ("models", "efforts"):
      if field in agent and (not isinstance(agent[field], list)
                             or any(not isinstance(item, str) for item in agent[field])):
        raise ValueError("Agent " + field + " must be a string list")


def configure(section: str = "show", name: str = "", value: dict[str, Any] | None = None) -> dict[str, Any]:
  """Upsert a role/provider or limits only when the user requests configuration."""
  with st.locked():
    cfg = st.config()
    if section == "show":
      return cfg
    if section in {"agents", "providers"}:
      st.key(name)
      if not isinstance(value, dict):
        raise ValueError("value must be an object")
      cfg[section][name] = {**cfg[section].get(name, {}), **value}
    elif section == "limits" and isinstance(value, dict):
      if set(value) - {"max_concurrency", "max_session_jobs", "max_timeout_seconds", "quota_reserve_percent"}:
        raise ValueError("Unknown limit")
      cfg.update(value)
    elif section == "defaults" and isinstance(value, dict):
      if set(value) != {"default_mode"}:
        raise ValueError("defaults requires exactly default_mode")
      cfg.update(value)
    elif section == "routing" and isinstance(value, dict):
      cfg["routing"] = {**cfg.get("routing", {}), **value}
    else:
      raise ValueError("section must be show/agents/providers/limits/routing/defaults")
    validate_config(cfg)
    st.write("config.json", cfg)
  return cfg


def choose(agent: dict[str, Any], provider: dict[str, Any], provider_name: str,
           model: str, effort: str) -> None:
  """Require an explicit, supported model-effort pair for every task."""
  for field, selected in (("models", model), ("efforts", effort)):
    if field in agent and selected not in agent[field]:
      raise ValueError("Choice is outside this agent's " + field + " allowlist")
  data = catalog(provider_name)
  if provider["kind"] == "codex":
    row = next((row for row in data["models"] if row.get("model") == model or row.get("id") == model), None)
    if row is None:
      raise ValueError("Model is absent from live Codex catalog; refresh catalog")
    allowed = [entry["reasoningEffort"] for entry in row.get("supportedReasoningEfforts", [])]
    if effort not in allowed:
      raise ValueError("Unsupported effort; available: " + ", ".join(allowed))
  elif model not in data["models"] or effort not in data["efforts"]:
    raise ValueError("Model/effort is outside provider allowlist")
  elif data.get("model_details"):
    row = next(r for r in data["model_details"] if r["id"] == model)
    levels = row.get("effort", {}).get("supported_levels")
    if levels and effort not in levels:
      raise ValueError("Provider model does not support this effort: " + ", ".join(levels))


def summary(value: dict[str, Any], result: bool = False) -> dict[str, Any]:
  """Return compact public job information without repeatedly sending task context."""
  fields = ("id", "session_id", "agent", "provider_name", "model", "effort", "rationale", "status",
            "task_id", "work_item", "plan_id", "complexity", "category",
            "created_at", "finished_at", "cwd", "source_cwd", "thread_id", "turn_id", "error", "result",
            "usage", "usage_delta", "model_usage", "acceptance", "review", "review_evidence", "messages", "last_event", "resume_job", "cancel_requested", "codex_session", "cli_session", "allow_commit", "git_commit")
  output = {field: value[field] for field in fields if field in value and (field != "result" or result)}
  output["runtime"] = runtime.observe(value)
  output["record_path"] = str(st.root() / ("job-" + value["id"] + ".json"))
  return output


def check_quota(value: dict[str, Any], reserve: float) -> None:
  """Block observed exhausted/reserved Codex quota; missing data stays unknown."""
  if value.get("status") != "known":
    return
  data = value["data"]
  if data.get("ordinaryUsageAllowed") is False:
    raise ValueError("Ordinary Codex usage is unavailable; no paid fallback will be used")
  bucket = data.get("rateLimits", {})
  if bucket.get("spendControlReached") or bucket.get("rateLimitReachedType"):
    raise ValueError("Codex reported a reached usage limit")
  for window in (bucket.get("primary"), bucket.get("secondary")):
    if window and window.get("usedPercent", 0) >= 100 - reserve:
      raise ValueError("Codex quota reserve reached; choose another allowed provider or wait for reset")


def run(session_id: str, agent: str, model: str, effort: str, task: str, acceptance: str,
        rationale: str, cwd: str, context: str = "", one_shot: bool = False,
        timeout_seconds: int = 900, resume_job: str | None = None, work_item: str = "",
        plan_id: str = "", category: str = "", complexity: str = "", operation: str = "read",
        estimated_main_tokens: int = 0, wait_seconds: int = 0,
        await_completion: bool = True, allow_commit: bool = False,
        commit_message: str = "", commit_paths: list[str] | None = None) -> dict[str, Any]:
  """Dispatch under the resume guard, then wait without blocking worker archival."""
  if type(wait_seconds) is not int or not 0 <= wait_seconds <= 50:
    raise ValueError("wait_seconds must be 0..50")
  arguments = locals().copy()
  arguments.pop("wait_seconds")
  receipt = _dispatch(**arguments)
  if wait_seconds and receipt.get("id") and "action" not in receipt:
    return jobs(session_id, "result", receipt["id"], wait_seconds=wait_seconds)
  return receipt


@thread_cleanup.guard_resume
def _dispatch(session_id: str, agent: str, model: str, effort: str, task: str, acceptance: str,
        rationale: str, cwd: str, context: str = "", one_shot: bool = False,
        timeout_seconds: int = 900, resume_job: str | None = None, work_item: str = "",
        plan_id: str = "", category: str = "", complexity: str = "", operation: str = "read",
        estimated_main_tokens: int = 0, await_completion: bool = True, allow_commit: bool = False,
        commit_message: str = "", commit_paths: list[str] | None = None) -> dict[str, Any]:
  """Validate authorization and selection, then start a detached bounded worker."""
  st.key(session_id)
  if type(one_shot) is not bool or type(await_completion) is not bool:
    raise ValueError("one_shot and await_completion must be booleans")
  cfg = st.config()
  if complexity and complexity not in {"trivial", "simple", "standard", "complex"}:
    raise ValueError("Invalid complexity")
  if agent == "auto":
    if model != "auto" or effort != "auto" or plan_id:
      raise ValueError("Automatic selection uses agent/model/effort=auto without a preselected plan")
    if st.session(session_id)["mode"] != "auto":
      raise ValueError("Automatic selection requires session opt-in; use explicit role/model for a one-shot request")
    if complexity and complexity not in {"trivial", "simple", "standard", "complex"}:
      raise ValueError("Invalid complexity")
    state = st.session(session_id)
    root_task = accounting.load(state["active_task"]) if state.get("active_task") else accounting.begin(session_id)
    with st.locked():
      root_task = accounting.load(root_task["id"])
      if not root_task["jobs"]:
        root_task["label"] = task.splitlines()[0][:120] if task.strip() else root_task["label"]
        accounting.save(root_task)
    work_item = work_item or (st.job(resume_job).get("work_item", "main") if resume_job else "main")
    decision = optimizer.plan(session_id, work_item, operation, estimated_main_tokens, len(context),
                              complexity=complexity, category=category)
    if decision["action"] != "delegate":
      return decision
    agent, model, effort, plan_id = decision["agent"], decision["model"], decision["effort"], decision["id"]
    rationale = rationale + " | " + decision["reason"]
  role = cfg["agents"].get(agent)
  if not role or not role["enabled"]:
    raise ValueError("Agent is missing or disabled")
  commits.validate(allow_commit, commit_message, commit_paths, role["workspace"])
  provider_name = role["provider"]
  provider = cfg["providers"][provider_name]
  if not provider["enabled"]:
    raise ValueError("Provider is disabled")
  if any(not isinstance(s, str) or not s.strip() for s in (model, effort, task, acceptance, rationale)):
    raise ValueError("model, effort, task, acceptance and selection rationale are required")
  if len(task) + len(context) + len(acceptance) > 100000:
    raise ValueError("Task packet exceeds 100000 characters; pass focused context")
  workdir = Path(cwd).resolve(strict=True)
  if not workdir.is_dir():
    raise ValueError("cwd must be a directory")
  runtime.validate(timeout_seconds, cfg["max_timeout_seconds"])

  def authorize(state: dict[str, Any]) -> None:
    if not one_shot and (state["mode"] != "auto" or not role["automatic"]):
      raise ValueError("Delegation is off/manual or this role is manual-only; explicit one-shot request required")
    if state["agents"] is not None and agent not in state["agents"]:
      raise ValueError("Agent is outside this session's allowed pool")

  authorize(st.session(session_id))
  if model_cooldown(provider_name, model):
    raise ValueError("Model is temporarily at capacity; wait for the 60-second cooldown or choose another allowed model")
  choose(role, provider, provider_name, model, effort)
  quota_snapshot = quota(provider_name, session_id=session_id)
  if quota_snapshot.get("status") == "limited" and quota_snapshot.get("model") == model:
    raise ValueError("Provider recently reported this model's limit: " + str(quota_snapshot.get("reason")))
  if provider["kind"] == "codex":
    check_quota(quota_snapshot, cfg["quota_reserve_percent"])
  if provider["kind"] == "anthropic":
    credentials.environment(provider)
    if quota_snapshot.get("status") == "known" and quota_snapshot.get("data", {}).get("is_available") is False:
      raise ValueError("Provider reports insufficient API balance")
  previous = st.job(resume_job) if resume_job else None
  usage_baseline = {}
  if previous:
    if previous["session_id"] != session_id or previous["agent"] != agent:
      raise ValueError("Resume must use the same session and role")
    previous = reconcile(resume_job)
    if previous.get("backend_starting") or group_running(previous.get("backend_identity")):
      raise ValueError("Previous backend cleanup is unconfirmed; resume is blocked")
    if previous["status"] in ACTIVE or not previous.get("finished_at") or not previous.get("thread_id"):
      raise ValueError("Resume requires a finished job with a backend session ID")
    if previous["provider_name"] != provider_name or previous["role"] != role or previous["provider"] != provider:
      raise ValueError("Role changed; create a new task with explicit handoff")
    if str(workdir) != previous["source_cwd"]:
      raise ValueError("Resume cwd must match original source_cwd")
    # A manual cleanup can orphan an older attempt no longer in active.json.
    # The per-thread guard spans this check and registration of the new job.
    siblings = []
    for sibling_id in st.session(session_id)["jobs"]:
      sibling = st.job(sibling_id)
      if sibling.get("thread_id") != previous["thread_id"]:
        continue
      siblings.append(sibling)
      sibling = reconcile(sibling_id)
      if sibling["status"] in ACTIVE or sibling.get("backend_starting") or group_running(sibling.get("backend_identity")):
        raise ValueError("This backend thread already has an active job or unconfirmed cleanup")
    latest = siblings[-1]  # Session registration order is stable; cleanup can rewrite finished_at.
    usage_baseline = (latest.get("usage") or {}).get("total")
  job_id = "j" + uuid.uuid4().hex[:16]
  parent_state = st.session(session_id)
  parent_task = accounting.load(parent_state["active_task"]) if parent_state.get("active_task") else accounting.begin(session_id)
  work_item = work_item or (previous.get("work_item", "main") if previous else "main")
  st.key(work_item)
  classification = st.read("plan-" + st.key(plan_id) + ".json", {}) if plan_id else {}
  job_complexity = complexity or classification.get("complexity") or (previous or {}).get("complexity") or parent_task["complexity"]
  job_category = category or classification.get("category") or (previous or {}).get("category") or parent_task["category"]
  value = {"allow_commit": allow_commit, "commit_message": commit_message, "commit_paths": commit_paths,
           "worktree": previous.get("worktree") if previous else None, "complexity": job_complexity, "category": job_category[:80], "id": job_id, "session_id": session_id, "await_completion": await_completion, "agent": agent, "role": copy.deepcopy(role),
           "provider_name": provider_name, "provider": copy.deepcopy(provider), "model": model, "effort": effort,
           "task": task, "context": context, "acceptance": acceptance, "rationale": rationale,
           "source_cwd": str(workdir), "cwd": previous["cwd"] if previous else str(workdir),
           "timeout_seconds": timeout_seconds, "runtime_control_version": 1, "created_at": time.time(), "status": "queued",
           "quota_before": quota_snapshot, "cancel_requested": False, "messages": [], "review": "pending",
           "task_id": parent_task["id"], "work_item": work_item, "plan_id": plan_id or None,
           "resume_job": resume_job, "thread_id": previous["thread_id"] if previous else None,
           "backend_config_dir": previous.get("backend_config_dir") if previous else None,
           "usage_before": usage_baseline}
  for active_id in st.read("active.json", []):
    reconcile(active_id)
  with st.locked():
    registration.recover_locked()
    state = st.session(session_id)
    authorize(state)
    active = [j for j in st.read("active.json", []) if st.job(j)["status"] in ACTIVE]
    if len(active) >= cfg["max_concurrency"]:
      raise ValueError("Global concurrency limit reached; collect existing work first")
    if len(state["jobs"]) >= cfg["max_session_jobs"]:
      raise ValueError("Session job limit reached; change the limit explicitly to continue")
    if previous and any(st.job(j).get("thread_id") == previous["thread_id"] for j in active):
      raise ValueError("This backend thread already has an active job")
    optimizer.validate_locked(parent_task, value, plan_id)
    st.write("active.json", active)
    registration.commit_locked(value)
  try:
    child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("worker.py")), job_id],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    st.update_job(job_id, worker_pid=child.pid, worker_identity=identity(child.pid))
  except Exception as exc:
    st.update_job(job_id, status="failed", error=str(exc), finished_at=time.time())
    raise
  return summary(st.job(job_id))


def jobs(session_id: str, action: str = "list", job_id: str = "", message: str = "",
         wait_seconds: int = 0, verdict: str = "", evidence: str = "", detail: bool = False,
         timeout_seconds: int | None = None) -> Any:
  """List, inspect, interrupt, steer, or record parent verification of a task."""
  state = st.session(session_id)
  if action == "list":
    return [summary(reconcile(j)) for j in state["jobs"][-50:]]
  value = reconcile(job_id)
  if value["session_id"] != session_id:
    raise ValueError("Job belongs to another session")
  if action == "runtime":
    if timeout_seconds is not None:
      runtime.validate(timeout_seconds, st.config()["max_timeout_seconds"])
      with st.locked():
        current = st.job(job_id)
        if current.get("runtime_control_version") != 1:
          raise ValueError("Worker predates live runtime controls; restart Claude and launch/resume with the new version")
        if current["status"] not in {"queued", "running"} or current.get("cancel_requested"):
          raise ValueError("Runtime changes require an active non-cancelling execution")
        current["timeout_seconds"] = timeout_seconds
        current["execution_deadline"] = (current["execution_started_at"] + timeout_seconds
          if current.get("execution_started_at") and timeout_seconds else None)
        current["runtime_changed_at"] = time.time()
        st.write("job-" + job_id + ".json", current)
      value = current
    return summary(value)
  if action == "detach":
    return summary(st.update_job(job_id, await_completion=False))
  if action == "archive":
    thread_cleanup.archive(job_id)
    return summary(st.job(job_id), result=True)
  if action == "result":
    if not 0 <= wait_seconds <= 50:
      raise ValueError("wait_seconds must be 0..50")
    deadline = time.monotonic() + wait_seconds
    while value["status"] in ACTIVE and value["status"] != "orphaned" and time.monotonic() < deadline:
      time.sleep(min(1, max(0, deadline - time.monotonic())))
      value = reconcile(job_id)
    if value["status"] not in ACTIVE or value["status"] == "orphaned":
      st.update_job(job_id, collected_at=time.time())
    elif wait_seconds:
      with st.locked():
        current = st.job(job_id)
        current["wait_count"] = current.get("wait_count", 0) + 1
        st.write("job-" + job_id + ".json", current)
    result = summary(value, result=True)
    if not detail:
      usage = accounting.job_usage(value)
      result["observed_tokens"] = accounting.totals([usage]) if usage else None
      for field in ("usage", "usage_delta", "model_usage", "rationale", "acceptance", "messages"):
        result.pop(field, None)
      text = result.get("result") or ""
      if len(text) > 8000:
        result["result"] = text[:8000]
        result["result_truncated"] = True
    return result
  if action == "cancel":
    if value["status"] == "orphaned":
      return summary(cancel_orphan(job_id))
    if value["status"] not in ACTIVE:
      return summary(value)
    return summary(st.update_job(job_id, cancel_requested=True))
  if action == "tell":
    if not message.strip() or len(message) > 20000:
      raise ValueError("Message must contain 1..20000 characters")
    if value["provider"]["kind"] != "codex":
      raise ValueError("This backend cannot steer live; collect the result then resume with the instruction")
    with st.locked():
      value = st.job(job_id)
      if value["status"] not in ACTIVE:
        raise ValueError("Job ended; use delegate_run with resume_job")
      value["messages"].append({"text": message, "status": "queued", "created_at": time.time()})
      st.write("job-" + job_id + ".json", value)
    return summary(value)
  if action == "verify":
    if value["status"] in ACTIVE or verdict not in {"accepted", "needs-work", "blocked"} or not evidence.strip():
      raise ValueError("Verify requires a finished job, verdict, and concrete evidence")
    return summary(st.update_job(job_id, review=verdict, review_evidence=evidence), result=True)
  raise ValueError("Unknown job action")

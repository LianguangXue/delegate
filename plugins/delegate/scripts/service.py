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
import runtime_image
import attempts as at
import math
import capabilities
import source_inputs as drafts
import result_delivery
import completion
import routing_policy as policy
import input_contract
import persistent_tasks



def catalog(provider_name: str = "codex", refresh: bool = False, agent_name: str = "") -> dict[str, Any]:
  """Discover role names alongside each provider's model and effort catalog."""
  cfg = st.config()
  if agent_name:
    if agent_name not in cfg["agents"]:
      raise ValueError("Unknown agent_name; catalog agents lists exact configured role names")
    provider_name = cfg["agents"][agent_name]["provider"]
  value = _catalog(provider_name, refresh)
  agents = [{"name": name, **role} for name, role in cfg["agents"].items()]
  return {**value, "agents": agents, "selected_provider_agents": [r for r in agents if r["provider"] == provider_name],
          "catalog_scope": "models/efforts belong to the selected provider; agents lists all configured roles",
          "configured_providers": [{"name": name, "enabled": p["enabled"],
            "models": p.get("models", []), "efforts": p.get("efforts", [])} for name, p in cfg["providers"].items()],
          "timeout_seconds": runtime.bounds(cfg["max_timeout_seconds"])}


def _catalog(provider_name: str, refresh: bool) -> dict[str, Any]:
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
      state["mode_revision"] = state.get("mode_revision", 0) + 1
      if agents is not None:
        state["agents"] = agents
      if strategy is not None:
        state["strategy"] = strategy
      st.save_session(session_id, state)
  return {"session_id": session_id, **state, "configured_agents": cfg["agents"],
          "server_runtime": {"scripts": str(Path(__file__).resolve().parent),
            "version": runtime_image.VERSION,
            "frozen": (Path(__file__).resolve().parent / "runtime-image.json").is_file(),
            "legacy_session_fallback": "delegate-agent call with JSON name/arguments and the same session_id"},
          "worktree_timeout_seconds": cfg.get("worktree_timeout_seconds", 600),
          "timeout_seconds": runtime.bounds(cfg["max_timeout_seconds"]),
          "execution_profiles": {"default": "sandboxed", "available": sorted(capabilities.PROFILES),
            "authorization": "Per-role execution_profiles; host requires explicit selection and a worktree role. No automatic escalation."},
          "model_policy": {"preferences": policy.PREFERENCES, "max_auto_effort": policy.MAX_AUTO_EFFORT,
            "selection": policy.SELECTION_POLICY,
            "assessment": "Parent reasons once using task_profile; no classifier model call. Explicit model/effort remain authoritative."},
          "persistent_tasks": state.get("loops", []),
          "routing": ({"mode":"host-aware planning", "parent_provider":state.get("parent_provider") or "unknown"}
            if state.get("host_kind") in {"codex", "dsh"} else budget.recommendation(session_id)),
          "configuration": str(st.root() / "config.json"), "default_for_new_sessions": cfg.get("default_mode", "off")}


def validate_config(cfg: dict[str, Any]) -> None:
  """Validate config before committing any change; never store credential values."""
  mode = cfg.get("default_mode", "off")
  if not isinstance(mode, str) or mode not in {"off", "manual", "auto"}:
    raise ValueError("default_mode must be off/manual/auto")
  seconds = cfg.get("worktree_timeout_seconds", 600)
  if type(seconds) is not int or not 10 <= seconds <= 86400:
    raise ValueError("worktree_timeout_seconds must be 10..86400 seconds")
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
    if set(agent) - {"provider", "enabled", "automatic", "workspace", "instructions", "models", "efforts", "execution_profiles", "gpu_devices"}:
      raise ValueError("Unknown agent field")
    if agent.get("provider") not in cfg["providers"]:
      raise ValueError("Unknown agent provider")
    if agent.get("workspace") not in {"read-only", "worktree"}:
      raise ValueError("Workspace must be read-only or worktree")
    if any(type(agent.get(field)) is not bool for field in ("enabled", "automatic")):
      raise ValueError("Agent enabled/automatic must be boolean")
    if not isinstance(agent.get("instructions"), str):
      raise ValueError("Agent instructions must be a string")
    profiles = agent.get("execution_profiles", ["sandboxed"])
    if not isinstance(profiles, list) or not profiles or any(not isinstance(p, str) or p not in capabilities.PROFILES for p in profiles):
      raise ValueError("execution_profiles must list sandboxed/network/host")
    if "host" in profiles and agent["workspace"] != "worktree":
      raise ValueError("host execution profile cannot be granted to a read-only role")
    if cfg["providers"][agent["provider"]]["kind"] != "codex" and profiles != ["sandboxed"]:
      raise ValueError("File-tool providers do not support expanded execution profiles")
    if "gpu_devices" in agent:
      capabilities.normalize({"gpu": bool(agent["gpu_devices"]), "gpu_devices": agent["gpu_devices"]})
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
      if set(value) - {"max_concurrency", "max_session_jobs", "max_timeout_seconds", "quota_reserve_percent", "worktree_timeout_seconds"}:
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
            "usage", "usage_delta", "model_usage", "acceptance", "review", "review_evidence", "messages", "last_event", "resume_job", "cancel_requested", "codex_session", "cli_session", "allow_commit", "git_commit", "runtime_image", "timeout_warning", "workspace_setup", "recovery", "await_completion", "delivery_check", "execution_environment", "selection", "source_inputs", "source_snapshot", "task_profile", "task_assessment")
  output = {field: value[field] for field in fields if field in value and (field != "result" or result)}
  output["job_runtime_version"] = runtime_image.job_version(value)
  output["server_runtime_version"] = runtime_image.VERSION
  for field in ("required_files", "output_files", "input_access"):
    if field in value:
      output[field] = value[field]
  if value.get("delivery_check"):
    report = value["delivery_check"]
    output["delivery_check"] = {k: v for k, v in report.items() if k != "findings"}
    output["delivery_check"]["findings"] = report.get("findings", [])[:5]
    output["delivery_check"]["findings_in_record"] = len(report.get("findings", []))
  if value.get("shutdown_observation"):
    observation = value["shutdown_observation"]
    output["shutdown_observation"] = {k: observation[k] for k in (
      "available", "observation_version", "live_child_count", "runtime_helper_count", "workload_or_unknown_count",
      "omitted", "observed_at", "note", "cleanup") if k in observation}
    output["shutdown_observation"]["active_commands"] = observation.get("active_commands", [])[:8]
    output["shutdown_observation"]["processes"] = [
      {k: row.get(k) for k in ("pid", "ppid", "name", "command_line", "command_line_status", "cwd", "arguments_truncated",
                              "executable", "process_kind", "classification_reason")}
      for row in sorted(observation.get("processes", []), key=lambda row: row.get("process_kind") == "runtime_helper")[:8]]
    output["shutdown_observation"]["processes_omitted_from_summary"] = max(0, observation.get("live_child_count", 0) - 8)
    output["shutdown_observation"]["detail_action"] = "delegate_job inspect"
  output["runtime"] = runtime.observe(value)
  output["background"] = not value.get("await_completion", True)
  if value.get("completion_mode") == "notify" or value.get("completion_waiters"):
    output["completion_mode"] = value.get("completion_mode", "wait")
    output["completion"] = completion.describe(value)
  output["attempt"] = at.describe(value)
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
        commit_message: str = "", commit_paths: list[str] | None = None,
        background: bool = False, requirements: dict[str, Any] | None = None,
        execution_profile: str = "auto", source_inputs: list[str] | None = None,
        notify: bool = False, task_profile: dict[str, Any] | None = None,
        required_files: list[str] | None = None, output_files: list[str] | None = None) -> dict[str, Any]:
  """Dispatch under the resume guard, then wait without blocking worker archival."""
  if type(wait_seconds) is not int or not 0 <= wait_seconds <= 50:
    raise ValueError("wait_seconds must be 0..50")
  if type(background) is not bool:
    raise ValueError("background must be a boolean")
  if type(notify) is not bool:
    raise ValueError("notify must be a boolean")
  if background:
    await_completion = False
    wait_seconds = 0
  if notify:
    wait_seconds = 0
  arguments = locals().copy()
  arguments.pop("background")
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
        commit_message: str = "", commit_paths: list[str] | None = None,
        requirements: dict[str, Any] | None = None, execution_profile: str = "auto",
        source_inputs: list[str] | None = None, notify: bool = False,
        task_profile: dict[str, Any] | None = None, required_files: list[str] | None = None,
        output_files: list[str] | None = None) -> dict[str, Any]:
  """Validate authorization and selection, then start a detached bounded worker."""
  st.key(session_id)
  launch = persistent_tasks.context(session_id)
  operation = optimizer.normalize_operation(operation)
  input_paths = drafts.validate(source_inputs)
  profile_supplied = task_profile is not None
  task_profile = policy.normalize(task_profile)
  required_declared, output_declared = required_files is not None, output_files is not None
  required_files = input_contract.normalize(required_files, "required_files")
  output_files = input_contract.normalize(output_files, "output_files")
  if any(not isinstance(s, str) for s in (task, context, acceptance)) or len(task) + len(context) + len(acceptance) > 100000:
    raise ValueError("Task packet must be text up to100000 characters; pass focused context")
  if not required_declared:
    required_files = input_contract.normalize(input_contract.discover(task + "\n" + context + "\n" + acceptance), "required_files")
  workdir = Path(cwd).resolve(strict=True)
  if not workdir.is_dir():
    raise ValueError("cwd must be a directory")
  if complexity == "auto":
    complexity = ""
  if type(one_shot) is not bool or type(await_completion) is not bool:
    raise ValueError("one_shot and await_completion must be booleans")
  cfg = st.config()
  previous = st.job(resume_job) if resume_job else None
  if previous and previous["session_id"] != session_id:
    raise ValueError("Resume must use the same session")
  if previous and agent != "auto" and previous["agent"] != agent:
    raise ValueError("Resume must use the same session and role")
  if previous and task_profile is None:
    task_profile = previous.get("task_profile")
  if previous:
    if not required_declared:
      required_files = input_contract.normalize(previous.get("required_files", []) + required_files, "required_files")
    if not output_declared:
      output_files = input_contract.normalize(previous.get("output_files"), "output_files")
    old_inputs = previous.get("source_inputs", [])
    if source_inputs is not None and input_paths != old_inputs:
      raise ValueError("Resume preserves its existing source_inputs snapshot; use a fresh task to select new inputs")
    input_paths = old_inputs
  requirements_declared = requirements is not None
  if previous:
    prior_execution = capabilities.from_job(previous)
    if requirements is None:
      requirements = prior_execution["requirements"]
    if execution_profile == "auto":
      execution_profile = prior_execution["profile"] if prior_execution["profile"] != "file-tools" else "sandboxed"
  capabilities.normalize(requirements)
  capabilities.validate_profile(execution_profile)
  if complexity and complexity not in {"trivial", "simple", "standard", "complex"}:
    raise ValueError("Invalid complexity")
  if agent == "auto":
    if input_paths and operation != "write":
      raise ValueError("source_inputs requires operation='write' for automatic builder selection")
    if model != "auto" or effort != "auto" or plan_id:
      raise ValueError("Automatic selection uses agent/model/effort=auto without a preselected plan")
    if st.session(session_id)["mode"] != "auto":
      raise ValueError("Automatic selection requires session opt-in; use explicit role/model for a one-shot request")
    if complexity and complexity not in {"trivial", "simple", "standard", "complex"}:
      raise ValueError("Invalid complexity")
    state = st.session(session_id)
    root_task = persistent_tasks.task_for(session_id) or (accounting.load(state["active_task"]) if state.get("active_task") else accounting.begin(session_id))
    with st.locked():
      root_task = accounting.load(root_task["id"])
      if not root_task["jobs"]:
        root_task["label"] = task.splitlines()[0][:120] if task.strip() else root_task["label"]
        accounting.save(root_task)
    work_item = work_item or (st.job(resume_job).get("work_item", "main") if resume_job else "main")
    decision = optimizer.plan(session_id, work_item, operation, estimated_main_tokens, len(context),
                              complexity=complexity, category=category, requirements=requirements,
                              execution_profile=execution_profile, task_profile=task_profile, cwd=str(workdir),
                              required_files=required_files, output_files=output_files)
    if decision["action"] != "delegate":
      return decision
    agent, model, effort, plan_id = decision["agent"], decision["model"], decision["effort"], decision["id"]
    rationale = rationale + " | " + decision["reason"]
  role = cfg["agents"].get(agent)
  if not role or not role["enabled"]:
    names = [name for name, row in cfg["agents"].items() if row["enabled"]]
    raise ValueError("Agent is missing or disabled; enabled agent names: " + ", ".join(names) +
                     ". Use delegate_catalog(provider_name=...) for roles/models/efforts")
  if input_paths and role["workspace"] != "worktree":
    raise ValueError("source_inputs is for isolated worktree builders; read-only reviewers already see current source files")
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
    if launch and agent not in launch["authorized_agents"]:
      raise ValueError("Agent is outside the persistent task's original pool")

  authorize(st.session(session_id))
  selection_state = st.session(session_id)
  selection_task = persistent_tasks.task_for(session_id) or (accounting.load(selection_state["active_task"]) if selection_state.get("active_task") else None)
  item = work_item or (previous or {}).get("work_item", "main")
  attempts = [st.job(j) for j in selection_task["jobs"] if st.job(j).get("work_item", "main") == item] if selection_task else []
  last_attempt = next((j for j in reversed(attempts) if at.model_attempted(j)), None)
  selection_plan = st.read("plan-" + st.key(plan_id) + ".json", {}) if plan_id else {}
  if "required_files" in selection_plan:
    expected_paths = {(workdir / name).resolve() for name in selection_plan["required_files"]}
    current_paths = {(workdir / name).resolve() for name in required_files}
    if ((required_declared and current_paths != expected_paths)
        or (not required_declared and not current_paths <= expected_paths)):
      raise ValueError("required_files differs from its plan; obtain a fresh plan")
    if output_declared and output_files != selection_plan["output_files"]:
      raise ValueError("output_files differs from its plan; obtain a fresh plan")
    required_files, output_files = selection_plan["required_files"], selection_plan["output_files"]
    if selection_plan["cwd"] and str(workdir) != selection_plan["cwd"]:
      raise ValueError("cwd differs from its file-access plan; obtain a fresh plan")
  elif selection_plan and (required_files or output_files):
    raise ValueError("Plan predates file-access checks; obtain a fresh plan")
  if selection_plan.get("assessment"):
    assessment = selection_plan["assessment"]
    if profile_supplied and task_profile != assessment["profile"]:
      raise ValueError("task_profile differs from its plan; obtain a fresh plan")
    if complexity and policy.LEVELS[complexity] > policy.LEVELS[assessment["complexity"]]:
      raise ValueError("complexity exceeds its plan; obtain a fresh plan")
    task_profile = assessment["profile"]
  else:
    if not profile_supplied and attempts and attempts[-1].get("task_profile") is not None:
      task_profile = policy.normalize(attempts[-1]["task_profile"])
    inherited = selection_plan.get("complexity") or (previous or {}).get("complexity") or (
      attempts[-1].get("complexity") if attempts else None) or (selection_task or {}).get("complexity", "standard")
    assessment = policy.assess(task_profile, complexity, inherited)
  complexity = assessment["complexity"]
  if assessment["requires_shell"] and provider["kind"] != "codex":
    raise ValueError("Task assessment requires shell/tests/replay; this selected role has file tools only. Choose an execution role explicitly")
  execution_cwd = previous["cwd"] if previous else str(workdir)
  access = input_contract.check(provider, role["workspace"], execution_cwd, required_files, output_files)
  execution = capabilities.select(role, provider, requirements, execution_profile)
  if previous:
    old = capabilities.from_job(previous)
    execution["declared"] = requirements_declared or old.get("declared", False)
    if execution["profile"] != old["profile"] or execution["requirements"] != old["requirements"]:
      raise ValueError("Resume cannot change execution profile/resource requirements; create a new task with explicit handoff")
  if model_cooldown(provider_name, model):
    raise ValueError("Model is temporarily at capacity; wait for the 60-second cooldown or choose another allowed model")
  selection = None
  if model == "auto" or effort == "auto":
    requested = {"model": model, "effort": effort}
    model, effort = optimizer.role_pair(role, provider, catalog(provider_name), model, effort,
                                        complexity, provider_name, last_attempt)
    selection = {"requested": requested, "model": model, "effort": effort, "complexity": complexity,
                 "model_family": policy.family(model), "assessment": assessment,
                 "scope": "Selected role/provider only; capability tiers are heuristics"}
  choose(role, provider, provider_name, model, effort)
  quota_snapshot = quota(provider_name, session_id=session_id)
  if quota_snapshot.get("status") == "limited" and quota_snapshot.get("model") == model:
    raise ValueError("Provider recently reported this model's limit: " + str(quota_snapshot.get("reason")))
  if provider["kind"] == "codex":
    check_quota(quota_snapshot, cfg["quota_reserve_percent"])
  available = budget.remaining(quota_snapshot, "codex" if provider["kind"] == "codex" else "claude")
  if available is not None and available <= cfg["quota_reserve_percent"]:
    raise ValueError("Known quota is at or below the configured reserve; choose another allowed provider or wait")
  if provider["kind"] == "anthropic":
    credentials.environment(provider)
    if quota_snapshot.get("status") == "known" and quota_snapshot.get("data", {}).get("is_available") is False:
      raise ValueError("Provider reports insufficient API balance")
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
  image = runtime_image.freeze(Path(__file__).resolve().parent)
  parent_state = st.session(session_id)
  parent_task = persistent_tasks.task_for(session_id) or (accounting.load(parent_state["active_task"]) if parent_state.get("active_task") else accounting.begin(session_id))
  work_item = work_item or (previous.get("work_item", "main") if previous else "main")
  st.key(work_item)
  classification = st.read("plan-" + st.key(plan_id) + ".json", {}) if plan_id else {}
  job_complexity = complexity or classification.get("complexity") or (previous or {}).get("complexity") or parent_task["complexity"]
  job_category = category or classification.get("category") or (previous or {}).get("category") or parent_task["category"]
  value = {"execution_environment": execution, "runtime_image": image, "inference_state": "not_started", "worker_wait_supported": previous.get("worker_wait_supported", False) if previous else True,
           "delegate_version": runtime_image.VERSION, "required_files": required_files, "output_files": output_files,
           "input_access": access,
           "allow_commit": allow_commit, "commit_message": commit_message, "commit_paths": commit_paths,
           "worktree": previous.get("worktree") if previous else None, "complexity": job_complexity, "category": job_category[:80], "id": job_id, "session_id": session_id, "await_completion": await_completion, "agent": agent, "role": copy.deepcopy(role),
           "provider_name": provider_name, "provider": copy.deepcopy(provider), "model": model, "effort": effort,
           "task": task, "context": context, "acceptance": acceptance, "rationale": rationale,
           "selection": selection,
           "task_profile": task_profile, "task_assessment": assessment,
           "completion_mode": "notify" if notify else "wait",
           "source_inputs": input_paths, "source_snapshot": previous.get("source_snapshot") if previous else None,
           "source_cwd": str(workdir), "cwd": previous["cwd"] if previous else str(workdir),
           "timeout_seconds": timeout_seconds, "runtime_control_version": 1, "runtime_revision": 0,
           "runtime_ack_expected": True, "created_at": time.time(), "status": "queued",
           "quota_before": quota_snapshot, "cancel_requested": False, "messages": [], "review": "pending",
           "task_id": parent_task["id"], "work_item": work_item, "plan_id": plan_id or None,
           "resume_job": resume_job, "thread_id": previous["thread_id"] if previous else None,
           "backend_config_dir": previous.get("backend_config_dir") if previous else None,
           "usage_before": usage_baseline}
  if launch:
    value.update(loop_id=launch["loop_id"], loop_cycle=launch["cycle"], loop_attempt=launch["attempt"], loop_launch_key=launch["key"])
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
    if launch:
      persistent_tasks.validate_launch_locked(launch, value)
    st.write("active.json", active)
    registration.commit_locked(value)
  try:
    child = subprocess.Popen([sys.executable, str(Path(image["path"]) / "worker.py"), job_id],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    st.update_job(job_id, worker_pid=child.pid, worker_identity=identity(child.pid))
  except Exception as exc:
    st.update_job(job_id, status="failed", error=str(exc), finished_at=time.time())
    raise
  return summary(st.job(job_id))


def jobs(session_id: str, action: str = "list", job_id: str = "", message: str = "",
         wait_seconds: int = 0, verdict: str = "", evidence: str = "", detail: bool = False,
         timeout_seconds: int | None = None, extend_seconds: int | None = None,
         result_offset: int = 0, result_limit: int = result_delivery.DEFAULT_LIMIT,
         result_sha256: str = "", result_view: str = "auto") -> Any:
  """List, inspect, interrupt, steer, or record parent verification of a task."""
  state = st.session(session_id)
  if action == "list":
    return [summary(reconcile(j)) for j in state["jobs"][-50:]]
  value = reconcile(job_id)
  if value["session_id"] != session_id:
    raise ValueError("Job belongs to another session")
  if action == "status":
    return summary(value)
  if action == "watch":
    return summary(st.update_job(job_id, completion_mode="notify"))
  if action == "inspect":
    return {**summary(value), "inspection": runtime.inspect(value)}
  if action in {"runtime", "extend"}:
    maximum = st.config()["max_timeout_seconds"]
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 50:
      raise ValueError("wait_seconds must be 0..50")
    if timeout_seconds is not None and extend_seconds is not None:
      raise ValueError("Choose timeout_seconds (total from start) OR extend_seconds (additional time)")
    if action == "extend" and extend_seconds is None:
      raise ValueError("action=extend requires positive integer extend_seconds")
    if extend_seconds is not None and (type(extend_seconds) is not int or extend_seconds <= 0):
      raise ValueError(f"extend_seconds must be a positive integer. Finite total range: 10..{maximum} seconds; 0 removes the total limit explicitly.")
    if timeout_seconds is not None:
      runtime.validate(timeout_seconds, maximum)
    if timeout_seconds is not None or extend_seconds is not None:
      with st.locked():
        current = st.job(job_id)
        if current.get("runtime_control_version") != 1:
          raise ValueError("Worker predates live runtime controls and cannot safely extend in place. "
            "Do not assume a record edit changes its running code; collect/resume only after it ends. "
            "For new jobs use the installed CLI/new session. " + f"Allowed totals: 0 or 10..{maximum} seconds.")
        if current["status"] not in {"queued", "running"} or current.get("cancel_requested") or current.get("execution_finished_at"):
          raise ValueError("Runtime changes require an active execution before cancellation/teardown begins; an interrupted backend cannot be revived by extending its timer")
        before = current["timeout_seconds"]
        if extend_seconds is not None:
          if before == 0:
            raise ValueError("Job already has timeout_seconds=0 (no total limit); no extension is needed")
          elapsed = math.ceil(max(0, time.time() - current["execution_started_at"])) if current.get("execution_started_at") else 0
          timeout_seconds = max(before, elapsed) + extend_seconds
          runtime.validate(timeout_seconds, maximum)
        current["timeout_seconds"] = timeout_seconds
        current["execution_deadline"] = (current["execution_started_at"] + timeout_seconds
          if current.get("execution_started_at") and timeout_seconds else None)
        current["runtime_revision"] = current.get("runtime_revision", 0) + 1
        current["runtime_changed_at"] = time.time()
        current["runtime_change"] = {"previous_timeout_seconds": before, "timeout_seconds": timeout_seconds,
          "operation": "extend" if extend_seconds is not None else "set_total", "extend_seconds": extend_seconds}
        st.write("job-" + job_id + ".json", current)
      value = current
    requested_revision = value.get("runtime_revision", 0)
    if wait_seconds and (value.get("runtime_ack_expected") or value.get("runtime_ack_version") == 1):
      until = time.monotonic() + wait_seconds
      while time.monotonic() < until:
        applied = value.get("runtime_applied_revision")
        if (value.get("runtime_revision", 0) != requested_revision or value["status"] not in {"queued", "running"}
            or (applied is not None and applied >= requested_revision
                and value.get("runtime_applied_timeout_seconds") == value["timeout_seconds"])):
          break
        time.sleep(min(0.25, max(0, until-time.monotonic())))
        value = st.job(job_id)
    result = summary(value)
    result["runtime"]["allowed_timeout_seconds"] = runtime.bounds(maximum)
    result["runtime"]["last_change"] = value.get("runtime_change")
    result["runtime"]["request_superseded"] = value.get("runtime_revision", 0) != requested_revision
    return result
  if action == "detach":
    return summary(st.update_job(job_id, await_completion=False))
  if action == "archive":
    thread_cleanup.archive(job_id)
    return result_delivery.receipt(st.job(job_id))
  if action == "result":
    if result_view not in {"auto", "page", "summary", "file"}:
      raise ValueError("result_view must be auto/page/summary/file; page explicitly rereads text, file exports the whole report")
    result_delivery.validate(result_offset, result_limit, result_sha256)
    if result_view == "file":
      if result_offset or result_sha256:
        raise ValueError("file view exports the full current result; omit paging arguments")
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 50:
      raise ValueError("wait_seconds must be 0..50")
    deadline = time.monotonic() + wait_seconds
    while value["status"] in ACTIVE and value["status"] != "orphaned" and time.monotonic() < deadline:
      time.sleep(min(1, max(0, deadline - time.monotonic())))
      value = reconcile(job_id)
    if value["status"] in ACTIVE - {"orphaned"} and wait_seconds:
      with st.locked():
        current = st.job(job_id)
        current["wait_count"] = current.get("wait_count", 0) + 1
        st.write("job-" + job_id + ".json", current)
    if result_view == "file":
      if value["status"] in ACTIVE - {"orphaned"}:
        return result_delivery.receipt(value)
      return result_delivery.artifact(session_id, job_id)
    digest, ranges, complete = result_delivery.proof(value)
    if result_view == "summary":
      return result_delivery.receipt(value)
    if result_view == "auto" and not result_offset and not result_sha256 and ranges:
      return result_delivery.receipt(value, unchanged=True)
    result = summary(value, result=True)
    if not detail:
      usage = accounting.job_usage(value)
      result["observed_tokens"] = accounting.totals([usage]) if usage else None
      for field in ("usage", "usage_delta", "model_usage", "rationale", "acceptance", "messages"):
        result.pop(field, None)
    return result_delivery.page(value, result, result_offset, result_limit, result_sha256)
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
    return result_delivery.receipt(st.update_job(job_id, review=verdict, review_evidence=evidence))
  raise ValueError("Unknown job action")

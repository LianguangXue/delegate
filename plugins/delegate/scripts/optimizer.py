"""Measured task routing with conservative bootstrap estimates and explicit quota tradeoffs."""
from typing import Any
import statistics
import time
import uuid

import storage as st
import accounting as ac
import budget
import attempts as at
import capabilities
import routing_policy as policy
import input_contract
import persistent_tasks
from pathlib import Path
from lifecycle import ACTIVE

EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]


def normalize_operation(operation: str) -> str:
  """Keep permission choices explicit, with compatibility for common read-only labels."""
  aliases = {"read-only": "read", "read-only review": "read", "review": "read"}
  if isinstance(operation, str):
    operation = aliases.get(operation, operation)
  if operation not in ("read", "write"):
    raise ValueError("operation must be 'read' (review/analyze without edits) or 'write' (edit in an isolated worktree)")
  return operation


def role_pair(role: dict[str, Any], provider: dict[str, Any], catalog: dict[str, Any],
              model: str, effort: str, complexity: str, provider_name: str,
              previous: dict[str, Any] | None = None) -> tuple[str, str]:
  """Resolve auto within one explicitly selected role, never switch providers or grant permissions."""
  import service
  desired = {"trivial": (0, "low"), "simple": (0, "low"),
             "standard": (1, "medium"), "complex": (2, "high")}[complexity]
  if previous:
    prior_tier = tier(previous["model"])
    prior_effort = EFFORTS.index(previous["effort"]) if previous["effort"] in EFFORTS else 0
    desired = (max(desired[0], min(2, prior_tier + (previous.get("review") == "needs-work"))),
               EFFORTS[min(max(EFFORTS.index(desired[1]), prior_effort + (previous.get("review") == "needs-work")),
                           len(EFFORTS) - 1)])
  rows = catalog["models"] if provider["kind"] == "codex" else [
    next((r for r in catalog.get("model_details", []) if r["id"] == m), {"model": m})
    for m in catalog["models"]]
  options = []
  for row in rows:
    name = row.get("model", row.get("id"))
    if model == "auto" and row.get("hidden"):
      continue
    if model != "auto" and model != name:
      continue
    if role.get("models") is not None and name not in role["models"]:
      continue
    if service.model_cooldown(provider_name, name):
      continue
    level = tier(name, row.get("description", ""))
    if model == "auto" and level < desired[0]:
      continue
    levels = ([r["reasoningEffort"] for r in row.get("supportedReasoningEfforts", [])]
      if provider["kind"] == "codex" else row.get("effort", {}).get("supported_levels", catalog["efforts"]))
    for candidate in levels:
      if candidate not in EFFORTS or (role.get("efforts") is not None and candidate not in role["efforts"]):
        continue
      if effort != "auto" and candidate != effort:
        continue
      if effort == "auto" and EFFORTS.index(candidate) > EFFORTS.index(policy.MAX_AUTO_EFFORT):
        continue
      if effort == "auto" and complexity == "complex" and EFFORTS.index(candidate) < EFFORTS.index("high"):
        continue
      if previous and effort == "auto" and level <= prior_tier and EFFORTS.index(candidate) < prior_effort:
        continue
      options.append((abs(level - desired[0]), policy.preference(name, complexity) if model == "auto" else 0,
                      abs(EFFORTS.index(candidate) - EFFORTS.index(desired[1])), EFFORTS.index(candidate), name, candidate))
  if not options:
    raise ValueError("No allowed model/effort pair satisfies this role and complexity; inspect delegate_catalog(agent_name=...) or revise explicitly")
  chosen = min(options, key=lambda option: option[:4])
  return chosen[4], chosen[5]


def tier(model: str, description: str = "") -> int:
  """Classify known model families for bootstrap selection, not as measured quality ratings."""
  name = (model + " " + description).lower()
  if policy.family(model) == "terra":
    return 0  # Fast workflow lane; not a benchmark of intrinsic model quality.
  if "fable" in model.lower():
    return 2
  if any(word in name for word in ("luna", "haiku", "flash", "mini", "fast and affordable")):
    return 0
  if any(word in name for word in ("astra", "opus", "-pro", "frontier")):
    return 2
  return 1


def pressure(sample: dict[str, Any], provider: str) -> dict[str, Any]:
  """Use all reported windows; imminent reset only breaks close cost ties."""
  remaining = budget.remaining(sample, provider)
  data = sample.get("data") or {}
  windows = [(data.get("rateLimits") or {}).get(k) for k in ("primary", "secondary")] if provider == "codex" else [
    data.get(k) for k in ("five_hour", "seven_day")]
  limiting = [w for w in windows if isinstance(w, dict)
              and remaining is not None
              and w.get("usedPercent", w.get("used_percentage")) == 100 - remaining]
  reset_times = [w.get("resetsAt", w.get("resets_at")) for w in limiting]
  future = [t for t in reset_times if isinstance(t, (int, float)) and t > time.time()]
  hours = (max(future) - time.time()) / 3600 if future else None
  return {"remaining_percent": remaining, "reset_in_hours": round(hours, 2) if hours is not None else None,
          "surplus_priority": (remaining / 100) / (1 + hours) if remaining is not None and hours is not None else 0,
          "quota_status": sample.get("status", "unknown")}


def observations(task: dict[str, Any], agent: str, model: str, effort: str,
                 history: dict[str, Any]) -> dict[str, Any]:
  """Include failed attempts in empirical expected cost; no quality claim from unreviewed jobs."""
  costs, overheads, accepted, attempts, unknown = [], [], 0, 0, 0
  for previous in history.values():
    for job in previous["rows"]:
      if job.get("model_attempted") is False:
        continue
      if (job.get("category", previous["category"]), job.get("complexity", previous["complexity"])) != (task["category"], task["complexity"]):
        continue
      if (job["agent"], job["model"], job["effort"]) != (agent, model, effort):
        continue
      attempts += 1
      unknown += not job.get("quality_known", True)
      if job["tokens"] is not None and previous["parent_share"] is not None:
        costs.append(job["tokens"])
        overheads.append(previous["parent_share"])
      accepted += job["accepted"]
  return {"attempts": attempts, "accepted": accepted, "unreviewed": unknown,
          "cost_samples": len(costs), "smoothed_success_rate": (accepted + 1) / (attempts + 2),
          "mean_attempt_tokens": statistics.mean(costs) if costs else None,
          "mean_parent_share": statistics.mean(overheads) if overheads else None}


def work_state(task: dict[str, Any], work_item: str) -> dict[str, Any] | None:
  """Avoid duplicate work and bound repair loops for one logical work item."""
  jobs = [st.job(j) for j in task["jobs"] if st.job(j).get("work_item", "main") == work_item]
  running = [j["id"] for j in jobs if j["status"] in ACTIVE]
  if running:
    return {"action": "wait", "job_ids": running, "reason": "This work item already has running work"}
  accepted = [j for j in jobs if j.get("review") == "accepted"]
  if accepted and not accepted[-1].get("evidence_checkpoint"):
    return {"action": "replan", "reason": "Accepted result lacks an evidence checkpoint; revalidate before reuse"}
  if accepted and accepted[-1].get("evidence_checkpoint"):
    import inspection
    try:
      check = inspection.inspect(accepted[-1]["session_id"], job_id=accepted[-1]["id"])
    except (ValueError, OSError) as exc:
      return {"action": "replan", "reason": "Evidence inspection unavailable: " + str(exc)}
    if check["blockers"]:
      return {"action": "replan", "reason": "; ".join(check["blockers"])}
  if accepted:
    return {"action": "reuse", "job_id": accepted[-1]["id"], "reason": "Reuse validated output instead of repeating it"}
  counted = [j for j in jobs if at.model_attempted(j)]
  if len(counted) >= task["max_attempts"]:
    return {"action": "replan", "reason": "Work-item attempt limit reached; revisit the approach before spending more"}
  return None


def plan(session_id: str, work_item: str = "main", operation: str = "read",
         estimated_main_tokens: int = 0, context_chars: int = 0, refresh: bool = False,
         complexity: str = "", category: str = "", requirements: dict[str, Any] | None = None,
         execution_profile: str = "auto", task_profile: dict[str, Any] | None = None,
         cwd: str = "", required_files: list[str] | None = None, output_files: list[str] | None = None) -> dict[str, Any]:
  """Recommend main or a concrete role/model/effort; no inference or dispatch occurs here."""
  import service  # Delayed to keep service's execution gates independent.
  needed = capabilities.normalize(requirements)
  capabilities.validate_profile(execution_profile)
  task_profile = policy.normalize(task_profile)
  if complexity == "auto":
    complexity = ""
  shell = policy.assess(task_profile, complexity)["requires_shell"]
  needs_environment = any(needed[k] for k in ("gpu", "ssh", "network")) or execution_profile not in {"auto", "sandboxed"}
  needs_environment = needs_environment or shell
  operation = normalize_operation(operation)
  required_files = input_contract.normalize(required_files, "required_files")
  output_files = input_contract.normalize(output_files, "output_files")
  needs_environment = needs_environment or bool(required_files or output_files)
  if (required_files or output_files) and not cwd:
    raise ValueError("cwd is required for file-access preflight")
  if output_files and operation != "write":
    return {"action": "replan", "reason": "output_files cannot be written by read-only reviewers; choose write with source_inputs or return the report as text"}
  cwd = str(Path(cwd).resolve(strict=True)) if cwd else ""
  if type(estimated_main_tokens) is not int or estimated_main_tokens < 0:
    raise ValueError("estimated_main_tokens must be a non-negative integer")
  if type(context_chars) is not int or not 0 <= context_chars <= 100000:
    raise ValueError("context_chars must be 0..100000")
  if complexity and complexity not in {"trivial", "simple", "standard", "complex"}:
    raise ValueError("Invalid subtask complexity")
  st.key(work_item)
  state = st.session(session_id)
  launch = persistent_tasks.context(session_id)
  if launch:
    state = {**state, "agents": [n for n in launch["authorized_agents"] if state["agents"] is None or n in state["agents"]]}
  if state["mode"] != "auto":
    if needs_environment:
      return {"action":"replan","reason":"Delegation is off/manual and main environment capabilities are unverified; use explicit one-shot selection or authorized non-model preflight"}
    return {"action": "main", "reason": "Automatic delegation is off/manual; explicit one-shot selection remains available"}
  task = persistent_tasks.task_for(session_id) or (ac.load(state["active_task"]) if state.get("active_task") else ac.begin(session_id))
  if task["arm"] == "baseline":
    if needs_environment:
      return {"action":"replan","reason":"Baseline excludes delegation and does not verify main execution capabilities"}
    return {"action": "main", "reason": "Baseline arm excludes plugin delegation"}
  with st.locked():
    task = ac.load(task["id"])
    ac.collect(task)
    ac.save(task)
    reuse = work_state(task, work_item)
    measurement = ac.report(task)
    work_jobs = [st.job(j) for j in task["jobs"] if st.job(j).get("work_item", "main") == work_item]
    previous_jobs = [j for j in work_jobs if at.model_attempted(j)]
  prior = work_jobs[-1] if work_jobs else {}
  if task_profile is None:
    task_profile = policy.normalize(prior.get("task_profile"))
  assessment = policy.assess(task_profile, complexity, prior.get("complexity") or task["complexity"])
  shell = assessment["requires_shell"]
  needs_environment = needs_environment or shell
  if reuse:
    if reuse["action"] == "reuse":
      accepted = st.job(reuse["job_id"])
      if ((task_profile is not None and task_profile != accepted.get("task_profile"))
          or (complexity and complexity != accepted.get("complexity"))):
        return {"action": "replan", "job_id": accepted["id"],
                "reason": "Task assessment changed; revalidate the accepted artifact against the new task before reuse"}
    return reuse
  # Classification belongs to this work item; do not relabel its parent ledger.
  task = {**task, "complexity": assessment["complexity"],
          "category": (category or prior.get("category") or task["category"])[:80]}
  if task["token_budget"] and measurement["total"]["processed_tokens"] >= task["token_budget"]:
    return {"action": "replan", "reason": "Observed task dispatch budget exhausted; no new work authorized by this plan"}
  main_tokens = estimated_main_tokens or {"trivial": 2000, "simple": 12000, "standard": 40000, "complex": 100000}[task["complexity"]]
  history = st.read("optimization-samples.json", {})
  main_models = sorted({r["model"] for r in task["requests"].values() if r["source"] == "parent"})
  main_samples = [r["total"] for r in history.values() if not r["rows"] and r.get("measurement_complete")
    and r["verdict"] == "accepted" and (r["category"], r["complexity"]) == (task["category"], task["complexity"])
    and main_models and r.get("main_models") == main_models]
  main_source = "caller estimate" if estimated_main_tokens else "bootstrap"
  if not estimated_main_tokens and len(main_samples) >= 3:
    main_tokens, main_source = round(statistics.median(main_samples)), "empirical main-only tasks"
  # Trivial work remains in main even if another subscription has unused capacity.
  if task["complexity"] == "trivial":
    if needs_environment:
      return {"action":"replan","reason":"This small task needs an execution-environment check; use authorized non-model preflight rather than assuming main capabilities or spending a worker model call"}
    return {"action": "main", "reason": "Avoid worker startup and handoff overhead for trivial work",
            "assessment": assessment,
            "estimate_source": "bootstrap heuristic, not a measured saving"}
  cfg = st.config()
  execution_specs, capability_errors = {}, {}
  for name, role in cfg["agents"].items():
    provider = cfg["providers"][role["provider"]]
    if not role["enabled"] or not role["automatic"] or not provider["enabled"]:
      continue
    if state["agents"] is not None and name not in state["agents"]:
      continue
    if (operation == "write") != (role["workspace"] == "worktree"):
      continue
    if shell and provider["kind"] != "codex":
      capability_errors["agent:" + name] = "Task validation requires shell tools; this provider is file-tools-only"
      continue
    try:
      if required_files or output_files:
        input_contract.check(provider, role["workspace"], cwd, required_files, output_files)
      execution_specs[name] = capabilities.select(role, provider, requirements, execution_profile)
    except ValueError as exc:
      capability_errors["agent:" + name] = str(exc)
  if needs_environment and not execution_specs:
    return {"action":"replan","reason":"No permitted role satisfies the execution requirements; main capabilities are unverified",
            "assessment": assessment, "errors":capability_errors}
  if refresh and state.get("host_kind", "claude") == "claude":
    service.quota("claude", refresh=True, session_id=session_id)
  host = state.get("host_kind", "claude")
  parent_provider = state.get("parent_provider") if host != "claude" else "claude"
  if host == "claude":
    parent_sample = budget.read_claude(session_id)
    routing = budget.recommendation(session_id)
  elif parent_provider:
    parent_sample = service.quota(parent_provider, refresh=refresh, session_id=session_id)
    parent_quota_kind = "codex" if cfg["providers"][parent_provider]["kind"] == "codex" else "claude"
    parent_remaining = budget.remaining(parent_sample, parent_quota_kind)
    routing = {"mode":"handoff" if parent_remaining is not None and parent_remaining <= 5 else "normal"}
  else:
    parent_sample = {"status":"unknown"}
    routing = {"mode":"normal"}
  parent_quota_kind = "codex" if parent_provider and cfg["providers"][parent_provider]["kind"] == "codex" else "claude"
  main_pressure = pressure(parent_sample, parent_quota_kind)
  candidates, errors, catalogs, quotas, pressures = [], capability_errors, {}, {}, {}
  desired_tier = {"simple": 0, "standard": 1, "complex": 2}[task["complexity"]]
  desired_effort = {"simple": "low", "standard": "medium", "complex": "high"}[task["complexity"]]
  if previous_jobs:
    prior = previous_jobs[-1]
    prior_tier = tier(prior["model"])
    desired_tier = max(desired_tier, min(2, prior_tier + (prior.get("review") == "needs-work")))
    level = EFFORTS.index(prior["effort"]) if prior["effort"] in EFFORTS else 2
    desired_effort = EFFORTS[min(max(EFFORTS.index(desired_effort), level + (prior.get("review") == "needs-work")),
                                len(EFFORTS) - 1)]
  lane = "complex" if desired_tier == 2 else task["complexity"]
  for name, role in cfg["agents"].items():
    provider_name = role["provider"]
    provider = cfg["providers"][provider_name]
    if not role["enabled"] or not role["automatic"] or not provider["enabled"]:
      continue
    if state["agents"] is not None and name not in state["agents"]:
      continue
    if state["strategy"] == "preserve-parent" and parent_provider and provider_name == parent_provider:
      continue
    if state["strategy"] == "preserve-claude" and provider["kind"] == "claude":
      continue
    if (operation == "write") != (role["workspace"] == "worktree"):
      continue
    if provider_name == parent_provider and routing["mode"] in {"external-preferred", "handoff"}:
      continue
    if name not in execution_specs:
      continue
    execution = execution_specs[name]
    try:
      if provider_name not in catalogs:
        catalogs[provider_name] = service.catalog(provider_name, refresh=refresh)
        quotas[provider_name] = service.quota(provider_name, refresh=refresh, session_id=session_id)
        pressures[provider_name] = pressure(quotas[provider_name], "codex" if provider["kind"] == "codex" else "claude")
      catalog, quota = catalogs[provider_name], quotas[provider_name]
      if provider["kind"] == "codex":
        service.check_quota(quota, cfg["quota_reserve_percent"])
      if quota.get("data", {}).get("is_available") is False:
        continue
      rows = catalog["models"] if provider["kind"] == "codex" else [
        next((r for r in catalog.get("model_details", []) if r["id"] == m), {"model": m}) for m in catalog["models"]]
      options = []
      for row in rows:
        model = row.get("model", row.get("id"))
        if row.get("hidden"):
          continue
        if role.get("models") is not None and model not in role["models"]:
          continue
        if service.model_cooldown(provider_name, model):
          continue
        if quota.get("status") == "limited" and quota.get("model") == model:
          continue
        levels = [r["reasoningEffort"] for r in row.get("supportedReasoningEfforts", [])] if provider["kind"] == "codex" else row.get("effort", {}).get("supported_levels", catalog["efforts"])
        levels = [e for e in levels if e in EFFORTS and EFFORTS.index(e) <= EFFORTS.index(policy.MAX_AUTO_EFFORT)
                  and (role.get("efforts") is None or e in role["efforts"])]
        model_tier = tier(model, row.get("description", ""))
        if model_tier < desired_tier:
          continue
        if desired_tier == 2:
          levels = [e for e in levels if EFFORTS.index(e) >= EFFORTS.index("high")]
        if previous_jobs and tier(model) <= tier(previous_jobs[-1]["model"]):
          previous_level = EFFORTS.index(previous_jobs[-1]["effort"]) if previous_jobs[-1]["effort"] in EFFORTS else 0
          levels = [e for e in levels if EFFORTS.index(e) >= previous_level]
        if not levels:
          continue
        effort = min(levels, key=lambda e: abs(EFFORTS.index(e) - EFFORTS.index(desired_effort)))
        options.append((abs(model_tier - desired_tier), model, effort, model_tier))
      if not options:
        continue
      best_distance = min(o[0] for o in options)
      shortlist = sorted([o for o in options if o[0] == best_distance], key=lambda option: policy.preference(option[1], lane))
      for _, model, effort, model_tier in shortlist[:4]:
        observed = observations(task, name, model, effort, history)
        source = "empirical" if (task["category"] != "general" and observed["attempts"] >= 3 and observed["cost_samples"] == observed["attempts"]
                                  and not observed["unreviewed"]) else "bootstrap"
        startup = 18000 if provider["kind"] == "codex" else 5000
        handoff = observed["mean_parent_share"] if source == "empirical" else 4000 + context_chars // 4
        expected = ((observed["mean_attempt_tokens"] + handoff) / observed["smoothed_success_rate"]
                    if source == "empirical" else startup + handoff + main_tokens)
        quota_pressure = pressures[provider_name]
        candidates.append({"action": "delegate", "agent": name, "provider": provider_name, "model": model,
          "model_family": policy.family(model), "preference_rank": policy.preference(model, lane),
          "routing_lane": lane, "minimum_capability_tier": desired_tier,
          "execution_profile": execution["profile"],
          "effort": effort, "capability_tier": model_tier, "expected_processed_tokens": round(expected), "estimate_source": source,
          "observations": observed, "quota": quota_pressure,
          "funding": "subscription" if provider["kind"] in {"codex", "claude"} else "API balance; actual cost may be unknown"})
    except Exception as exc:
      errors[provider_name] = str(exc)
  parent_models = [r["model"] for r in task["requests"].values() if r["source"] == "parent"]
  parent_model = parent_models[-1] if parent_models else state.get("parent_model") or None
  parent_tier = tier(parent_model) if parent_model else None
  main = {"action": "main", "model": parent_model, "capability_tier": parent_tier,
          "expected_processed_tokens": main_tokens, "estimate_source": main_source,
          "quota": main_pressure, "funding": "subscription" if parent_provider in {"codex", "claude"} else "parent cost unknown"}
  remaining = main_pressure["remaining_percent"]
  reserve_main = ((state["strategy"] == "preserve-parent" or (state["strategy"] == "preserve-claude" and parent_provider == "claude")) or routing["mode"] == "handoff"
                  or (remaining is not None and remaining <= 20 and main_tokens > 3000))
  # The parent transcript does not expose its effort. Do not assume a frontier
  # parent is already using the high reasoning required by complex work.
  if not needs_environment and not reserve_main and desired_tier < 2 and (parent_tier is not None and parent_tier >= desired_tier
                           or parent_tier is None and desired_tier < 2):
    candidates.append(main)
  if not candidates:
    return {"action": "replan", "reason": "No eligible backend with capacity; do not expand account permissions",
            "assessment": assessment, "errors": errors}
  # Small bounded lookups still avoid a worker. Substantial simple work prefers
  # fast models; standard/complex work requires the matching capability floor.
  if task["complexity"] == "simple" and main_tokens <= 3000 and main in candidates and not previous_jobs:
    candidates = [main]
  else:
    distance = min(abs((c["capability_tier"] if c["capability_tier"] is not None else desired_tier) - desired_tier)
                   for c in candidates)
    candidates = [c for c in candidates if abs(
      (c["capability_tier"] if c["capability_tier"] is not None else desired_tier) - desired_tier) == distance]
  delegates = [c for c in candidates if c["action"] == "delegate"]
  if delegates:
    preferred = min(c["preference_rank"] for c in delegates)
    candidates = [c for c in candidates if c["action"] != "delegate" or c["preference_rank"] == preferred]
  best_cost = min(c["expected_processed_tokens"] for c in candidates)
  # Bounded tradeoff: surplus can win only among candidates within 15% of best token estimate.
  close = [c for c in candidates if c["expected_processed_tokens"] <= best_cost * 1.15]
  selected = min(close, key=lambda c: (-c["quota"]["surplus_priority"], c["expected_processed_tokens"]))
  result = {**selected, "id": "p" + uuid.uuid4().hex[:16], "session_id": session_id, "task_id": task["id"],
            "cwd": cwd, "required_files": required_files, "output_files": output_files,
            "assessment": assessment,
            "requirements": capabilities.normalize(requirements),
            "work_item": work_item, "complexity": task["complexity"], "category": task["category"],
            "created_at": time.time(), "operation": operation,
            "reason": "Subtask capability and effort first, then expected work and quota headroom",
            "capability_basis": "model family/catalog description heuristic, not a measured cross-provider quality ranking",
            "scope": "token-volume heuristic; not a claim about billed cost or optimal quality",
            "candidates": sorted(candidates, key=lambda c: c["expected_processed_tokens"])[:5], "errors": errors}
  st.write("plan-" + result["id"] + ".json", result)
  return result


def validate_locked(task: dict[str, Any], job: dict[str, Any], plan_id: str = "") -> None:
  """Apply task dispatch guards atomically with service.run's global registration lock."""
  current = ac.load(task["id"])
  ac.collect(current)
  ac.save(current)
  if current["status"] != "open" or current["arm"] == "baseline":
    raise ValueError("This task is closing/closed or is a main-only baseline")
  blocked = work_state(current, job["work_item"])
  if blocked:
    raise ValueError(blocked["reason"])
  measured = ac.report(current)["total"]["processed_tokens"]
  if current["token_budget"] and measured >= current["token_budget"]:
    raise ValueError("Observed task budget exhausted; no further dispatch")
  if plan_id:
    plan = st.read("plan-" + st.key(plan_id) + ".json")
    if not plan or plan.get("used_by") or time.time() - plan["created_at"] > 300:
      raise ValueError("Plan is missing, already used or expired; obtain a fresh plan")
    expected = (job["session_id"], task["id"], job["work_item"], job["agent"], job["model"], job["effort"])
    actual = (plan["session_id"], plan["task_id"], plan["work_item"], plan.get("agent"), plan.get("model"), plan.get("effort"))
    classification_matches = all(job.get(k) == plan[k] for k in ("complexity", "category") if k in plan)
    if plan["action"] != "delegate" or actual != expected or not classification_matches:
      raise ValueError("Dispatch does not match this session/task/model/effort plan")
    if "assessment" in plan and job.get("task_assessment") != plan["assessment"]:
      raise ValueError("Dispatch task assessment differs from its plan; obtain a fresh plan")
    if "assessment" not in plan and job.get("task_profile") is not None:
      raise ValueError("This plan predates structured task assessment; obtain a fresh plan")
    if "required_files" in plan and any(job.get(k) != plan[k] for k in ("required_files", "output_files")):
      raise ValueError("Dispatch file-access contract differs from its plan; obtain a fresh plan")
    execution = job.get("execution_environment") or {}
    if "requirements" in plan:
      if plan["requirements"] != execution.get("requirements") or plan.get("execution_profile") != execution.get("profile"):
        raise ValueError("Dispatch execution requirements/profile differ from this plan; obtain a fresh plan")
    elif execution.get("profile") not in {None, "sandboxed", "file-tools"} or any(
        (execution.get("requirements") or {}).get(k) for k in ("gpu", "ssh", "network")):
      raise ValueError("Plan predates execution requirements; obtain a fresh plan")

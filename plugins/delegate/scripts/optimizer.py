"""Measured task routing with conservative bootstrap estimates and explicit quota tradeoffs."""
from typing import Any
import statistics
import hashlib
import json
import time
import uuid

import storage as st
import accounting as ac
import advisory
import budget
import attempts as at
import capabilities
import routing_policy as policy
import dispatch_policy
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


def _generation_rank(row: dict[str, Any]) -> int:
  """Deprioritize previous-generation or legacy models within the same tier/family."""
  desc = str(row.get("description", "")).lower()
  if "legacy" in desc:
    return 2
  if "previous generation" in desc:
    return 1
  return 0


def _catalog_rows(provider: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
  """Normalize provider catalog entries across Codex, Claude, Anthropic, and Antigravity."""
  if provider["kind"] == "codex":
    return list(catalog["models"])
  details = catalog.get("model_details") or []
  return [
    next((r for r in details if r.get("id", r.get("model")) == m), {"model": m})
    for m in catalog["models"]
  ]


def _row_efforts(row: dict[str, Any], provider: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
  """Extract supported reasoning effort levels for one catalog model entry."""
  if provider["kind"] == "codex":
    return [r["reasoningEffort"] for r in row.get("supportedReasoningEfforts", [])]
  effort_meta = row.get("effort")
  if isinstance(effort_meta, dict):
    return list(effort_meta.get("supported_levels", catalog.get("efforts", [])))
  return list(catalog.get("efforts") or provider.get("efforts") or [])


def role_pair(role: dict[str, Any], provider: dict[str, Any], catalog: dict[str, Any],
              model: str, effort: str, complexity: str, provider_name: str,
              previous: dict[str, Any] | None = None, escalate: bool = False,
              recommended_effort: str = "") -> tuple[str, str]:
  """Resolve auto within one explicitly selected role, never switch providers or grant permissions."""
  import service
  base_effort = (recommended_effort if recommended_effort in EFFORTS
                 else {"trivial": "low", "simple": "low", "standard": "medium", "complex": "high"}[complexity])
  desired = ({"trivial": 0, "simple": 0, "standard": 1, "complex": 2}[complexity], base_effort)
  if previous:
    prior_tier = tier(previous["model"])
    prior_effort = EFFORTS.index(previous["effort"]) if previous["effort"] in EFFORTS else 0
    desired = (max(desired[0], min(2, prior_tier + (previous.get("review") == "needs-work"))),
               EFFORTS[min(max(EFFORTS.index(desired[1]), prior_effort + (previous.get("review") == "needs-work")),
                           len(EFFORTS) - 1)])
  rows = _catalog_rows(provider, catalog)
  lane = {0: "simple", 1: "standard", 2: "complex"}[desired[0]]
  default_name = (role or {}).get("default_model") or (provider or {}).get("default_model")
  options = []
  for row in rows:
    name = row.get("model", row.get("id"))
    if dispatch_policy.reason(name, role, provider, escalate, previous, complexity=complexity):
      continue
    if model == "auto" and row.get("hidden"):
      continue
    if model != "auto" and model != name:
      continue
    if role.get("models") is not None and name not in role["models"]:
      continue
    if service.model_cooldown(provider_name, name):
      continue
    level = effective_tier(
      name, desired[0], row.get("description", ""),
      role=role, provider=provider, escalate=escalate, previous=previous,
    )
    if model == "auto" and level < desired[0]:
      continue
    levels = _row_efforts(row, provider, catalog)
    gen = _generation_rank(row) if model == "auto" else 0
    pref = policy.preference(name, lane) if model == "auto" else 0
    default_rank = 0 if (model == "auto" and default_name and name == default_name) else 1
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
      options.append((abs(level - desired[0]), pref, default_rank, gen,
                      abs(EFFORTS.index(candidate) - EFFORTS.index(desired[1])),
                      EFFORTS.index(candidate), name, candidate))
  if not options:
    raise ValueError(
      "No allowed model/effort pair satisfies this role and complexity; "
      "inspect delegate_catalog(agent_name=...) or revise explicitly"
    )
  chosen = min(options, key=lambda option: option[:6])
  return chosen[6], chosen[7]


def _tier_override(model: str, cfg: dict[str, Any] | None = None) -> int | None:
  """Return configured explicit or substring model tier override if defined."""
  overrides = (st.config() if cfg is None else cfg).get("model_tiers") or {}
  if not isinstance(overrides, dict):
    return None
  if model in overrides and type(overrides[model]) is int and 0 <= overrides[model] <= 2:
    return overrides[model]
  lowered = model.lower()
  for key, level in overrides.items():
    if isinstance(key, str) and type(level) is int and 0 <= level <= 2 and key.lower() in lowered:
      return level
  return None


def tier(model: str, description: str = "") -> int:
  """Classify known model families for bootstrap selection, not as measured quality ratings."""
  override = _tier_override(model)
  if override is not None:
    return override
  fam = policy.family(model)
  if fam in {"terra", "gemini-flash"}:
    return 0  # Fast workflow lane; not a benchmark of intrinsic model quality.
  if fam == "gemini-neon" or "fable" in model.lower():
    return 2
  if fam == "gemini-pro":
    return 2 if "-pro" in model.lower() else 1
  name = (model + " " + description).lower()
  if any(word in name for word in ("luna", "haiku", "flash", "-mini", " mini", "fast and affordable")):
    return 0
  if any(word in name for word in ("astra", "opus", "-pro", "frontier")):
    return 2
  return 1


def effective_tier(
  model: str,
  desired_tier: int,
  description: str = "",
  role: dict[str, Any] | None = None,
  provider: dict[str, Any] | None = None,
  escalate: bool = False,
  previous: dict[str, Any] | None = None,
  cfg: dict[str, Any] | None = None,
) -> int:
  """Compute planning capability tier while letting DeepSeek default to flash unless escalated.

  Args:
    model: Candidate model identifier.
    desired_tier: Target minimum tier for the current subtask (0..2).
    description: Optional catalog model description text.
    role: Role configuration dictionary.
    provider: Provider configuration dictionary.
    escalate: Whether explicit complex escalation was requested.
    previous: Optional prior job record for retry continuity.
    cfg: Optional configuration override dictionary.

  Returns:
    Integer capability tier in 0..2 used for floor admission and ranking.
  """
  override = _tier_override(model, cfg=cfg)
  if override is not None:
    return override
  default_name = (role or {}).get("default_model") or (provider or {}).get("default_model")
  fam = policy.family(model)
  is_ds_default = fam == "deepseek-flash" or (
    "deepseek" in model.lower() and bool(default_name) and model == default_name and fam != "deepseek-pro"
  )
  if is_ds_default:
    # Keep DeepSeek on flash by default across standard/complex unless escalated or retrying v4-pro.
    prior_model = (previous or {}).get("model", "")
    prior_was_pro = (
      policy.family(prior_model) == "deepseek-pro"
      or ("deepseek" in prior_model.lower() and tier(prior_model) >= 2)
    )
    if prior_was_pro or (escalate and desired_tier >= 2):
      return min(1, max(0, desired_tier))
    return max(0, min(2, desired_tier))
  return tier(model, description)


def pressure(sample: dict[str, Any], provider: str) -> dict[str, Any]:
  """Use all reported windows; imminent reset only breaks close cost ties."""
  remaining = budget.remaining(sample, provider)
  data = sample.get("data") or {}
  windows = [(data.get("rateLimits") or {}).get(k) for k in ("primary", "secondary")] if provider == "codex" else [
    data.get(k) for k in ("five_hour", "seven_day", "spend_limit")]
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
  quality_known_attempts = attempts - unknown
  rate_denom = quality_known_attempts if quality_known_attempts >= 3 else attempts
  return {"attempts": attempts, "accepted": accepted, "unreviewed": unknown,
          "quality_known_attempts": quality_known_attempts,
          "cost_samples": len(costs), "smoothed_success_rate": (accepted + 1) / (rate_denom + 2),
          "mean_attempt_tokens": statistics.mean(costs) if costs else None,
          "mean_parent_share": statistics.mean(overheads) if overheads else None}


def default_work_item(
  task: str,
  acceptance: str,
  context: str,
  cwd: str,
  operation: str,
  required_files: list[str] | None = None,
  output_files: list[str] | None = None,
  source_inputs: list[str] | None = None,
  source_inputs_base: str = "repo",
  snapshot_required_files: bool = False,
  permissions: str = "auto",
  writable_roots: list[str] | None = None,
  network_access: bool | None = None,
) -> str:
  """Identify an omitted work item from its intent and declared inputs, never its chosen model.

  Args:
    task: Subtask instruction text.
    acceptance: Verifiable acceptance criteria.
    context: Focused context text.
    cwd: Target working directory.
    operation: Canonical ``read`` or ``write`` operation.
    required_files: Declared required input file paths.
    output_files: Declared output artifact paths.
    source_inputs: Optional selected draft paths.
    source_inputs_base: Base for ``source_inputs`` (``repo`` or ``cwd``).
    snapshot_required_files: Whether dirty ``required_files`` are snapshotted.
    permissions: Subagent permission tier or ``auto``.
    writable_roots: Optional shared writable directories outside ``cwd``.
    network_access: Optional explicit network override.

  Returns:
    Deterministic ``auto-<sha256_prefix>`` identifier string.

  Raises:
    ValueError: If ``permissions``, ``writable_roots``, or ``network_access`` is invalid.
  """
  contract: list[Any] = [
    sorted(set(required_files or [])), sorted(set(output_files or [])), sorted(set(source_inputs or [])),
    source_inputs_base, snapshot_required_files,
  ]
  norm_perms = capabilities.normalize_permissions(permissions)
  if norm_perms != "auto":
    contract.append(norm_perms)
  norm_roots = capabilities.normalize_writable_roots(writable_roots)
  if norm_roots:
    contract.append(norm_roots)
  norm_net = capabilities.normalize_network_access(network_access)
  if norm_net is not None:
    contract.append(norm_net)
  intent = [task.strip(), acceptance.strip(), context.strip(), str(Path(cwd).resolve()), operation, contract]
  return "auto-" + hashlib.sha256(json.dumps(intent, ensure_ascii=False).encode()).hexdigest()[:24]


def matches_work_item(job: dict[str, Any], work_item: str) -> bool:
  """Match exact keys plus equivalent legacy main jobs without mixing unrelated historical intents."""
  if job.get("work_item", "main") == work_item:
    return True
  if (job.get("work_item", "main") != "main" or not isinstance(work_item, str)
      or not work_item.startswith("auto-") or not isinstance(job.get("task"), str)):
    return False
  execution = job.get("execution_environment") or {}
  roots = job.get("writable_roots") if "writable_roots" in job else execution.get("writable_roots")
  return default_work_item(
    job["task"], str(job.get("acceptance", "")), str(job.get("context", "")),
    job.get("source_cwd") or job.get("cwd") or ".",
    "write" if job.get("role", {}).get("workspace") == "worktree" else "read",
    job.get("required_files"), job.get("output_files"), job.get("source_inputs"),
    job.get("source_inputs_base", "repo"), job.get("snapshot_required_files", False),
    job.get("permissions", "auto"), roots, execution.get("network_access_requested"),
  ) == work_item


def _error_signature(err: Any) -> str:
  """Normalize string or structured Codex error payloads into a comparable signature."""
  if isinstance(err, str):
    return err.strip()
  if isinstance(err, dict):
    core = {k: err[k] for k in ("codexErrorInfo", "type", "code", "message") if k in err and err[k] is not None}
    return json.dumps(core or err, sort_keys=True, ensure_ascii=False)
  return ""


def _is_gate_review_job(job: dict[str, Any]) -> bool:
  """Return True when a job is a gate/read-only review rather than a worktree builder."""
  if job.get("review_gate") or job.get("workflow") == "gate-review":
    return True
  workspace = (job.get("role") or {}).get("workspace")
  return workspace != "worktree" and not str(job.get("agent", "")).endswith("-builder")


def work_state(task: dict[str, Any], work_item: str) -> dict[str, Any] | None:
  """Avoid duplicate work and bound repair loops for one logical work item."""
  jobs = [st.job(j) for j in task["jobs"] if matches_work_item(st.job(j), work_item)]
  running = [j["id"] for j in jobs if j["status"] in ACTIVE]
  if running:
    return {"action": "wait", "job_ids": running, "reason": "This work item already has running work"}
  accepted = [j for j in jobs if j.get("review") == "accepted"]
  if accepted and not accepted[-1].get("evidence_checkpoint"):
    return {"action": "replan", "job_id": accepted[-1]["id"], "work_item": work_item,
      "reason": "Accepted result lacks an evidence checkpoint; revalidate before reuse",
      "recovery": "For this same work, inspect current evidence, call delegate_inspect checkpoint with explicit files, then verify again. For different work omit work_item on delegate_run or choose a distinct stable ID. Acceptance text is not a file fingerprint."}
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
  needs_work = [j for j in jobs if _is_gate_review_job(j) and j.get("review") == "needs-work"]
  review_round = len(needs_work)
  if review_round >= advisory.MAX_REVIEW_ROUNDS:
    return {
      "action": "escalate_to_user",
      "job_id": needs_work[-1]["id"],
      "work_item": work_item,
      "review_round": review_round,
      "max_review_rounds": advisory.MAX_REVIEW_ROUNDS,
      "reason": (
        f"Review round cap reached ({review_round}/{advisory.MAX_REVIEW_ROUNDS} needs-work rounds); "
        "escalate open trade-offs to the user instead of starting round 3+"
      ),
    }
  counted = [j for j in jobs if at.model_attempted(j)]
  if len(counted) >= task["max_attempts"]:
    return {"action": "replan", "reason": "Work-item attempt limit reached; revisit the approach before spending more"}
  if len(jobs) >= 2:
    prev_job, last_job = jobs[-2], jobs[-1]
    prev_sig = _error_signature(prev_job.get("error"))
    if (prev_job.get("status") == "failed" and last_job.get("status") == "failed"
        and prev_sig and prev_sig == _error_signature(last_job.get("error"))):
      return {
        "action": "replan",
        "job_id": last_job["id"],
        "reason": "Repeated identical failure on this work_item; fix contract, permissions, or inputs before retrying",
        "last_error": prev_sig[:240],
      }
    if (prev_job.get("status") == "timed_out" and last_job.get("status") == "timed_out"
        and not last_job.get("resume_job") and prev_job.get("timeout_seconds")
        and prev_job.get("timeout_seconds") == last_job.get("timeout_seconds")):
      return {
        "action": "replan",
        "job_id": last_job["id"],
        "reason": (
          "Repeated scratch timeout with unchanged timeout_seconds; "
          "resume_job with higher timeout_seconds or split the task"
        ),
      }
  return None


def plan(session_id: str, work_item: str = "main", operation: str = "read",
         estimated_main_tokens: int = 0, context_chars: int = 0, refresh: bool = False,
         complexity: str = "", category: str = "", requirements: dict[str, Any] | None = None,
         execution_profile: str = "auto", task_profile: dict[str, Any] | None = None,
         cwd: str = "", required_files: list[str] | None = None, output_files: list[str] | None = None,
         escalate: bool = False, permissions: str = "auto",
         writable_roots: list[str] | None = None, network_access: bool | None = None,
         workflow: str = "auto", blind_context: bool = False, review_gate: bool = False,
         estimated_lines: int | None = None) -> dict[str, Any]:
  """Recommend main or a concrete role/model/effort; no inference or dispatch occurs here.

  Args:
    session_id: Owning parent session ID.
    work_item: Logical subtask identifier.
    operation: ``read`` or ``write`` intent when ``permissions`` is ``auto``.
    estimated_main_tokens: Optional parent token estimate.
    context_chars: Character length of the context payload.
    refresh: Whether to bypass cached quota/catalog reads.
    complexity: Minimum complexity level or empty/``auto``.
    category: Subtask category label.
    requirements: Optional GPU, SSH, or network declarations.
    execution_profile: Sandbox profile (``auto``/``sandboxed``/``network``/``host``).
    task_profile: Structured subtask assessment signals.
    cwd: Execution directory for file-contract checks.
    required_files: Declared required input file paths.
    output_files: Declared output artifact paths.
    escalate: Explicit Astra escalation flag under ``sol-first``.
    permissions: Explicit subagent permission tier.
    writable_roots: Optional shared writable directories outside ``cwd``.
    network_access: Optional explicit network boolean override.
    workflow: Advisory workflow label from ``advisory.WORKFLOWS``.
    blind_context: Whether independent blind second-opinion mode was requested.
    review_gate: Whether 2-tier ``BLOCKING`` vs ``ADVISORY`` review gate was requested.
    estimated_lines: Optional changed-line estimate for inline break-even gating.

  Returns:
    Recommendation dictionary with ``action``, ``advisory``, and selected route or ``replan`` reason.

  Raises:
    ValueError: If arguments fail validation.
  """
  import service  # Delayed to keep service's execution gates independent.
  if type(escalate) is not bool or type(blind_context) is not bool or type(review_gate) is not bool:
    raise ValueError("escalate, blind_context, and review_gate must be booleans")
  workflow = advisory.normalize_workflow(workflow)
  if estimated_lines is not None and (type(estimated_lines) is not int or estimated_lines < 0):
    raise ValueError("estimated_lines must be a non-negative integer or None")
  permissions = capabilities.normalize_permissions(permissions)
  needed = capabilities.normalize(requirements)
  capabilities.validate_profile(execution_profile)
  net_req = capabilities.normalize_network_access(network_access)
  task_profile = policy.normalize(task_profile)
  if complexity == "auto":
    complexity = ""
  effective_review_gate = review_gate or workflow == "gate-review"
  sensitive_scope = advisory.is_sensitive_scope(
    [*(required_files or []), *(output_files or [])],
    (task_profile or {}).get("reason", "") or category,
  )
  shell = policy.assess(
    task_profile, complexity, review_gate=effective_review_gate, sensitive_scope=sensitive_scope,
  )["requires_shell"]
  needs_environment = (
    any(needed[k] for k in ("gpu", "ssh", "network"))
    or execution_profile not in {"auto", "sandboxed"}
    or bool(net_req)
    or permissions in {"network-shell", "full-access"}
  )
  needs_environment = needs_environment or shell
  operation = normalize_operation(operation)
  cwd = str(Path(cwd).resolve(strict=True)) if cwd else ""
  norm_roots = capabilities.normalize_writable_roots(writable_roots, base_cwd=cwd)
  if permissions != "auto":
    operation = "read" if permissions == "read-only" else "write"
  elif norm_roots:
    operation = "write"
  if permissions == "read-only" and norm_roots:
    raise ValueError("writable_roots cannot be combined with permissions='read-only'")
  required_files = input_contract.normalize(required_files, "required_files")
  output_files = input_contract.normalize(output_files, "output_files")
  needs_environment = needs_environment or bool(required_files or output_files or norm_roots)
  if (required_files or output_files) and not cwd:
    raise ValueError("cwd is required for file-access preflight")
  if output_files and operation != "write":
    return {
      "action": "replan",
      "reason": (
        "output_files cannot be written by read-only reviewers; "
        "choose write with source_inputs or return the report as text"
      ),
    }
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
    allowed_pool = [n for n in launch["authorized_agents"] if state["agents"] is None or n in state["agents"]]
    state = {**state, "agents": allowed_pool}
  early_adv = advisory.recommend(
    (task_profile or {}).get("reason", "") or category or work_item,
    caller_host=state.get("host_kind", "claude"),
    workflow="gate-review" if effective_review_gate and workflow == "auto" else workflow,
    task_profile=task_profile,
    required_files=required_files,
    output_files=output_files,
    estimated_lines=estimated_lines,
  )
  if state["mode"] != "auto":
    if needs_environment:
      return {
        "action": "replan",
        "advisory": early_adv,
        "reason": (
          "Delegation is off/manual and main environment capabilities are unverified; "
          "use explicit one-shot selection or authorized non-model preflight"
        ),
      }
    return {
      "action": "main",
      "advisory": early_adv,
      "reason": "Automatic delegation is off/manual; explicit one-shot selection remains available",
    }
  task = (
    persistent_tasks.task_for(session_id)
    or (ac.load(state["active_task"]) if state.get("active_task") else ac.begin(session_id))
  )
  if task["arm"] == "baseline":
    if needs_environment:
      return {
        "action": "replan",
        "advisory": early_adv,
        "reason": "Baseline excludes delegation and does not verify main execution capabilities",
      }
    return {"action": "main", "advisory": early_adv, "reason": "Baseline arm excludes plugin delegation"}
  with st.locked():
    task = ac.load(task["id"])
    ac.collect(task)
    ac.save(task)
    reuse = work_state(task, work_item)
    measurement = ac.report(task)
    work_jobs = [st.job(j) for j in task["jobs"] if matches_work_item(st.job(j), work_item)]
    previous_jobs = [j for j in work_jobs if at.model_attempted(j)]
  prior = work_jobs[-1] if work_jobs else {}
  if task_profile is None:
    task_profile = policy.normalize(prior.get("task_profile"))
  task_hint = str(prior.get("task") or (task_profile or {}).get("reason") or category or work_item)
  sensitive_scope = advisory.is_sensitive_scope([*(required_files or []), *(output_files or [])], task_hint)
  review_round = sum(1 for j in work_jobs if _is_gate_review_job(j) and j.get("review") == "needs-work")
  adv_rec = advisory.recommend(
    task_hint,
    caller_host=state.get("host_kind", "claude"),
    workflow="gate-review" if effective_review_gate and workflow == "auto" else workflow,
    task_profile=task_profile,
    required_files=required_files,
    output_files=output_files,
    estimated_lines=estimated_lines,
    review_round=review_round,
  )
  assessment = policy.assess(
    task_profile, complexity, prior.get("complexity") or task["complexity"],
    review_gate=effective_review_gate, sensitive_scope=sensitive_scope,
  )
  shell = assessment["requires_shell"]
  needs_environment = needs_environment or shell
  if reuse:
    if reuse["action"] == "reuse":
      accepted = st.job(reuse["job_id"])
      if ((task_profile is not None and task_profile != accepted.get("task_profile"))
          or (complexity and complexity != accepted.get("complexity"))):
        return {"action": "replan", "job_id": accepted["id"], "advisory": adv_rec,
                "reason": "Task assessment changed; revalidate the accepted artifact against the new task before reuse"}
    return {**reuse, "advisory": adv_rec} if "advisory" not in reuse else reuse
  if effective_review_gate and review_round >= advisory.MAX_REVIEW_ROUNDS:
    return {
      "action": "escalate_to_user",
      "work_item": work_item,
      "review_round": review_round,
      "max_review_rounds": advisory.MAX_REVIEW_ROUNDS,
      "advisory": adv_rec,
      "reason": adv_rec["reason"],
    }
  # Classification belongs to this work item; do not relabel its parent ledger.
  task = {**task, "complexity": assessment["complexity"],
          "category": (category or prior.get("category") or task["category"])[:80]}
  if task["token_budget"] and measurement["total"]["processed_tokens"] >= task["token_budget"]:
    return {"action": "replan", "advisory": adv_rec,
            "reason": "Observed task dispatch budget exhausted; no new work authorized by this plan"}
  main_tokens = estimated_main_tokens or {"trivial": 2000, "simple": 12000, "standard": 40000, "complex": 100000}[task["complexity"]]
  history = st.read("optimization-samples.json", {})
  main_models = sorted({r["model"] for r in task["requests"].values() if r["source"] == "parent"})
  main_samples = [r["total"] for r in history.values() if not r["rows"] and r.get("measurement_complete")
    and r["verdict"] == "accepted" and (r["category"], r["complexity"]) == (task["category"], task["complexity"])
    and main_models and r.get("main_models") == main_models]
  main_source = "caller estimate" if estimated_main_tokens else "bootstrap"
  if not estimated_main_tokens and len(main_samples) >= 3:
    main_tokens, main_source = round(statistics.median(main_samples)), "empirical main-only tasks"
  if (estimated_lines is not None and estimated_lines <= advisory.INLINE_LINE_THRESHOLD
      and not effective_review_gate and workflow != "dual-plan" and not needs_environment):
    return {"action": "main", "reason": adv_rec["reason"], "assessment": assessment,
            "advisory": adv_rec, "estimate_source": "inline line-count break-even gate"}
  # Trivial work remains in main even if another subscription has unused capacity.
  if task["complexity"] == "trivial":
    if needs_environment:
      return {
        "action": "replan",
        "advisory": adv_rec,
        "reason": (
          "This small task needs an execution-environment check; use authorized non-model "
          "preflight rather than assuming main capabilities or spending a worker model call"
        ),
      }
    return {"action": "main", "reason": "Avoid worker startup and handoff overhead for trivial work",
            "assessment": assessment, "advisory": adv_rec,
            "estimate_source": "bootstrap heuristic, not a measured saving"}
  cfg = st.config()
  target_workspace = "worktree" if operation == "write" else "read-only"
  matched_providers = {
    role["provider"] for name, role in cfg["agents"].items()
    if role["enabled"] and role["automatic"] and cfg["providers"][role["provider"]]["enabled"]
    and (state["agents"] is None or name in state["agents"])
    and role["workspace"] == target_workspace
  }
  execution_specs, capability_errors = {}, {}
  for name, role in cfg["agents"].items():
    provider = cfg["providers"][role["provider"]]
    if not role["enabled"] or not role["automatic"] or not provider["enabled"]:
      continue
    if state["agents"] is not None and name not in state["agents"]:
      continue
    if permissions == "auto":
      if (operation == "write") != (role["workspace"] == "worktree"):
        continue
    elif role["provider"] in matched_providers and role["workspace"] != target_workspace:
      continue
    eff_role = capabilities.effective_role(
      role, provider, permissions, writable_roots=norm_roots, base_cwd=cwd,
    )
    eff_perms = eff_role["permissions"]
    if (shell and provider["kind"] != "codex"
        and eff_perms not in {"workspace-shell", "network-shell", "full-access"}):
      capability_errors["agent:" + name] = (
        "Task validation requires shell tools; pass permissions='workspace-shell', "
        "'network-shell', or 'full-access' or choose Codex"
      )
      continue
    try:
      spec = capabilities.select(
        role, provider, requirements, execution_profile,
        permissions=eff_perms, writable_roots=norm_roots, network_access=net_req,
      )
      if required_files or output_files:
        input_contract.check(
          provider, eff_role["workspace"], cwd, required_files, output_files,
          permissions=eff_perms, writable_roots=norm_roots,
          unrestricted_fs=capabilities.is_unrestricted_fs(spec),
        )
      execution_specs[name] = spec
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
  default_effort = {"simple": "low", "standard": "medium", "complex": "high"}[task["complexity"]]
  base_effort = assessment.get("recommended_effort") or default_effort
  desired_effort = base_effort if base_effort in EFFORTS else "medium"
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
    if provider_name == parent_provider and routing["mode"] in {"external-preferred", "handoff"}:
      continue
    if name not in execution_specs:
      continue
    execution = execution_specs[name]
    if (operation == "write") != (execution["workspace"] == "worktree"):
      continue
    try:
      if provider_name not in catalogs:
        catalogs[provider_name] = service.catalog(provider_name, refresh=refresh)
        quotas[provider_name] = service.quota(provider_name, refresh=refresh, session_id=session_id)
        pressures[provider_name] = pressure(quotas[provider_name], "codex" if provider["kind"] == "codex" else "claude")
      catalog, quota = catalogs[provider_name], quotas[provider_name]
      if provider["kind"] == "codex":
        service.check_quota(quota, cfg["quota_reserve_percent"])
      available = pressures[provider_name]["remaining_percent"]
      if available is not None and available <= cfg["quota_reserve_percent"]:
        errors[provider_name] = "Known quota is at or below the configured reserve"
        continue
      if quota.get("data", {}).get("is_available") is False:
        continue
      rows = _catalog_rows(provider, catalog)
      default_name = (role or {}).get("default_model") or (provider or {}).get("default_model")
      options = []
      for row in rows:
        model = row.get("model", row.get("id"))
        rejection = dispatch_policy.reason(
          model, role, provider, escalate, previous_jobs[-1] if previous_jobs else None,
          cfg, complexity=task["complexity"], review_gate=effective_review_gate,
          sensitive_scope=sensitive_scope,
        )
        if rejection:
          errors[provider_name + ":" + model] = rejection
          continue
        if row.get("hidden"):
          continue
        if role.get("models") is not None and model not in role["models"]:
          continue
        if service.model_cooldown(provider_name, model):
          continue
        if quota.get("status") == "limited" and quota.get("model") == model:
          continue
        levels = _row_efforts(row, provider, catalog)
        levels = [e for e in levels if e in EFFORTS and EFFORTS.index(e) <= EFFORTS.index(policy.MAX_AUTO_EFFORT)
                  and (role.get("efforts") is None or e in role["efforts"])]
        model_tier = effective_tier(
          model, desired_tier, row.get("description", ""),
          role=role, provider=provider, escalate=escalate,
          previous=previous_jobs[-1] if previous_jobs else None, cfg=cfg,
        )
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
        options.append((abs(model_tier - desired_tier), model, effort, model_tier, _generation_rank(row)))
      if not options:
        continue
      for _, model, effort, model_tier, generation_rank in options:
        observed = observations(task, name, model, effort, history)
        source = "empirical" if (
          task["category"] != "general"
          and observed.get("quality_known_attempts", observed.get("attempts", 0)) >= 3
          and observed["cost_samples"] >= 3
        ) else "bootstrap"
        startup = 18000 if provider["kind"] == "codex" else (14000 if desired_tier >= 1 else 5000)
        handoff = observed["mean_parent_share"] if source == "empirical" else 4000 + context_chars // 4
        excess_tier = max(0, model_tier - desired_tier)
        effort_step = max(0, EFFORTS.index(effort) - EFFORTS.index(desired_effort))
        tier_basis = max(main_tokens, 40000)
        tier_overhead = round(tier_basis * 0.35 * excess_tier + main_tokens * 0.15 * effort_step)
        expected = ((observed["mean_attempt_tokens"] + handoff) / observed["smoothed_success_rate"]
                    if source == "empirical" else startup + handoff + main_tokens + tier_overhead)
        quota_pressure = pressures[provider_name]
        candidates.append({"action": "delegate", "agent": name, "provider": provider_name, "model": model,
          "provider_default": bool(provider.get("is_default")),
          "default_model_rank": 0 if (default_name and model == default_name) else 1,
          "model_family": policy.family(model), "preference_rank": policy.preference(model, lane),
          "generation_rank": generation_rank,
          "routing_lane": lane, "minimum_capability_tier": desired_tier,
          "execution_profile": execution["profile"], "permissions": execution["permissions"],
          "writable_roots": execution["writable_roots"], "network_access": execution["network_access"],
          "effort": effort, "capability_tier": model_tier, "expected_processed_tokens": round(expected),
          "estimate_source": source, "observations": observed, "quota": quota_pressure,
          "funding": "subscription" if provider["kind"] in {"codex", "claude", "antigravity"}
            else "API balance; actual cost may be unknown"})
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
            "assessment": assessment, "advisory": adv_rec, "errors": errors}
  # Small bounded lookups still avoid a worker. Substantial simple work prefers
  # fast models; standard/complex work requires the matching capability floor.
  if task["complexity"] == "simple" and main_tokens <= 3000 and main in candidates and not previous_jobs:
    candidates = [main]
  elif main in candidates and parent_tier is not None and parent_tier > desired_tier and any(
      c["action"] == "delegate" and c["capability_tier"] == desired_tier for c in candidates):
    candidates.remove(main)  # Preserve the stronger parent for reasoning rather than substantial lower-tier execution.
    reserve_main = True
  selected, selection_policy = policy.select(candidates, desired_tier)
  result = {**selected, "id": "p" + uuid.uuid4().hex[:16], "session_id": session_id, "task_id": task["id"],
            "cwd": cwd, "required_files": required_files, "output_files": output_files,
            "assessment": assessment, "advisory": adv_rec, "workflow": workflow,
            "blind_context": blind_context or adv_rec["blind_context"],
            "review_gate": effective_review_gate, "review_round": review_round,
            "max_review_rounds": advisory.MAX_REVIEW_ROUNDS, "escalate": escalate,
            "requested_permissions": permissions, "writable_roots": norm_roots, "network_access_requested": net_req,
            "requirements": capabilities.normalize(requirements),
            "work_item": work_item, "complexity": task["complexity"], "category": task["category"],
            "created_at": time.time(), "operation": operation,
            "reason": "Meet capability and reserve constraints, bound expected work, preserve scarce quota, then apply model preferences",
            "selection_policy": selection_policy,
            "parent_reserved": reserve_main,
            "parent_in_cost_comparison": main in candidates,
            "capability_basis": "model family/catalog description heuristic, not a measured cross-provider quality ranking",
            "scope": "token-volume heuristic; not a claim about billed cost or optimal quality",
            "candidates": [selected] + sorted([c for c in candidates if c is not selected],
              key=lambda c: c["expected_processed_tokens"])[:4], "errors": errors}
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
      if ("writable_roots" not in plan and execution.get("writable_roots")) or (
          "network_access_requested" not in plan
          and (execution.get("network_access_requested") is not None
               or execution.get("permissions") in {"network-shell", "full-access"})
      ):
        raise ValueError("Plan predates parent sandbox controls; obtain a fresh plan")
      if (plan["requirements"] != execution.get("requirements")
          or plan.get("execution_profile") != execution.get("profile")
          or ("permissions" in plan and plan["permissions"] != execution.get("permissions"))
          or ("writable_roots" in plan and plan["writable_roots"] != execution.get("writable_roots", []))
          or ("network_access_requested" in plan
              and plan["network_access_requested"] != execution.get("network_access_requested"))):
        raise ValueError("Dispatch execution requirements/profile differ from this plan; obtain a fresh plan")
    elif execution.get("profile") not in {None, "sandboxed", "file-tools", "workspace-shell"} or any(
        (execution.get("requirements") or {}).get(k) for k in ("gpu", "ssh", "network")):
      raise ValueError("Plan predates execution requirements; obtain a fresh plan")

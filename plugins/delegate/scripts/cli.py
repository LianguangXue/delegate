"""Discoverable parent CLI; explicit protocols remain separate from the MCP stdio stream."""
from pathlib import Path
from typing import Any, Callable
import argparse
import hashlib
import inspect
import json
import os
import shlex
import stat
import sys

import capabilities
import completion
import gc_store
import hosts
from lifecycle import ACTIVE, reconcile
import persistent_tasks
import result_delivery
import runtime_gate
import runtime_image
import storage as st

HELP = """Zero-heredoc cross-host commands (Claude, Codex, Antigravity/agy, DeepSeek/dsh):
  delegate-agent advise "Refactor auth token flow" --host auto --files src/auth.py
  delegate-agent ask codex-review "Audit v0.28 changes" --review-gate --files src/auth.py
  delegate-agent ask agy "Independent architecture review" --blind --workflow dual-plan

JSON protocol (normal delegate tool names and arguments):
  delegate-agent call <<'JSON'
  {"name":"delegate_job","arguments":{"session_id":"SID","action":"result","job_id":"JOB"}}
  JSON
  delegate-agent call --input request.json
  delegate-agent call codex < request.json  # native parent; dsh/antigravity also supported

Full report, no report-body echo or private job-JSON parsing:
  delegate-agent result JOB --session-id SID --output /tmp/review.md
  delegate-agent verify JOB --session-id SID --verdict accepted --evidence "Checked the complete report"

Dirty source builder: use source_inputs=["README.md","doc/PLAN.md"] inside
delegate_run arguments, plus operation="write".
"""

ALL_HOST_CHOICES: list[str] = ["auto", "claude", "codex", "antigravity", "agy", "dsh"]
_SENSITIVE_FALLBACK_TOKENS: tuple[str, ...] = (
  "auth", "login", "token", "secret", "cred", "permission", "acl",
  "billing", "payment", "money", "feishu", "webhook", "publish", "prod",
)


def read_text(name: str | None, maximum: int = 1048576, label: str = "JSON") -> str:
  """Read bounded stdin or a regular file; empty input never produces a JSON traceback."""
  if name is None or name == "-":
    if name is None and sys.stdin.isatty():
      raise ValueError("No JSON input. Use a quoted heredoc or --input request.json; see call --help")
    text = sys.stdin.read(maximum + 1)
  else:
    fd = os.open(Path(name).expanduser(), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
      if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise ValueError("Input must be a regular text file")
      text = stream.read(maximum + 1)
  if len(text) > maximum:
    raise ValueError("CLI input exceeds its bounded text limit")
  if not text.strip():
    if label != "JSON":
      raise ValueError(label + " input is empty")
    raise ValueError(
      'No JSON input. Expected {"name":"delegate_job",'
      '"arguments":{"session_id":"SID","action":"result","job_id":"JOB"}}; see call --help'
    )
  return text


def payload(name: str | None) -> dict[str, Any]:
  """Validate the documented envelope before any dispatch side effects."""
  try:
    value = json.loads(read_text(name))
  except json.JSONDecodeError as exc:
    raise ValueError(f"Invalid JSON input at line {exc.lineno}, column {exc.colno}; see call --help") from None
  if not isinstance(value, dict):
    raise ValueError("CLI input must be a JSON object")
  return value


def resolve_caller_host(host: str | None) -> str:
  """Resolve canonical caller host from CLI ``--host`` or ambient host session variables.

  Args:
    host: Explicit host flag (``auto``, ``claude``, ``codex``, ``antigravity``, ``agy``, ``dsh``) or None.

  Returns:
    Canonical caller host string (``claude``, ``codex``, ``antigravity``, or ``dsh``).
  """
  if host and host != "auto":
    return hosts.HOST_ALIASES.get(host, host)
  existing = os.environ.get("DELEGATE_CALLER_HOST", "").strip()
  if existing and existing != "auto":
    return hosts.HOST_ALIASES.get(existing, existing)
  if os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip() or os.environ.get("ANTIGRAVITY_CLI", "").strip():
    return "antigravity"
  if os.environ.get("CODEX_THREAD_ID", "").strip() or os.environ.get("CODEX_CI", "").strip():
    return "codex"
  if os.environ.get("DSH_SESSION_ID", "").strip() or os.environ.get("DSH_PROFILE", "").strip():
    return "dsh"
  if os.environ.get("CLAUDE_SESSION_ID", "").strip() or os.environ.get("CLAUDECODE", "").strip():
    return "claude"
  return "claude"


def set_host(host: str | None) -> None:
  """Preserve host namespace checking for both legacy and convenience commands."""
  if host:
    os.environ["DELEGATE_CALLER_HOST"] = resolve_caller_host(host)


def _normalize_file_args(raw_files: list[str] | None) -> list[str]:
  """Expand space- or comma-separated ``--files`` arguments into a deduplicated path list."""
  if not raw_files:
    return []
  items: list[str] = []
  for token in raw_files:
    for part in str(token).split(","):
      cleaned = part.strip()
      if cleaned and cleaned not in items:
        items.append(cleaned)
  return items


def _fallback_recommend(
  task: str,
  caller_host: str,
  workflow: str,
  files: list[str],
  estimated_lines: int | None,
  configured_agents: dict[str, Any],
) -> dict[str, Any]:
  """Provide zero-token deterministic routing advice when ``advisory`` module is unavailable."""
  haystack = " ".join([task, *files]).lower()
  sensitive = any(tok in haystack for tok in _SENSITIVE_FALLBACK_TOKENS)
  if estimated_lines is not None and estimated_lines <= 40 and workflow in {"auto", "frontend"}:
    return {
      "action": "keep_inline",
      "workflow": workflow,
      "agent": None,
      "model": None,
      "effort": None,
      "blind_context": False,
      "review_gate": False,
      "max_review_rounds": 2,
      "sensitive_scope": sensitive,
      "reason": "Small bounded edit (<=40 lines); keep inline to avoid delegation handoff overhead.",
    }
  wf = workflow
  if wf == "auto":
    if any(k in haystack for k in ("review", "audit", "gate", "verify")):
      wf = "gate-review"
    elif any(k in haystack for k in ("architecture", "design", "second opinion", "plan")):
      wf = "dual-plan"
    elif any(k in haystack for k in ("copy", "ux", "wording")):
      wf = "copy-ux"
    else:
      wf = "backend-module"
  if wf in {"dual-plan", "copy-ux"}:
    agent = "claude-reviewer" if caller_host == "antigravity" else (
      "agy-advisor" if "agy-advisor" in configured_agents else "codex-reviewer"
    )
    return {
      "action": "dual_opinion" if wf == "dual-plan" else "delegate",
      "workflow": wf,
      "agent": agent,
      "model": "gemini-4-neon-high" if agent.startswith("agy-") else "sonnet",
      "effort": "high",
      "blind_context": wf == "dual-plan",
      "review_gate": False,
      "max_review_rounds": 2,
      "sensitive_scope": sensitive,
      "reason": "Complementary cross-model perspective selected in 0 model tokens.",
    }
  if wf == "gate-review":
    return {
      "action": "delegate",
      "workflow": wf,
      "agent": "codex-reviewer",
      "model": "gpt-6.2-sol",
      "effort": "xhigh" if sensitive else "high",
      "blind_context": False,
      "review_gate": True,
      "max_review_rounds": 2,
      "sensitive_scope": sensitive,
      "reason": "2-tier BLOCKING/ADVISORY pre-merge gate review (max 2 rounds).",
    }
  return {
    "action": "delegate",
    "workflow": wf,
    "agent": "codex-builder" if "codex-builder" in configured_agents else "codex-reviewer",
    "model": "gpt-6.2-sol",
    "effort": "medium",
    "blind_context": False,
    "review_gate": False,
    "max_review_rounds": 2,
    "sensitive_scope": sensitive,
    "reason": "Self-contained implementation module routed to Codex Sol.",
  }


def _recommend(
  task: str,
  caller_host: str,
  workflow: str = "auto",
  files: list[str] | None = None,
  estimated_lines: int | None = None,
) -> dict[str, Any]:
  """Invoke ``advisory.recommend`` lazily with graceful zero-token fallback."""
  cfg_agents = st.config().get("agents", {})
  req_files = files or []
  try:
    import advisory  # type: ignore[import-not-found]
    return advisory.recommend(
      task,
      caller_host=caller_host,
      workflow=workflow,
      required_files=req_files or None,
      estimated_lines=estimated_lines,
      configured_agents=cfg_agents,
    )
  except ImportError:
    return _fallback_recommend(task, caller_host, workflow, req_files, estimated_lines, cfg_agents)


def _fallback_contract_snippet(blind_context: bool = False, review_gate: bool = False) -> str:
  """Build compact contract instructions if ``advisory.contract_snippet`` is not yet loaded."""
  parts: list[str] = []
  if blind_context:
    parts.append(
      "BLIND OPINION: Form an independent plan from raw goal and verified code facts only; "
      "tag unverified assumptions [speculative]."
    )
  if review_gate:
    parts.append(
      "REVIEW GATE (2-tier): Mark BLOCKING only for concrete math/money bugs, data loss, "
      "auth/permission bypass, bad external mutation, or crash with trigger; all else ADVISORY."
    )
  return "\n".join(parts)


def _contract_snippet(workflow: str, blind_context: bool, review_gate: bool) -> str:
  """Return optional blind/review-gate contract snippet with 0 tokens on standard runs."""
  if not blind_context and not review_gate:
    return ""
  try:
    import advisory  # type: ignore[import-not-found]
    return str(
      advisory.contract_snippet(workflow=workflow, blind_context=blind_context, review_gate=review_gate)
    )
  except ImportError:
    return _fallback_contract_snippet(blind_context=blind_context, review_gate=review_gate)


def _format_advise_text(rec: dict[str, Any], caller_host: str, task: str) -> str:
  """Format ``advisory.recommend`` output into a compact 2-3 line summary."""
  action = str(rec.get("action") or "delegate")
  wf = str(rec.get("workflow") or "auto")
  agent = rec.get("agent") or "inline"
  model = rec.get("model") or "-"
  effort = rec.get("effort") or "-"
  tags: list[str] = []
  if rec.get("blind_context"):
    tags.append("blind")
  if rec.get("review_gate"):
    tags.append(f"review-gate(max={rec.get('max_review_rounds', 2)})")
  if rec.get("sensitive_scope"):
    tags.append("sensitive")
  tag_suffix = (" [" + ", ".join(tags) + "]") if tags else ""
  line1 = f"[advise host={caller_host} workflow={wf} action={action}] {agent} {model}/{effort}{tag_suffix}"
  line2 = "reason: " + str(rec.get("reason") or "Deterministic zero-token route.")
  if action == "keep_inline" or not rec.get("agent"):
    line3 = "next: keep inline in current host session (skip worker handoff)"
  elif action == "escalate_to_user":
    line3 = "next: review round cap reached; summarize options and escalate decision to user"
  else:
    flags = " --host " + shlex.quote(caller_host)
    if wf and wf != "auto":
      flags += " --workflow " + shlex.quote(wf)
    if rec.get("blind_context"):
      flags += " --blind"
    if rec.get("review_gate"):
      flags += " --review-gate"
    preview_raw = task.strip().splitlines()[0][:48] if task.strip() else "TASK"
    quoted_agent = shlex.quote(str(rec["agent"]))
    quoted_preview = shlex.quote(preview_raw)
    line3 = f"next: delegate-agent ask {quoted_agent} {quoted_preview}{flags}"
  return "\n".join((line1, line2, line3))


def _ensure_host_session(caller_host: str, explicit_session_id: str | None, resolved_cwd: str) -> str:
  """Register or reuse a deterministic per-host ``session_id`` without manual ``delegate_host``."""
  os.environ["DELEGATE_CALLER_HOST"] = caller_host
  if explicit_session_id:
    sid = st.key(explicit_session_id)
    with st.locked():
      existing = st.read("session-" + sid + ".json")
      if existing is None:
        state = st.session(sid)
        state["host_kind"] = caller_host
        if caller_host == "codex":
          state["parent_provider"] = "codex"
        st.save_session(sid, state)
    hosts.check({"session_id": sid})
    return sid

  cwd_token = "cwd-" + hashlib.sha256(resolved_cwd.encode("utf-8")).hexdigest()[:24]
  if caller_host in hosts.NATIVE_HOSTS:
    env_var = {
      "antigravity": "ANTIGRAVITY_CONVERSATION_ID",
      "codex": "CODEX_THREAD_ID",
      "dsh": "DSH_SESSION_ID",
    }[caller_host]
    native = os.environ.get(env_var, "").strip() or cwd_token
    return str(hosts.connect(native_session_id=native[:200])["session_id"])

  raw_claude = os.environ.get("CLAUDE_SESSION_ID", "").strip()
  if raw_claude:
    try:
      valid_key = st.key(raw_claude)
      existing_claude = st.read("session-" + valid_key + ".json")
      if existing_claude is not None and existing_claude.get("host_kind", "claude") == "claude":
        return valid_key
    except ValueError:
      pass
  native_claude = raw_claude or cwd_token
  sid = "claude-" + hashlib.sha256(native_claude.encode("utf-8")).hexdigest()[:32]
  with st.locked():
    if st.read("session-" + sid + ".json") is None:
      state = st.session(sid)
      state["host_kind"] = "claude"
      st.save_session(sid, state)
  return sid


def _pick_deepseek_agent(cfg: dict[str, Any], write_mode: bool) -> str:
  """Select the best configured DeepSeek/Anthropic-bridge role for ``deepseek`` shorthand."""
  agents = cfg.get("agents", {})
  providers = cfg.get("providers", {})
  desired_ws = "worktree" if write_mode else "read-only"
  candidates: list[tuple[int, str]] = []
  for name, role in agents.items():
    prov_name = role.get("provider", "")
    prov = providers.get(prov_name, {})
    is_ds = "deepseek" in name.lower() or "deepseek" in prov_name.lower() or prov.get("kind") == "anthropic"
    if not is_ds:
      continue
    score = 0
    if role.get("enabled") and prov.get("enabled"):
      score += 4
    elif role.get("enabled"):
      score += 2
    if role.get("workspace") == desired_ws:
      score += 2
    if "deepseek" in name.lower() or "deepseek" in prov_name.lower():
      score += 1
    candidates.append((score, name))
  if candidates:
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]
  return "deepseek-docs"


def _is_auto_eligible(name: str, cfg: dict[str, Any]) -> bool:
  """Return True only when ``name`` is enabled, ``automatic=True``, and its provider is enabled."""
  role = cfg.get("agents", {}).get(name)
  if not isinstance(role, dict) or not role.get("enabled") or not role.get("automatic"):
    return False
  prov = cfg.get("providers", {}).get(role.get("provider", ""))
  return isinstance(prov, dict) and bool(prov.get("enabled"))


def resolve_ask_target(
  target: str,
  cfg: dict[str, Any],
  *,
  caller_host: str,
  task: str,
  workflow: str,
  files: list[str],
  permissions: str,
  estimated_lines: int | None = None,
) -> tuple[str, dict[str, Any] | None]:
  """Map ``delegate-agent ask <target>`` shorthand to a concrete configured agent name.

  Args:
    target: Shorthand alias or exact configured agent name.
    cfg: Current delegate configuration dictionary.
    caller_host: Canonical caller host string.
    task: Task instruction string.
    workflow: Requested advisory workflow name.
    files: Normalized required file list.
    permissions: Normalized permission tier or ``auto``.
    estimated_lines: Optional estimated changed line count for inline break-even check.

  Returns:
    Tuple of ``(agent_name, advisory_recommendation_or_none)``.

  Raises:
    ValueError: If ``target`` is empty, unrecognized, or ``auto`` has no automatic-eligible role.
  """
  agents = cfg.get("agents", {})
  providers = cfg.get("providers", {})
  key = (target or "").strip()
  if not key:
    raise ValueError("ask requires a non-empty target (e.g. codex-review, agy, claude, deepseek, auto)")
  if key in agents:
    return key, None

  norm_perms = capabilities.normalize_permissions(permissions)
  write_mode = norm_perms not in {"auto", "read-only"}
  lowered = key.lower()
  if lowered == "auto":
    rec = _recommend(
      task, caller_host=caller_host, workflow=workflow, files=files, estimated_lines=estimated_lines
    )
    if rec.get("action") in {"keep_inline", "escalate_to_user"}:
      return "", rec
    rec_agent = rec.get("agent")
    if isinstance(rec_agent, str) and _is_auto_eligible(rec_agent, cfg):
      return rec_agent, rec
    desired_ws = "worktree" if write_mode else "read-only"
    preferred = ["codex-builder", "claude-builder"] if write_mode else [
      "codex-reviewer", "claude-explorer"
    ]
    for cand in preferred:
      if _is_auto_eligible(cand, cfg):
        return cand, rec
    for name, role in agents.items():
      if _is_auto_eligible(name, cfg) and role.get("workspace") == desired_ws:
        prov_kind = providers.get(role.get("provider", ""), {}).get("kind")
        if prov_kind != caller_host:
          return name, rec
    for name, role in agents.items():
      if _is_auto_eligible(name, cfg) and role.get("workspace") == desired_ws:
        return name, rec
    for name in agents:
      if _is_auto_eligible(name, cfg):
        return name, rec
    raise ValueError("No enabled automatic-pool agent available for 'ask auto'; specify an explicit target")

  direct_map = {
    "codex": "codex-builder" if write_mode and "codex-builder" in agents else "codex-reviewer",
    "codex-review": "codex-reviewer",
    "codex-build": "codex-builder",
    "agy": "agy-builder" if write_mode and "agy-builder" in agents else "agy-advisor",
    "antigravity": "agy-builder" if write_mode and "agy-builder" in agents else "agy-advisor",
    "gemini": "agy-builder" if write_mode and "agy-builder" in agents else "agy-advisor",
    "agy-review": "agy-advisor",
    "gemini-review": "agy-advisor",
    "agy-build": "agy-builder",
    "gemini-build": "agy-builder",
    "claude": (
      "claude-builder"
      if write_mode and "claude-builder" in agents
      else ("claude-reviewer" if "claude-reviewer" in agents else "claude-explorer")
    ),
    "claude-review": "claude-reviewer",
    "claude-build": "claude-builder",
  }
  if lowered in direct_map:
    mapped = direct_map[lowered]
    if mapped in agents:
      return mapped, None
    if lowered in {"agy", "antigravity", "gemini", "agy-review", "gemini-review", "agy-build", "gemini-build"}:
      for name, role in agents.items():
        if role.get("provider") == "antigravity":
          return name, None
    return mapped, None
  if lowered in {"deepseek", "dsh", "deepseek-review", "deepseek-build"}:
    ds_write = write_mode or lowered == "deepseek-build"
    return _pick_deepseek_agent(cfg, write_mode=ds_write), None
  raise ValueError(
    "Unknown target '" + key + "'; use codex, codex-review, codex-build, agy, gemini, deepseek, "
    "claude, claude-review, claude-build, auto, or an exact configured agent name"
  )


def _resolve_model_effort(
  agent_name: str,
  cfg: dict[str, Any],
  explicit_model: str | None,
  explicit_effort: str | None,
  advisory_rec: dict[str, Any] | None,
  review_gate: bool,
  files: list[str],
  task: str,
) -> tuple[str, str]:
  """Select ``(model, effort)`` preserving explicit CLI flags while handling Antigravity defaults."""
  role = cfg.get("agents", {}).get(agent_name, {})
  provider = cfg.get("providers", {}).get(role.get("provider", ""), {})
  model = (explicit_model or "").strip()
  effort = (explicit_effort or "").strip()
  if advisory_rec and advisory_rec.get("agent") == agent_name:
    if not model and isinstance(advisory_rec.get("model"), str):
      model = advisory_rec["model"]
    if not effort and isinstance(advisory_rec.get("effort"), str):
      effort = advisory_rec["effort"]
  if provider.get("kind") == "antigravity":
    import agy_worker
    cat = agy_worker.catalog(provider)
    if not model or model == "auto":
      model = str(role.get("default_model") or cat.get("default_model") or agy_worker.DEFAULT_MODELS[0])
    if not effort or effort == "auto":
      effort = "high" if role.get("workspace") == "read-only" else "medium"
    return model, effort
  if not model:
    if review_gate and provider.get("kind") == "codex":
      model = "auto"
    else:
      model = "auto"
  if not effort:
    if review_gate and provider.get("kind") == "codex":
      haystack = " ".join([task, *files]).lower()
      effort = "xhigh" if any(tok in haystack for tok in _SENSITIVE_FALLBACK_TOKENS) else "high"
    else:
      effort = "auto"
  return model, effort


def _run_ask(args: argparse.Namespace) -> int:
  """Execute ``delegate-agent ask`` and print ultra-compact worker output."""
  import service
  caller = resolve_caller_host(args.host)
  os.environ["DELEGATE_CALLER_HOST"] = caller
  workdir = str(Path(args.cwd or os.getcwd()).resolve(strict=True))
  sid = _ensure_host_session(caller, args.session_id, workdir)
  files = _normalize_file_args(args.files)
  task_text = read_text("-", 100000, "Task") if args.task == "-" else str(args.task)
  if not task_text.strip():
    raise ValueError("Task text cannot be empty")
  cfg = st.config()
  perms = capabilities.normalize_permissions(args.permissions)
  effective_wf = (
    "gate-review"
    if (args.review_gate and args.workflow == "auto")
    else ("dual-plan" if (args.blind and args.workflow == "auto") else args.workflow)
  )
  agent_name, adv_rec = resolve_ask_target(
    args.target,
    cfg,
    caller_host=caller,
    task=task_text,
    workflow=effective_wf,
    files=files,
    permissions=perms,
    estimated_lines=getattr(args, "lines", None),
  )
  if args.target.strip().lower() == "auto" and (adv_rec or {}).get("action") in {
    "keep_inline", "escalate_to_user"
  }:
    act = str((adv_rec or {}).get("action"))
    wf = str((adv_rec or {}).get("workflow") or effective_wf or "auto")
    reason = str((adv_rec or {}).get("reason") or "Handle inline without spawning a worker.")
    print(f"[ask auto action={act} host={caller} workflow={wf}]")
    print("reason: " + reason)
    return 0
  blind = bool(args.blind or (adv_rec or {}).get("blind_context"))
  gate = bool(args.review_gate or (adv_rec or {}).get("review_gate"))
  model, effort = _resolve_model_effort(
    agent_name, cfg, args.model, args.effort, adv_rec, gate, files, task_text
  )
  role = cfg.get("agents", {}).get(agent_name, {})
  is_write = (
    perms not in {"auto", "read-only"}
    or (perms == "auto" and role.get("workspace") == "worktree")
  )
  acceptance = (
    "Return a 2-tier BLOCKING vs ADVISORY review with file:line evidence."
    if gate
    else "Complete the requested task and report concrete evidence concisely."
  )
  rationale = f"CLI ask ({caller} -> {agent_name}, workflow={effective_wf})"
  starter = getattr(service, "start", None) or service.run
  sig_params = inspect.signature(starter).parameters
  context_text = ""
  call_kwargs: dict[str, Any] = {
    "session_id": sid,
    "agent": agent_name,
    "model": model,
    "effort": effort,
    "task": task_text,
    "acceptance": acceptance,
    "rationale": rationale,
    "cwd": workdir,
    "one_shot": True,
    "operation": "write" if is_write else "read",
    "permissions": perms,
    "required_files": files or None,
    "background": bool(args.background),
  }
  if "workflow" in sig_params:
    call_kwargs["workflow"] = effective_wf
  if "blind_context" in sig_params:
    call_kwargs["blind_context"] = blind
  if "review_gate" in sig_params:
    call_kwargs["review_gate"] = gate
  if (blind or gate) and ("blind_context" not in sig_params or "review_gate" not in sig_params):
    context_text = _contract_snippet(effective_wf, blind, gate)
  call_kwargs["context"] = context_text
  if "escalate" in sig_params and effort == "xhigh":
    call_kwargs["escalate"] = True

  receipt = starter(**call_kwargs)
  if not isinstance(receipt, dict) or "id" not in receipt or ("action" in receipt and "status" not in receipt):
    print(result_delivery.serialize(receipt))
    return 0

  job_id = str(receipt["id"])
  res_agent = str(receipt.get("agent") or agent_name)
  res_model = str(receipt.get("model") or model)
  res_effort = str(receipt.get("effort") or effort)
  if args.background:
    status_str = str(receipt.get("status") or "queued")
    print(f"[{res_agent} {res_model}/{res_effort} {job_id} status={status_str}]")
    print(f"session_id={sid} | collect: delegate-agent result {job_id} --session-id {sid} --host {caller} --all")
    return 0

  job = reconcile(job_id)
  if job["status"] in ACTIVE - {"orphaned"}:
    completion.wait({"session_id": sid, "job_id": job_id, "background": False})
    job = reconcile(job_id)
  if job["status"] != "completed":
    err = str(job.get("error") or ("Job ended with status=" + str(job["status"])))
    raise RuntimeError(f"[{res_agent} {res_model}/{res_effort} {job_id}] {job['status']}: {err}")

  text = str(job.get("result") or "")
  header = f"[{job.get('agent', res_agent)} {job.get('model', res_model)}/{job.get('effort', res_effort)} {job_id}]"
  sys.stdout.write(header + "\n" + text)
  if text and not text.endswith("\n"):
    sys.stdout.write("\n")
  sys.stdout.flush()
  result_delivery.acknowledge(
    job,
    result_delivery.fingerprint(text),
    0,
    len(text),
    len(text),
    source="CLI ask stdout delivery",
  )
  return 0


def guide_path() -> str:
  """Locate only the current installed package's bundled guide; never scan home directories."""
  candidates = [Path(__file__).resolve().parent.parent / "skills/delegate/references/cli.md"]
  if candidates[0].is_file():
    return str(candidates[0])
  try:
    registry = Path.home() / ".claude/plugins/installed_plugins.json"
    fd = os.open(registry, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
      info = os.fstat(stream.fileno())
      if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
        return "Built-in: delegate-agent call --help"
      raw = stream.read(1048577)
    if len(raw) <= 1048576:
      value = json.loads(raw)
      plugins = value.get("plugins", {}) if isinstance(value, dict) else {}
      if isinstance(plugins, dict):
        for index, (name, entries) in enumerate(plugins.items()):
          if index >= 64 or len(candidates) >= 8:
            break
          if name.split("@")[0] == "delegate" and isinstance(entries, list):
            for entry in entries[:16]:
              if (isinstance(entry, dict) and entry.get("version") == runtime_image.VERSION
                  and isinstance(entry.get("installPath"), str) and 0 < len(entry["installPath"]) <= 4096):
                candidates.append(Path(entry["installPath"]) / "skills/delegate/references/cli.md")
                if len(candidates) >= 8:
                  break
  except (OSError, ValueError, TypeError, KeyError):
    pass
  for path in candidates:
    try:
      if path.is_file():
        return str(path)
    except (OSError, ValueError):
      pass
  return "Built-in: delegate-agent call --help"


def parser() -> argparse.ArgumentParser:
  """Build the small user CLI without exposing internal worker/controller modes."""
  root = argparse.ArgumentParser(
    prog="delegate-agent",
    description="Quota-aware delegation CLI",
    epilog=HELP,
    formatter_class=argparse.RawDescriptionHelpFormatter,
  )
  root.add_argument("--version", action="version", version="delegate-agent " + runtime_image.VERSION)
  commands = root.add_subparsers(dest="command")
  host_choices = ["codex", "dsh", "antigravity", "agy"]

  adv = commands.add_parser(
    "advise",
    help="Zero-token dispatch recommendation across Claude, Codex, AGY, and DeepSeek",
  )
  adv.add_argument("task", nargs="?", default="", help="Short task summary (optional)")
  adv.add_argument("--host", choices=ALL_HOST_CHOICES, default="auto", help="Caller host (default: auto)")
  adv.add_argument("--workflow", default="auto", help="Workflow hint (auto, dual-plan, gate-review, ...)")
  adv.add_argument("--files", nargs="*", action="extend", default=[], help="Relevant file paths")
  adv.add_argument("--lines", type=int, default=None, help="Estimated changed line count")
  adv.add_argument("--json", action="store_true", help="Output machine-readable JSON recommendation")

  ask = commands.add_parser(
    "ask",
    help="One-line cross-host dispatch returning only compact header + clean worker output",
  )
  ask.add_argument("target", help="Target shorthand (codex, codex-review, agy, gemini, deepseek, claude, auto)")
  ask.add_argument("task", help="Task prompt for the delegated worker ('-' reads stdin)")
  ask.add_argument("--host", choices=ALL_HOST_CHOICES, default="auto", help="Caller host (default: auto)")
  ask.add_argument("--session-id", default=None, help="Optional explicit delegate session ID")
  ask.add_argument("--cwd", default=None, help="Working directory (default: current directory)")
  ask.add_argument("--model", default=None, help="Explicit model override")
  ask.add_argument("--effort", default=None, help="Explicit reasoning effort override")
  ask.add_argument("--workflow", default="auto", help="Advisory workflow tag")
  ask.add_argument("--blind", action="store_true", help="Enforce blind independent second-opinion contract")
  ask.add_argument("--review-gate", action="store_true", help="Enforce 2-tier BLOCKING vs ADVISORY review gate")
  ask.add_argument("--permissions", default="auto", help="Subagent permission tier (auto, read-only, ...)")
  ask.add_argument("--files", nargs="*", action="extend", default=[], help="Required file paths")
  ask.add_argument("--lines", type=int, default=None, help="Estimated changed line count")
  ask.add_argument("--background", action="store_true", help="Detach immediately instead of waiting for output")

  for name in ("call", "wait", "loop-wait"):
    sub = commands.add_parser(
      name,
      help="JSON request on stdin or --input",
      epilog=HELP,
      formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub.add_argument("host", nargs="?", choices=host_choices)
    sub.add_argument("--input", metavar="FILE", help="JSON request file; - reads stdin")
  for name in ("result", "verify", "status"):
    sub = commands.add_parser(
      name,
      help={
        "result": "Read/export a report",
        "verify": "Record verdict without report echo",
        "status": "Inspect metadata without report text",
      }[name],
    )
    sub.add_argument("job_id", nargs="?" if name == "status" else None)
    sub.add_argument("--session-id", required=True)
    sub.add_argument("--host", choices=["claude", *host_choices])
    if name == "result":
      sub.add_argument(
        "--view",
        choices=["auto", "page", "summary", "file"],
        default="auto",
        help="auto suppresses repeats; file exports a full private report",
      )
      sub.add_argument("--offset", type=int, default=0)
      sub.add_argument("--limit", type=int, default=result_delivery.DEFAULT_LIMIT)
      sub.add_argument("--sha256", default="")
      group = sub.add_mutually_exclusive_group()
      group.add_argument("--output", metavar="FILE", help="Export the FULL UTF-8 report; print only an acknowledgement")
      group.add_argument("--all", action="store_true", help="Explicit full text on stdout; host limits may apply")
      sub.add_argument("--force", action="store_true", help="Explicitly replace an existing export file")
    if name == "verify":
      sub.add_argument("--verdict", choices=["accepted", "needs-work", "blocked"], required=True)
      group = sub.add_mutually_exclusive_group(required=True)
      group.add_argument("--evidence")
      group.add_argument("--evidence-file")
  sub = commands.add_parser("doctor", help="Inspect this CLI's version and capabilities; no provider probes")
  sub.add_argument("--session-id")
  sub.add_argument("--host", choices=["claude", *host_choices])
  sub = commands.add_parser("schema", help="Show the exact current tool input schema")
  sub.add_argument("tool", nargs="?")
  sub = commands.add_parser("gc", help="Inspect or reclaim expired state; dry-run by default")
  sub.add_argument("--apply", action="store_true", help="Delete expired state, orphaned WAL files, and unused runtimes")
  sub.add_argument("--retention-days", type=int, default=14, help="Age in days before terminal artifacts expire")
  return root


def main(
  argv: list[str],
  dispatch: Callable[[str, dict[str, Any]], Any],
  tool_list: Callable[[], list[dict[str, Any]]],
) -> int:
  """Run user commands with clear errors; never turn help/empty input into an MCP stream."""
  root = parser()
  if not argv or argv == ["help"]:
    root.print_help()
    return 0
  args = root.parse_args(argv)
  try:
    if args.command == "advise":
      if os.environ.get("DELEGATE_WORKER"):
        raise ValueError("Delegated workers cannot recursively call delegate")
      caller = resolve_caller_host(args.host)
      os.environ["DELEGATE_CALLER_HOST"] = caller
      task_text = read_text("-", 100000, "Task") if args.task == "-" else str(args.task or "")
      files = _normalize_file_args(args.files)
      rec = _recommend(
        task_text,
        caller_host=caller,
        workflow=args.workflow,
        files=files,
        estimated_lines=args.lines,
      )
      if args.json:
        print(result_delivery.serialize(rec))
      else:
        print(_format_advise_text(rec, caller, task_text))
      return 0
    if args.command == "ask":
      if os.environ.get("DELEGATE_WORKER"):
        raise ValueError("Delegated workers cannot recursively call delegate")
      return _run_ask(args)
    set_host(getattr(args, "host", None))
    if args.command in {"call", "wait", "loop-wait"}:
      value = payload(args.input)
      if args.command == "call":
        if (not isinstance(value.get("name"), str)
            or not isinstance(value.get("arguments", {}), dict)
            or set(value) - {"name", "arguments"}):
          raise ValueError('call expects {"name":"TOOL","arguments":{...}}; see call --help')
        result = dispatch(value["name"], value.get("arguments", {}))
      else:
        hosts.check(value)
        result = completion.wait(value) if args.command == "wait" else persistent_tasks.wait(value)
    elif args.command == "schema":
      rows = tool_list()
      result = next((t for t in rows if t["name"] == args.tool), None) if args.tool else rows
      if result is None:
        raise ValueError("Unknown tool; run schema to list current names")
    elif args.command == "doctor":
      if args.session_id:
        hosts.check({"session_id": args.session_id})
      result = {
        "cli_version": runtime_image.VERSION,
        "scripts": str(Path(__file__).resolve().parent),
        "connected_mcp_version": "unknown: check the CONNECTED MCP delegate_control status server_runtime.version",
        "capabilities": [
          "source_inputs", "required_files", "task_profile", "paged_results", "compact_verify",
          "full_report_export", "persistent_tasks", "source_inputs_base", "advise_cli", "ask_cli",
          *runtime_gate.CAPABILITIES,
        ],
        "input_format": {
          "name": "delegate_job",
          "arguments": {"session_id": args.session_id or "SID", "action": "result", "job_id": "JOB"},
        },
        "help": "delegate-agent advise --help; delegate-agent ask --help; delegate-agent call --help",
        "documentation": guide_path(),
      }
    elif args.command in {"status", "verify", "result"}:
      arguments: dict[str, Any] = {"session_id": args.session_id, "job_id": args.job_id, "action": args.command}
      if args.command == "status" and not args.job_id:
        result = dispatch("delegate_control", {"session_id": args.session_id, "action": "status"})
      elif args.command == "verify":
        evidence = read_text(args.evidence_file, 65536, "Evidence") if args.evidence_file is not None else args.evidence
        result = dispatch("delegate_job", {**arguments, "verdict": args.verdict, "evidence": evidence})
      elif args.command == "result" and (args.output is not None or args.all):
        if args.output is not None and not args.output.strip():
          raise ValueError("--output requires a nonempty report destination; no report was collected")
        hosts.check({"session_id": args.session_id})
        if args.offset or args.sha256 or args.force and not args.output:
          raise ValueError("Full export uses the entire current result; no paging args, and --force requires --output")
        if args.all:
          job = reconcile(args.job_id)
          if job["session_id"] != args.session_id:
            raise ValueError("Job belongs to another session")
          if job["status"] in ACTIVE - {"orphaned"}:
            raise ValueError("Job is still active; use wait before requesting its full terminal report")
          text = job.get("result") or ""
          sys.stdout.write(text)
          sys.stdout.flush()
          if not result_delivery.acknowledge(
            job, result_delivery.fingerprint(text), 0, len(text), len(text), source="CLI explicit full stdout delivery"
          ):
            raise ValueError("A full snapshot was printed, but the current result changed; fetch its new version")
          return 0
        result = result_delivery.export(args.session_id, args.job_id, args.output, args.force)
      else:
        if args.command == "result":
          if args.force:
            raise ValueError("--force requires --output")
          arguments.update(
            result_view=args.view, result_offset=args.offset, result_limit=args.limit, result_sha256=args.sha256
          )
        result = dispatch("delegate_job", arguments)
    elif args.command == "gc":
      result = gc_store.collect_garbage(apply=args.apply, retention_days=args.retention_days)
    else:
      root.print_help()
      return 0
    print(result_delivery.serialize(result))
    return 0
  except Exception as exc:
    print(
      json.dumps(
        {
          "error": str(exc),
          "type": type(exc).__name__,
          "cli_version": runtime_image.VERSION,
          "hint": "delegate-agent --help or delegate-agent call --help; keep the original session_id",
        },
        ensure_ascii=False,
      ),
      file=sys.stderr,
    )
    return 2

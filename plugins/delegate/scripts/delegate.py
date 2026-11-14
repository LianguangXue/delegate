#!/usr/bin/env python3
"""MCP, Claude hook and diagnostic entrypoints for the delegation plugin."""
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
import inspect
import json
import os
import sys
import time
import types
import runtime_image

if __name__ == "__main__":
  runtime_image.bootstrap()

import service
import storage as st
import budget
import accounting
import optimizer
import rewake
import delivery
import handoff
import inspection
import hosts
import exchange
import attempts as at
import capabilities
import completion
import result_delivery
import routing_policy as policy
import persistent_tasks

TOOL_FUNCTIONS = {
  "delegate_loop": persistent_tasks.manage,
  "delegate_host": hosts.connect,
  "delegate_exchange": exchange.exchange,
  "delegate_inspect": inspection.inspect,
  "delegate_handoff": handoff.link,
  "delegate_control": service.control,
  "delegate_catalog": service.catalog,
  "delegate_quota": service.quota,
  "delegate_configure": service.configure,
  "delegate_run": service.run,
  "delegate_job": service.jobs,
  "delegate_budget": budget.report,
  "delegate_task": accounting.task_tool,
  "delegate_plan": optimizer.plan,
}
DESCRIPTIONS = {
  "delegate_loop": "Explicit persistent tasks and bounded loops. start with mode=persistent (parent reviews/advances each step) or loop (automatic bounded repetition, positive token_budget required), run_spec normal task/role/model/effort/cwd/acceptance fields, max_cycles1..100, interval_seconds30..86400. Optional trigger_file skips unchanged inputs; stop_file requires a small JSON done:true receipt and stops scheduling, never accepts the goal. State/conversation/worktree survive MCP exit; recover with the same session_id and loop_id, adopting an existing child rather than replaying it. status/inspect/result/verify/tell supervise the child; pause is after-current, cancel requests child cancellation, advance needs collected/accepted evidence, retry retains the same cycle/work_item. No automatic commits, hidden account switches, unbounded model loops or classifier calls. Foreground task accounting stays separate; inspect loop usage and task_id. Models are invoked only on authorized iterations, not while monitoring. Goal finish requires separate actual evidence.",
  "delegate_host": "Codex/DSH bootstrap: register this conversation once and use returned session_id for all tools. Pass actual native session ID when known; otherwise retain the generated ID. Parent provider/model optional; unknown usage stays unknown. Not for delegated workers.",
  "delegate_exchange": "Quota-first am work protocol: publish/search/get artifacts; watch/poll/ack explicit completion manifests; stage/stage-check evidence; configure/ask/inbox/reply bounded questions. payload is structured data. Never launches or messages another model. project scope required for artifact reads. Questions default disabled, one per am task, no forwarding; recipient must explicitly read/answer.",
  "delegate_inspect": "Check handoff workspace/liveness and evidence freshness; checkpoint explicit code/data files after validation; metrics shows real model/effort outcomes. Never grants lease takeover.",
  "delegate_handoff": "Bind to an existing am task, record next step, sync or report a structured cross-agent handoff. Never claims a lease or marks am tasks done. Actions: bind/next/sync/report.",
  "delegate_task": "Begin/update/finish/report an end-to-end task or compare accepted complete cases. "
    "Task accounting includes parent, native subagents and plugin jobs; finish needs evidence. "
    "update can accept already-inspected completed child IDs via accepted_jobs and evidence without closing or reclassifying the task; "
    "finish supports the same batch acceptance while finishing the task. Classification is frozen after dispatch. "
    "case_key must identify identical frozen inputs/acceptance for baseline vs optimized. "
    "token_budget stops subsequent dispatch after measured usage reaches it, not a hard in-flight token cap.",
  "delegate_plan": "First assess each SUBTASK in this turn with task_profile (kind, scope, uncertainty, risk, validation, optional requires_shell/reason). Omit complexity or use auto to derive it; a supplied complexity is a minimum. Do not copy the whole project's complexity onto every item or run another model just to classify. Fast bounded work prefers Terra/DeepSeek Flash, everyday work Sol, demanding work Astra; supported capability/effort, quotas, roles and retry floors still apply. "
    "Follow assessment.orchestration: resolve unknown interfaces before fanout, assign whole bounded units, independently review high-risk changes. "
    "Model preferences are soft: selection_policy compares expected work and known quota pressure before preferences; inspect candidates for alternatives. "
    "Choose main or a concrete agent/model/effort from task complexity, measured history, "
    "allowed pool and quota reset headroom. Pass subtask complexity/category per work_item; omitted values inherit the same work item or parent. Tiny tasks stay in main. "
    "Use a stable work_item across retries, different IDs for independent stages. Pass returned plan id to "
    "delegate_run(plan_id=...,work_item=...). Plans are estimates, not savings or pricing guarantees.",
  "delegate_budget": "Read fresh Claude/Codex quota evidence and the current routing recommendation. "
    "Only set claude_remaining_percent when the USER explicitly supplies a /usage value; never guess a percentage. "
    "Unknown Claude quota defaults to conserving main capacity in auto mode; off/manual remain unchanged. "
    "Low quota means delegate whole independent stages to allowed external agents, not just reviews. "
    "Native Claude subagents consume the same Claude quota. Model and effort remain per-task choices.",
  "delegate_control": "Read or change THIS session's optional delegation, or run action='gc' for a safe dry-run store garbage-collection inventory. New sessions inherit the configured default; explicit session off overrides it. "
    "Only explicit user on/off/manual requests change the mode; defaults are configured separately on user request. on allows automatic selection from the pool. "
    "Use the real session_id from the delegate hook or skill. Off leaves running jobs intact.",
  "delegate_catalog": "Discover ALL configured agent names and provider mappings. models/efforts belong to the selected provider (default codex); pass agent_name='deepseek-company-reviewer' to resolve its provider directly, or provider_name='deepseek-company'. Codex is live with TTL; "
    "other providers show configured allowlists, not proven inference availability. No inference cost.",
  "delegate_quota": "Read quota/remaining balances and timestamp. Missing Claude Team data is unknown, "
    "not zero usage. API balance and subscription quota are different units. No inference.",
  "delegate_configure": "Show configuration or upsert agent/provider/limits ONLY on user request. "
    "limits.worktree_timeout_seconds configures each workspace Git command (default 600, allowed 10..86400); separate from model runtime. "
    "Never store keys: providers use api_key_env OR bridge_state_dir plus bridge_account. "
    "Bridge accounts remain separate and do not change the selected main account. Changes do not enable session delegation unless the user explicitly changes defaults. "
    "Agents have provider, enabled, automatic, workspace(read-only/worktree), instructions; optional models/efforts allowlists. "
    "Provider kinds: codex, claude, anthropic (Claude CLI with provider-scoped env).",
  "delegate_run": "Before spending inference, read the referenced brief here and declare required_files plus output_files. File-tool reviewers cannot read outside cwd or write reports. File-tool builders need relative input references in their new worktree, with source_inputs for selected drafts. Python/commands need task_profile.requires_shell=true. Prefer report text for read-only analysis; file artifacts require write operation/role. An explicit required_files list is authoritative; omitted input lists use conservative existing-absolute-path discovery, not semantic intent inference. "
    "For long required work pass notify=true: return immediately, retain await_completion=true, and request completion notification. In a persistent Claude session its asyncRewake hook registers a leased waiter; Stop can pause this turn only after that waiter is verified live. Other hosts use the returned managed wait command in their background-task facility; CLI waits make no model calls and return a notice, not collected results. Single-shot/unknown hosts keep the collection guard. background=true is the separate user-authorized opt-out from required collection. Declare actual task execution needs in requirements: gpu/ssh/network booleans, allocated gpu_devices strings, ssh_targets/network_targets host+port (direct TCP probes, not logins or firewall allowlists). execution_profile auto picks sandboxed/network from role grants and never host; explicit host needs an approved worktree role. File-tool workers cannot execute these resources. Checks run under the selected sandbox before inference; unsupported needs fail without model usage. Never drop requirements or escalate profiles to work around failure. timeout_seconds=0 explicitly removes the total wall-clock limit. "
    "Start a background agent with explicit model AND effort, selection rationale, acceptance criteria, "
    "or use agent/model/effort='auto' to classify(category,complexity), plan and dispatch in ONE call. "
    "Before auto selection reason about each work_item here and pass task_profile: kind/scope/uncertainty/risk/validation, optional requires_shell/reason. Omit complexity or use auto for derived difficulty. Task length and keywords alone are not a difficulty analysis. Bounded clear low-risk work prefers Terra/DeepSeek Flash at low effort, ordinary implementation Sol/medium, architecture/high-risk/uncertain work Astra/high. Tests/replay require a shell-capable role. Do not spawn a classifier model; this analysis belongs in the current turn. "
    "Complex auto never falls back below its capability/effort floor. Auto can return action=main/wait/reuse/replan without starting a job. wait_seconds=50 can return completion "
    "without a separate polling round trip. Prefer this combined path for routine substantial work. "
    "and focused context. Allowed only in auto mode or with one_shot=true for an explicit user task request. "
    "Never claim user authorization yourself. Prefer the least costly likely-successful choice, not blindly cheapest. "
    "A fixed agent also supports model='auto' and/or effort='auto': selection stays within that role/provider and its allowlists. operation is read for reviews/analysis, write for edits. No silent fallback; resume_job continues a finished same-role thread with chosen model/effort. "
    "Writes use a detached Git worktree. To include dirty/untracked drafts explicitly pass source_inputs=['README.md','doc/PLAN.md'] with exact repo-root-relative files; only these inputs are snapshotted at worker launch. No ignored files/directories/known credential paths or source edits; without inputs the source must be clean. Read-only sees current files. Return is a job receipt, NOT completion. "
    "Retrieve result and verify every acceptance item. Evidence conflicts must be reported as CONFLICT, not skipped or hidden by changing rules/baselines. Set await_completion=false only for explicitly requested background work. "
    "For user-authorized local commits use allow_commit=true with commit_message and relative commit_paths, on a worktree builder only. Runtime creates a scoped commit after successful execution; no inherited authorization or implicit push/merge. Claude/Anthropic workers have scoped file tools, no shell; Codex can execute tests.",
  "delegate_job": "verify returns a compact acknowledgement, never the report. result_view=auto returns new report text once, then receipts with continuation/reread handles on duplicate calls; page explicitly rereads, summary is metadata only, file exports the FULL Markdown artifact and returns its path/hash without report echo. Prefer file for long reviews, then inspect the artifact before acceptance. Pagination is lossless: follow every non-null next_result_call; full_result_call is a one-call artifact alternative. job_runtime_version is the worker; server_runtime_version is this serving code, not an old job's version. detail controls metadata, not an unbounded text dump. Never accept only a preview. action='watch' arms notifications for an existing job and returns a managed wait command; use its host background facility instead of polling every50s. Waiting/notification never collects or accepts results. action='status' is non-collecting. Extend a RUNNING supported worker with action='extend', extend_seconds=3600; no timeout/resume needed. runtime timeout_seconds sets total from original start;0 removes the limit. Read allowed range/deadline and worker change_state(pending/applied/unconfirmed); wait_seconds up to50 waits for acknowledgement in code. Legacy workers cannot be hot-upgraded or revived after cancellation. inspect lists owned process commands and diagnostics. "
    "Receipts show attempt counting and workspace_setup recovery/retained paths. Proven pre-inference infrastructure failures do not count toward model attempts; unknown/possibly executed requests still count. "
    "List/get results, cancel, steer or verify jobs belonging to this session. "
    "detach lets an explicitly requested background job continue after this turn; archive retries safe archival of a finished owned conversation; it preserves task results and history. "
    "result can wait up to 50 seconds to avoid frequent model polling. tell is live Codex steering; other backends require resume. "
    "Completed execution is NOT acceptance. verify records accepted/needs-work/blocked with actual evidence. "
    "Unresolved acceptance conflicts, skipped checks or disputed baselines require blocked/needs-work and an explicit parent decision, never a silently weakened acceptance. "
    "Do not retry a write until the prior job has stopped and its worktree has been inspected.",
}


def schema(annotation: Any) -> dict[str, Any]:
  """Translate the small supported Python annotation vocabulary into JSON Schema."""
  origin = get_origin(annotation)
  if origin is types.UnionType:
    return {"anyOf": [schema(item) for item in get_args(annotation)]}
  if origin is list:
    return {"type": "array", "items": schema(get_args(annotation)[0])}
  if origin is dict:
    return {"type": "object", "additionalProperties": True}
  return {"type": {str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}[annotation]}


def tool_list() -> list[dict[str, Any]]:
  """Publish tool schemas directly from the checked service signatures."""
  result = []
  for name, function in TOOL_FUNCTIONS.items():
    hints = get_type_hints(function)
    properties = {}
    required = []
    for param in inspect.signature(function).parameters.values():
      properties[param.name] = schema(hints[param.name])
      if param.default is inspect.Parameter.empty:
        required.append(param.name)
    if "requirements" in properties:
      properties["requirements"] = capabilities.input_schema()
    if "execution_profile" in properties:
      properties["execution_profile"] = {"type":"string","enum":["auto",*sorted(capabilities.PROFILES)]}
    if "operation" in properties:
      properties["operation"] = {"type": "string", "enum": ["read", "write"], "default": "read",
        "description": "read = review/analyze without edits; write = edit in an isolated worktree. Not a task description."}
    if "source_inputs" in properties:
      properties["source_inputs"] = {"anyOf": [{"type": "null"}, {"type": "array", "minItems": 1,
        "maxItems": 64, "items": {"type": "string", "maxLength": 1024}}],
        "description": "Optional exact repository-root-relative draft files for a builder snapshot; 1 MiB/file, 8 MiB total. No directory copying."}
    if "notify" in properties:
      properties["notify"] = {"type": "boolean", "default": False,
        "description": "Return immediately and notify on completion while keeping the result required; differs from background=true."}
    if "task_profile" in properties:
      properties["task_profile"] = policy.input_schema()
      properties["task_profile"]["description"] = "Reason about this subtask once: kind, scope, uncertainty, correctness risk and validation; server derives difficulty and checks tools. No extra classifier inference."
    for field in ("required_files", "output_files"):
      if field in properties:
        properties[field] = {"anyOf": [{"type": "null"}, {"type": "array", "maxItems": 32,
          "items": {"type": "string", "maxLength": 4096}}],
          "description": "Exact file paths relative to worker cwd or absolute; at most8192 path characters total. Inputs must be readable; outputs require a writable role and stay inside worker cwd."}
    if "complexity" in properties and name in {"delegate_run", "delegate_plan"}:
      properties["complexity"] = {"type": "string", "enum": ["", "auto", "trivial", "simple", "standard", "complex"],
        "description": "Prefer auto/omitted with task_profile; a supplied level is a minimum and cannot weaken a high-risk assessment."}
    if "result_limit" in properties:
      properties["result_limit"] = {"type": "integer", "minimum": 1, "maximum": result_delivery.MAX_LIMIT,
        "default": result_delivery.DEFAULT_LIMIT, "description": "Requested result page size; UTF-8 wire budget may shorten a page."}
      properties["result_offset"] = {"type": "integer", "minimum": 0, "default": 0,
        "description": "Character offset from next_result_call; pass the matching result_sha256 for nonzero offsets."}
      properties["result_view"] = {"type": "string", "enum": ["auto", "page", "summary", "file"], "default": "auto",
        "description": "auto suppresses duplicate body; page explicitly rereads; summary never collects; file publishes the entire Markdown report without truncation or body echo."}
    if name == "delegate_control":
      properties["action"] = {"type": "string", "enum": ["status", "on", "off", "manual", "gc"], "default": "status"}
    if name == "delegate_loop":
      properties["action"] = {"type": "string", "enum": ["list", "start", "status", "watch", "inspect", "result",
        "verify", "tell", "pause", "cancel", "resume", "recover", "advance", "retry", "limits", "finish"]}
      properties["mode"] = {"type": "string", "enum": ["persistent", "loop"], "default": "persistent"}
      properties["run_spec"]["description"] = "Normal delegate_run task fields: agent/model/effort/task/acceptance/rationale/cwd, optional profile/resources/inputs. No session, resume, work_item or commit overrides. Each step is separate from goal acceptance."
      properties["token_budget"]["description"] = "Observed worker-token admission budget, not a hard in-flight or billing cap. Positive for automatic loop. Unknown usage blocks more inference."
    if name == "delegate_task":
      properties["action"] = {"type": "string", "enum": ["begin", "update", "finish", "report", "list", "compare"]}
      properties["accepted_jobs"]["description"] = (
        "update or finish only: successfully completed child IDs already inspected by the parent; evidence required. "
        "update records child acceptance without closing/reclassifying the parent or marking unseen reports collected.")
    result.append({"name": name, "description": DESCRIPTIONS[name],
                   "inputSchema": {"type": "object", "properties": properties,
                                   "required": required, "additionalProperties": False}})
  return result


def dispatch(name: str, arguments: dict[str, Any]) -> Any:
  """Call one named operation; unknown argument names fail before execution."""
  if name not in TOOL_FUNCTIONS:
    raise ValueError("Unknown tool")
  hosts.check(arguments)
  if name == "delegate_budget" and os.environ.get("DELEGATE_CALLER_HOST") in {"codex", "dsh"}:
    if arguments.get("claude_remaining_percent") is not None:
      raise ValueError("A non-Claude parent cannot report its quota as Claude quota")
    state = st.session(arguments["session_id"])
    provider = state.get("parent_provider")
    return {"host": state.get("host_kind"), "parent_provider": provider,
            "parent_quota": service.quota(provider, session_id=arguments["session_id"]) if provider else {"status":"unknown"},
            "parent_token_meter": "unavailable; use native host metrics"}
  return TOOL_FUNCTIONS[name](**arguments)


def stop_feedback(session_id: str, stop_hook_active: bool) -> str:
  """Continue a finishing turn while its required plugin jobs still need collection."""
  state = st.session(session_id)
  task_id = state.get("active_task")
  if not task_id:
    return ""
  task = accounting.load(task_id)
  pending = []
  for job_id in task["jobs"]:
    value = service.reconcile(job_id)
    if value.get("await_completion", True) and not value.get("collected_at"):
      if value["status"] in service.ACTIVE - {"orphaned"} and completion.defers_stop(value):
        continue  # Required task remains open; an owned waiter will report completion.
      pending.append((job_id, value["status"], value.get("wait_count", 0),
                      (value.get("result_delivery") or {}).get("ranges", [])))
  if not pending:
    return ""
  signature = json.dumps(pending, separators=(",", ":"))
  with st.locked():
    state = st.session(session_id)
    guard = state.get("stop_feedback_guard", {})
    count = guard.get("count", 0) if guard.get("task_id") == task_id else 0
    if count >= 3:
      return ""  # Hard bound survives old servers and hosts resetting stop_hook_active.
    if stop_hook_active and state.get("stop_feedback_signature") == signature:
      return ""  # The agent made no progress; do not force a continuation loop.
    state["stop_feedback_guard"] = {"task_id": task_id, "count": count + 1}
    state["stop_feedback_signature"] = signature
    st.save_session(session_id, state)
  items = ", ".join(job_id + " (" + status + ")" for job_id, status, *_ in pending)
  return ("Required delegate results remain uncollected: " + items + ". "
          "For terminal jobs call delegate_job(action='result') and follow next_result_call until result_delivery_complete. "
          "For running jobs arm delegate_job(action='watch') and use its managed wait command in the host background facility; "
          "once an owned live waiter is registered, notify mode allows this turn to pause without marking the job complete. "
          "A foreground managed waiter can wait beyond 50 seconds without model polling. For orphaned work inspect and cancel it. "
          "Inspect terminal output and validate it "
          "before reporting completion. If the user explicitly wants background execution, use "
          "await_completion=false when dispatching or delegate_job(action='detach') for an existing job.")


def hook(payload: dict[str, Any]) -> dict[str, Any]:
  """Expose session scope and completed jobs without any model or quota request."""
  if os.environ.get("DELEGATE_WORKER"):
    return {}
  session_id = payload.get("session_id")
  if not isinstance(session_id, str) or not session_id:
    return {}
  try:
    st.key(session_id)
  except ValueError:
    return {}
  event = payload.get("hook_event_name", "SessionStart")
  if event == "PreToolUse":
    import cli_history
    try:
      cli_history.prepare_resume(payload)
    except (ValueError, OSError) as exc:
      return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason": "Cannot safely restore delegate history: " + str(exc)}}
    return {}
  rewritten_output = rewake.compact_echo(payload)
  rewake.observe_collection(payload)
  migration_notice = rewake.runtime_notice(payload)
  if event == "Stop" and not payload.get("agent_id"):
    feedback = stop_feedback(session_id, bool(payload.get("stop_hook_active")))
    if feedback:
      # Capture this response without closing the task before the continuation.
      accounting.hook({**payload, "hook_event_name": "PostToolUse"})
      return {"decision": "block", "reason": feedback}
  accounting.hook(payload)
  if not payload.get("agent_id") and (event in {"SessionStart", "Stop", "StopFailure"}
      or (event == "PostToolUse" and str(payload.get("tool_name", "")).startswith("mcp__")
          and "delegate" in str(payload.get("tool_name", "")))):
    handoff.sync(session_id)
  if event in {"Stop", "StopFailure", "SubagentStart", "SubagentStop"} or payload.get("agent_id"):
    return {}  # Metering must never extend Stop or inject parent instructions into children.
  state = st.session(session_id)
  notes = []
  if migration_notice:
    notes.append(migration_notice)
  if event == "SessionStart":
    for lid in state.get("loops", [])[-50:]:
      loop_note = persistent_tasks.notice(persistent_tasks.load(lid), force=True)
      if loop_note:
        notes.append(loop_note)
  legacy = next((jid for jid in reversed(state["jobs"][-10:]) if at.legacy_preflight(st.job(jid))
                 and str(st.job(jid).get("error", "")).startswith("Legacy parent")), None)
  if legacy:
    with st.locked():
      current = st.session(session_id)
      if current.get("legacy_recovery_notice") != legacy:
        current["legacy_recovery_notice"] = legacy
        st.save_session(session_id, current)
        notes.append("A delegate job was rejected before inference because its MCP parent predates frozen runtimes. "
          "Do not repeatedly dispatch through that same server. /reload-plugins may defer MCP changes to the next session. "
          "Use the installed delegate-agent call CLI with JSON name/arguments and the SAME session_id=" + session_id +
          ", or start a new session. The CLI retains current role, mode, quota and work-item checks. "
          "Proven pre-inference failures do not consume the current release's model-attempt budget; "
          "parent coordination still consumes usage. No failed task is replayed automatically.")
  task = accounting.load(state["active_task"]) if state.get("active_task") else None
  if state["mode"] == "auto" and task and task["complexity"] == "trivial" and not task["jobs"]:
    if event in {"SessionStart", "UserPromptSubmit"}:
      return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
        "Delegate session_id=" + session_id + ". This task is classified trivial: handle it directly; "
        "avoid planning/worker startup overhead. Usage is metered locally."}}
    return {}
  if event == "SessionStart" or (event == "UserPromptSubmit" and state["mode"] == "auto"):
    notes.append("Delegate session_id=" + session_id + "; mode=" + state["mode"] +
                 "; strategy=" + state["strategy"] + "; allowed agents=" + json.dumps(state["agents"]) + ".")
    if state["mode"] == "auto":
      notes.append("Delegation is enabled for this session; use it directly for substantial tasks without requesting activation again. A task usage ledger tracks this turn. First check the connected MCP delegate_control status server_runtime.version once per conversation; "
        "this hook is version " + runtime_image.VERSION + ", which does not prove the connected MCP is current. "
        "If older or missing, use the installed delegate-agent call CLI with the same session_id, or a new session. "
        "Read referenced briefs before routing; declare required_files/output_files and shell needs. For dirty code "
        "use selected source_inputs; for read-only analysis return the report text rather than requesting file writes. "
        "For substantial independent work first reason about EACH subtask's kind/scope/uncertainty/risk/validation in this turn and pass task_profile; "
        "do not ask another model to classify or label all children with the overall project's difficulty. "
        "Use ONE delegate_run(agent/model/effort='auto',category,task_profile,operation,work_item,notify=true) "
        "with complexity omitted/auto to classify, plan, dispatch and wait; it may choose main instead. "
        "Follow task_assessment.orchestration: resolve uncertain interfaces before implementation; parallelize only ready independent units with disjoint writes; "
        "use targeted independent review for high-risk changes. Model preferences are soft; preserve scarce quota within the expected-work bound. "
        "Bounded fast work prefers Terra/DeepSeek Flash, daily implementation Sol, demanding or high-risk reasoning Astra. "
        "Tests/replay require shell-capable roles. Avoid separate planning/polling calls "
        "unless needed. Tiny tasks should stay in main with no planning calls. Resume failed work rather than "
        "copying context. After inspecting actual results call delegate_task finish with accepted_jobs and evidence "
        "to combine child and parent acceptance. Stop records final-response usage. "
        "No unsupported claim of savings: delegate_task compare requires matched accepted complete cases.")
    else:
      notes.append("External delegation is opt-in. Do not start automatically. Use /delegate:delegate "
                   "for on/off/manual or an explicit one-shot task. Native Claude workflow remains available.")
  completed = []
  if state["mode"] == "auto":
    budget.schedule_refresh()
    signature, text = budget.notice(session_id)
    if event in {"SessionStart", "UserPromptSubmit"} or state.get("routing_signature") != signature:
      notes.append(text)
      with st.locked():
        current = st.session(session_id)
        current["routing_signature"] = signature
        st.save_session(session_id, current)
  candidates = list(state["pending_jobs"]) if "pending_jobs" in state else list(state["jobs"])
  settled: set[str] = set()
  for job_id in candidates:
    reconciled = service.reconcile(job_id)
    note = delivery.notice(job_id, force=event == "SessionStart")
    if note:
      notes.append(note)
    if reconciled["status"] not in service.ACTIVE and reconciled.get("collected_at"):
      settled.add(job_id)
  if settled and "pending_jobs" in state:
    with st.locked():
      current = st.session(session_id)
      if "pending_jobs" in current:
        current["pending_jobs"] = [jid for jid in current["pending_jobs"] if jid not in settled]
        st.save_session(session_id, current)
  if not notes and rewritten_output is None:
    return {}
  output = {"hookEventName": event}
  if notes:
    output["additionalContext"] = "\n".join(notes)
  if rewritten_output is not None:
    output["updatedMCPToolOutput"] = rewritten_output
  return {"hookSpecificOutput": output}


def statusline(payload: dict[str, Any]) -> str:
  """Collect real quota and show the enabled session's routing pressure without inference."""
  raw_sid = payload.get("session_id")
  sid = None
  if isinstance(raw_sid, str) and raw_sid:
    try:
      sid = st.key(raw_sid)
    except ValueError:
      sid = None
  safe_payload = payload if sid == raw_sid else {k: v for k, v in payload.items() if k != "session_id"}
  sample = budget.capture(safe_payload)
  if sid:
    sample = budget.read_claude(sid)
  value = budget.remaining(sample, "claude")
  label = "unknown" if value is None else str(round(value, 1)) + "% left"
  if sid and st.session(sid)["mode"] == "auto":
    return "Delegate: " + budget.recommendation(sid)["mode"] + " | Claude quota: " + label
  return "Claude quota: " + label


def mcp() -> None:
  """Serve the small standard MCP stdio JSON-RPC surface without dependencies."""
  for line in sys.stdin:
    try:
      request = json.loads(line)
      if not isinstance(request, dict):
        raise ValueError("JSON-RPC request must be an object")
      if "id" not in request:
        continue
      method = request.get("method")
      params = request.get("params", {})
      if not isinstance(params, dict):
        raise ValueError("JSON-RPC params must be an object")
      if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "delegate", "version": runtime_image.VERSION}}
        if os.environ.get("DELEGATE_CALLER_HOST") in {"codex", "dsh"}:
          result["instructions"] = "Call delegate_host once per conversation; retain its session_id for all tools. Collect/validate every required result. Parent usage is not metered; no Claude idle hook exists here. Delegated workers cannot recursively delegate."
      elif method == "ping":
        result = {}
      elif method == "tools/list":
        result = {"tools": [] if os.environ.get("DELEGATE_WORKER") else tool_list()}
      elif method == "tools/call":
        try:
          value = dispatch(params["name"], params.get("arguments", {}))
          rendered = result_delivery.wire(value)
          result = {"content": [{"type": "text", "text": rendered}]}
        except Exception as exc:
          result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
      else:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                          "error": {"code": -32601, "message": "Method not found"}}), flush=True)
        continue
      print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    except (ValueError, TypeError) as exc:
      print(json.dumps({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": str(exc)}}), flush=True)


def main() -> None:
  """Select a transport or a JSON-on-stdin diagnostic operation."""
  os.umask(0o077)
  arguments = sys.argv[1:]
  cli_frontend = bool(arguments and arguments[0] == "cli")
  if cli_frontend:
    arguments = arguments[1:]
    sys.argv = [sys.argv[0], *arguments]
  internal = {"mcp", "host-mcp", "hook", "statusline", "loop-worker"}
  if ((cli_frontend and (not arguments or arguments[0] not in internal)) or
      (arguments and arguments[0] in {"call", "wait", "loop-wait", "--help", "-h", "help", "--version",
                                    "result", "verify", "status", "doctor", "schema"})):
    import cli
    sys.exit(cli.main(arguments, dispatch, tool_list))
  mode = sys.argv[1] if len(sys.argv) > 1 else "mcp"
  if mode in {"wait", "call", "loop-wait"} and len(sys.argv) == 3 and sys.argv[2] in {"codex", "dsh"}:
    os.environ["DELEGATE_CALLER_HOST"] = sys.argv[2]
  if mode == "host-mcp":
    if len(sys.argv) != 3 or sys.argv[2] not in {"codex", "dsh"}:
      raise ValueError("Use host-mcp codex|dsh")
    os.environ["DELEGATE_CALLER_HOST"] = sys.argv[2]
    mcp()
    return
  if mode == "loop-worker":
    if len(sys.argv) != 3:
      raise ValueError("Use loop-worker with one persistent-task ID")
    persistent_tasks.run(sys.argv[2])
    return
  if mode == "mcp":
    mcp()
    return
  payload = json.load(sys.stdin)
  if mode == "loop-wait":
    hosts.check(payload)
    print(json.dumps(persistent_tasks.wait(payload), ensure_ascii=False))
    return
  if mode == "wait":
    hosts.check(payload)
    print(json.dumps(completion.wait(payload), ensure_ascii=False))
    return
  if mode == "hook":
    print(json.dumps(hook(payload)))
  elif mode == "statusline":
    print(statusline(payload))
  elif mode == "call":
    print(json.dumps(dispatch(payload["name"], payload.get("arguments", {})), ensure_ascii=False, indent=2))
  else:
    raise ValueError("Use mcp, hook, statusline or call")


if __name__ == "__main__":
  main()

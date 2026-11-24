#!/usr/bin/env python3
"""MCP, Claude hook and diagnostic entrypoints for the delegation plugin."""
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
import inspect
import json
import os
import sys
import threading
import time
import types
import mcp_reload
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
import runtime_gate

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
  "delegate_plan": service.plan_task,
}
DESCRIPTIONS = {
  "delegate_loop": (
    "Persistent tasks/loops: mode=persistent stops each cycle for parent verification; "
    "mode=loop requires token_budget>0 and max_cycles 1..100. Survives MCP exit."
  ),
  "delegate_host": (
    "Codex/DSH/Antigravity one-time conversation registration to obtain session_id "
    "for all delegate tools."
  ),
  "delegate_exchange": (
    "Quota-first am work protocol (no model calls): publish/search/get artifacts, "
    "watch/poll/ack receipts, stage evidence, bounded Q&A."
  ),
  "delegate_inspect": (
    "Check workspace/liveness/checkpoint freshness, checkpoint validated files, "
    "or read outcome metrics (never transfers am leases)."
  ),
  "delegate_handoff": (
    "Bind session/task scope to an am task, set next_step, "
    "or sync/report cross-agent handoffs (bind/next/sync/report)."
  ),
  "delegate_task": (
    "Begin/update/finish/report task usage ledger or compare frozen case_key runs. "
    "accepted_jobs records inspected child acceptance; classification freezes after dispatch."
  ),
  "delegate_plan": (
    "Pre-dispatch route/effort/quota planner. Pass per-subtask task_profile "
    "(kind, scope, uncertainty, risk, validation, optional reasoning/requires_shell/reason) "
    "with complexity omitted/auto. DeepSeek defaults to deepseek-flash; reserve Codex astra "
    "strictly for complex tasks. Supports permissions, writable_roots, and network_access."
  ),
  "delegate_budget": (
    "Read cached Claude/Codex quota pressure and routing advice. "
    "Set claude_remaining_percent only from explicit user /usage."
  ),
  "delegate_control": (
    "Read/set session delegation mode (status/on/off/manual) or run gc dry-run inventory."
  ),
  "delegate_catalog": (
    "List all configured agents and one provider's models/efforts "
    "(default codex; pass agent_name or provider_name) plus timeout bounds."
  ),
  "delegate_quota": "Read provider quota/balance and timestamp without inference.",
  "delegate_configure": (
    "Show or update agent/provider/limits/defaults/model_tiers/model_policy "
    "ONLY on user request (never store raw secrets)."
  ),
  "delegate_run": (
    "Dispatch a bounded worker (agent/model/effort explicit or 'auto' to classify, plan, and run). "
    "DeepSeek defaults to deepseek-flash (avoid v4-pro unless requested). On Codex, use terra (simple) "
    "or sol medium/high (standard); reserve astra STRICTLY for genuinely complex tasks (architecture, "
    "system-wide high risk, or unverified high risk/uncertainty) with escalate=true under sol-first. "
    "Pass task_profile, omit/auto complexity, declare required_files/output_files/requirements, and set "
    "permissions ('auto'|'read-only'|'workspace-write'|'workspace-shell'|'network-shell'|'full-access'), "
    "writable_roots, network_access. Dirty Git repos need source_inputs/snapshot_required_files=true. "
    "Prefer notify=true for multi-minute work, resume_job for fixes; verify output with evidence."
  ),
  "delegate_job": (
    "Manage jobs (list/status/result/watch/inspect/runtime/extend/tell/cancel/detach/archive/verify). "
    "result_view=auto avoids duplicate echoes; file exports full Markdown report. "
    "Prefer watch+waiter over repeated 50s polling; "
    "check inspect/recovery_advice before retrying; verify records accepted/needs-work/blocked with evidence."
  ),
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
    if name == "delegate_host" and hosts.caller_host() not in hosts.NATIVE_HOSTS:
      continue
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
    if "permissions" in properties:
      properties["permissions"] = {
        "type": "string",
        "enum": capabilities.PERMISSION_CHOICES,
        "default": "auto",
        "description": (
          "Subagent permission tier: auto, read-only, workspace-write, workspace-shell, "
          "network-shell (cwd+networkAccess=true), or full-access (dangerFullAccess/unrestricted)."
        ),
      }
    if "writable_roots" in properties:
      properties["writable_roots"] = {
        "anyOf": [
          {"type": "null"},
          {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 4096}},
        ],
        "description": "Extra existing directories writable outside cwd (e.g. shared data dir).",
      }
    if "network_access" in properties:
      properties["network_access"] = {
        "anyOf": [{"type": "null"}, {"type": "boolean"}],
        "description": "Explicitly enable/disable sandbox network access (e.g. GitLab/HTTP/SSH).",
      }
    if "source_inputs_base" in properties:
      properties["source_inputs_base"] = {"anyOf": [{"type":"null"}, {"type":"string", "enum":["repo", "cwd"]}],
        "description":"Draft path base (default repo; resume retains original base)."}
      properties["snapshot_required_files"]["description"] = (
        "Snapshot declared dirty/untracked repo required_files into worktree."
      )
    if "model" in properties and name == "delegate_run":
      properties["model"]["description"] = (
        "Model ID or 'auto'. DeepSeek defaults to deepseek-flash; reserve Codex astra strictly for complex tasks."
      )
    if "escalate" in properties:
      properties["escalate"]["description"] = (
        "Authorize complex-only Astra escalation under sol-first (or DeepSeek v4-pro)."
      )
    if "source_inputs" in properties:
      properties["source_inputs"] = {"anyOf": [{"type": "null"}, {"type": "array", "minItems": 1,
        "maxItems": 64, "items": {"type": "string", "maxLength": 1024}}],
        "description": "Relative draft files to snapshot into builder worktree (<=1MiB/file, <=8MiB total)."}
    if "notify" in properties:
      properties["notify"] = {"type": "boolean", "default": False,
        "description": "Return immediately and notify on completion while keeping result required."}
    if "task_profile" in properties:
      properties["task_profile"] = policy.input_schema()
      properties["task_profile"]["description"] = (
        "Subtask assessment: kind, scope, uncertainty, risk, validation, optional reasoning/requires_shell/reason. "
        "Do not inflate risk/scope on routine work; reserve complex/Astra for architecture or high risk."
      )
    for field in ("required_files", "output_files"):
      if field in properties:
        properties[field] = {
          "anyOf": [
            {"type": "null"},
            {"type": "array", "maxItems": 32, "items": {"type": "string", "maxLength": 4096}},
          ],
          "description": "File paths; outputs allow cwd, writable_roots, or full-access.",
        }
    if "complexity" in properties and name in {"delegate_run", "delegate_plan"}:
      properties["complexity"] = {"type": "string", "enum": ["", "auto", "trivial", "simple", "standard", "complex"],
        "description": "Prefer auto/omitted with task_profile; explicit level acts as a minimum."}
    if "workflow" in properties:
      properties["workflow"] = {
        "type": "string",
        "enum": list(service._workflows()),
        "default": "auto",
        "description": "Workflow preset (auto/dual-plan/copy-ux/backend-module/gate-review/bulk-cheap).",
      }
    if "blind_context" in properties:
      properties["blind_context"] = {
        "type": "boolean",
        "default": False,
        "description": "Blind second opinion: raw user intent + facts only, no parent draft.",
      }
    if "review_gate" in properties:
      properties["review_gate"] = {
        "type": "boolean",
        "default": False,
        "description": "2-tier BLOCKING vs ADVISORY review rubric (max 2 rounds).",
      }
    if "estimated_lines" in properties:
      properties["estimated_lines"] = {
        "anyOf": [{"type": "null"}, {"type": "integer", "minimum": 0}],
        "description": "Estimated diff lines (<=40 favors inline).",
      }
    if "result_limit" in properties:
      properties["result_limit"] = {"type": "integer", "minimum": 1, "maximum": result_delivery.MAX_LIMIT,
        "default": result_delivery.DEFAULT_LIMIT, "description": "Page size (bounded by wire budget)."}
      properties["result_offset"] = {"type": "integer", "minimum": 0, "default": 0,
        "description": "Character offset from next_result_call (with matching result_sha256)."}
      properties["result_view"] = {
        "type": "string",
        "enum": ["auto", "page", "summary", "file"],
        "default": "auto",
        "description": (
          "auto avoids repeat echoes; page rereads; summary metadata only; "
          "file exports full Markdown path."
        ),
      }
    if name == "delegate_control":
      properties["action"] = {"type": "string", "enum": ["status", "on", "off", "manual", "gc"], "default": "status"}
    if name == "delegate_loop":
      properties["action"] = {"type": "string", "enum": ["list", "start", "status", "watch", "inspect", "result",
        "verify", "tell", "pause", "cancel", "resume", "recover", "advance", "retry", "limits", "finish"]}
      properties["mode"] = {"type": "string", "enum": ["persistent", "loop"], "default": "persistent"}
      properties["run_spec"]["description"] = (
        "delegate_run task fields "
        "(agent/model/effort/task/acceptance/rationale/cwd/permissions/inputs)."
      )
      properties["token_budget"]["description"] = "Observed-token admission gate (>0 required for loop mode)."
    if name == "delegate_run":
      properties["work_item"]["description"] = "Omit for deterministic intent key, or pass stable ID for retries."
    if name == "delegate_task":
      properties["action"] = {
        "type": "string",
        "enum": ["begin", "update", "finish", "report", "list", "compare", "resolve-native"],
      }
      properties["accepted_jobs"]["description"] = "Inspected completed child IDs to mark accepted (evidence required)."
    result.append({"name": name, "description": DESCRIPTIONS[name],
                   "inputSchema": {"type": "object", "properties": properties,
                                   "required": required, "additionalProperties": False}})
  return result


def dispatch(name: str, arguments: dict[str, Any]) -> Any:
  """Call one named operation; unknown argument names fail before execution."""
  if name not in TOOL_FUNCTIONS:
    raise ValueError("Unknown tool")
  hosts.check(arguments)
  if name == "delegate_budget" and hosts.caller_host() in hosts.NATIVE_HOSTS:
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
    rejection = runtime_gate.rejection(payload)
    if rejection:
      return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": rejection}}
    import cli_history
    try:
      cli_history.prepare_resume(payload)
    except (ValueError, OSError) as exc:
      return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason": "Cannot safely restore delegate history: " + str(exc)}}
    return {}
  runtime_gate.reset(payload)
  runtime_gate.observe(payload)
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
          "Do not repeatedly dispatch through that same server (>=0.26.8 hot-reloads in place; older servers need "
          "/reload-plugins --force or delegate-agent call with SAME session_id=" + session_id +
          "). The CLI retains current role, mode, quota and work-item checks. "
          "Proven pre-inference failures do not consume the current release's model-attempt budget; "
          "parent coordination still consumes usage. No failed task is replayed automatically.")
  handshakes = state.get("runtime_handshakes")
  if (event == "UserPromptSubmit" and isinstance(handshakes, dict)
      and any(isinstance(rec, dict) and not runtime_gate.live_identity(rec.get("identity"))
              for rec in handshakes.values())):
    notes.append(
      "Delegate MCP server restarted (e.g. via /reload-plugins): call delegate_control(action='status') "
      "once before delegate_run to refresh the session handshake (no session restart needed)."
    )
  task = accounting.load(state["active_task"]) if state.get("active_task") else None
  if state["mode"] == "auto" and task and task["complexity"] == "trivial" and not task["jobs"]:
    if event in {"SessionStart", "UserPromptSubmit"}:
      return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
        "Delegate session_id=" + session_id + ". This task is classified trivial: handle it directly; "
        "avoid planning/worker startup overhead. Usage is metered locally."}}
    return {}
  context_sig = json.dumps([
    runtime_image.VERSION, state["mode"], state.get("mode_revision", 0), state["strategy"], state["agents"]
  ], separators=(",", ":"))
  needs_full_context = (
    event == "SessionStart"
    or (event == "UserPromptSubmit" and state.get("hook_context_signature") != context_sig)
  )
  if needs_full_context:
    notes.append("Delegate session_id=" + session_id + "; mode=" + state["mode"] +
                 "; strategy=" + state["strategy"] + "; allowed agents=" + json.dumps(state["agents"]) + ".")
    if state["mode"] == "auto":
      notes.append(
        "Delegation is enabled for this session; use it directly for substantial tasks without requesting "
        "activation again. A task usage ledger tracks this turn. Check connected MCP delegate_control status "
        "server_runtime.version once per conversation; this hook is v"
        + runtime_image.VERSION
        + " (use installed delegate-agent call with SAME session_id if older). "
        + "Before routing, inspect referenced briefs, declare required_files/output_files/requirements, and select "
        + "permissions='auto'|'read-only'|'workspace-write'|'workspace-shell'|'network-shell'|'full-access', "
        + "and optionally writable_roots=[...]/network_access=true for shared data dirs or GitLab. "
        + "Dirty builders need source_inputs "
        + "or snapshot_required_files=true; read-only returns report text. "
        + "Analyze EACH subtask's task_profile (kind, scope, uncertainty, risk, validation, "
        + "optional reasoning/requires_shell/reason) with complexity omitted/auto; "
        + "use ONE delegate_run(agent/model/effort='auto',task_profile,permissions,notify=true). "
        + "DeepSeek defaults to deepseek-flash across all tiers (avoid v4-pro unless explicitly requested). "
        + "Codex uses Terra/low (simple) and Sol/medium or Sol/high (standard/cross-module/replay); "
        + "reserve Astra STRICTLY for genuinely complex tasks (escalate=true under sol-first) to conserve Codex quota. "
        + "Tests/replay require Codex or permissions='workspace-shell'. Keep tiny tasks in "
        + "main; use resume_job for fixes; finish with delegate_task accepted_jobs + evidence."
      )
    else:
      notes.append("External delegation is opt-in. Do not start automatically. Use /delegate:delegate "
                   "for on/off/manual or an explicit one-shot task. Native Claude workflow remains available.")
    with st.locked():
      current = st.session(session_id)
      current["hook_context_signature"] = context_sig
      st.save_session(session_id, current)
  elif event == "UserPromptSubmit" and state["mode"] == "auto":
    notes.append(
      "Delegate session_id=" + session_id + " (mode=auto, v" + runtime_image.VERSION
      + "). Pass task_profile; DeepSeek defaults to deepseek-flash; reserve Codex astra "
      + "strictly for complex tasks (escalate=true); notify=true/watch."
    )
  completed = []
  if state["mode"] == "auto":
    budget.schedule_refresh()
    signature, text = budget.notice(session_id)
    if event == "SessionStart" or state.get("routing_signature") != signature:
      notes.append(text)
      with st.locked():
        current = st.session(session_id)
        current["routing_signature"] = signature
        st.save_session(session_id, current)
  known = set(state.get("pending_jobs_seen", [])) if "pending_jobs" in state else set()
  candidates = list(dict.fromkeys([*state.get("pending_jobs", []),
    *(jid for jid in state["jobs"] if jid not in known)]))
  settled: set[str] = set()
  for job_id in candidates:
    reconciled = service.reconcile(job_id)
    note = delivery.notice(job_id, force=event == "SessionStart")
    if note:
      notes.append(note)
    if reconciled["status"] not in service.ACTIVE and reconciled.get("collected_at"):
      settled.add(job_id)
  if settled or state.get("pending_jobs_seen") != state["jobs"] or "pending_jobs" not in state:
    with st.locked():
      current = st.session(session_id)
      seen = set(current.get("pending_jobs_seen", [])) if "pending_jobs" in current else set()
      pending = list(dict.fromkeys([*current.get("pending_jobs", []),
        *(jid for jid in current["jobs"] if jid not in seen)]))
      pending = [jid for jid in pending if jid not in settled]
      if current.get("pending_jobs") != pending or current.get("pending_jobs_seen") != current["jobs"]:
        current["pending_jobs"] = pending
        current["pending_jobs_seen"] = list(current["jobs"])
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
  """Serve the standard MCP stdio JSON-RPC surface with zero-restart hot upgrades."""
  stdout_lock = threading.Lock()
  initialized = False

  def send_rpc(payload: dict[str, Any]) -> None:
    with stdout_lock:
      print(json.dumps(payload), flush=True)

  def notify_list_changed() -> None:
    if not initialized:
      return
    try:
      send_rpc({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    except OSError:
      return

  origin = Path(os.environ.get("DELEGATE_ORIGIN_SCRIPTS") or Path(__file__).resolve().parent)
  bridge = mcp_reload.HotRuntimeBridge(
    Path(__file__).resolve(),
    runtime_image.VERSION,
    origin,
    caller_host=hosts.caller_host(""),
  )

  def local_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
      value = dispatch(name, arguments)
      return {"content": [{"type": "text", "text": result_delivery.wire(value)}]}
    except Exception as exc:
      return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}

  bridge.start_watcher(notify_list_changed)
  try:
    for line in sys.stdin:
      request: Any = None
      req_id: Any = None
      try:
        request = json.loads(line)
        if not isinstance(request, dict):
          raise ValueError("JSON-RPC request must be an object")
        if "id" not in request:
          continue
        req_id = request["id"]
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
          raise ValueError("JSON-RPC params must be an object")
        mark_initialized = False
        if method == "initialize":
          bridge.refresh(force=True)
          result = {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "delegate", "version": bridge.active_version},
          }
          if hosts.caller_host("") in hosts.NATIVE_HOSTS:
            result["instructions"] = (
              "Call delegate_host once per conversation; retain its session_id for all tools. "
              "Collect/validate every required result. Parent usage is not metered; no Claude idle hook "
              "exists here. Delegated workers cannot recursively delegate."
            )
          mark_initialized = True
        elif method == "ping":
          result = {}
        elif method == "tools/list":
          result = {"tools": [] if os.environ.get("DELEGATE_WORKER") else bridge.list_tools(tool_list)}
        elif method == "tools/call":
          name = params["name"]
          arguments = params.get("arguments", {})
          if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
          try:
            result = bridge.call_tool(name, arguments, local_call)
          except Exception as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        else:
          send_rpc({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": "Method not found"},
          })
          continue
        send_rpc({"jsonrpc": "2.0", "id": req_id, "result": result})
        if mark_initialized:
          initialized = True
      except (ValueError, TypeError) as exc:
        err_id = (
          req_id if isinstance(request, dict) and isinstance(request.get("params", {}), dict) else None
        )
        send_rpc({
          "jsonrpc": "2.0", "id": err_id,
          "error": {"code": -32700, "message": str(exc)},
        })
      except Exception as exc:
        send_rpc({
          "jsonrpc": "2.0", "id": req_id,
          "error": {"code": -32603, "message": str(exc)},
        })
  finally:
    bridge.close()


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
      (arguments and arguments[0] in {
        "call", "wait", "loop-wait", "--help", "-h", "help", "--version",
        "result", "verify", "status", "doctor", "schema", "gc", "advise", "ask",
      })):
    import cli
    sys.exit(cli.main(arguments, dispatch, tool_list))
  mode = sys.argv[1] if len(sys.argv) > 1 else "mcp"
  native_args = {"codex", "dsh", "antigravity", "agy"}
  if mode in {"wait", "call", "loop-wait"} and len(sys.argv) == 3 and sys.argv[2] in native_args:
    os.environ["DELEGATE_CALLER_HOST"] = hosts.HOST_ALIASES.get(sys.argv[2], sys.argv[2])
  if mode == "host-mcp":
    if len(sys.argv) != 3 or sys.argv[2] not in native_args:
      raise ValueError("Use host-mcp codex|dsh|antigravity")
    os.environ["DELEGATE_CALLER_HOST"] = hosts.HOST_ALIASES.get(sys.argv[2], sys.argv[2])
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

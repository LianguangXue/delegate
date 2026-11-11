"""Task-level observed usage; bounded transcript reads and no prompt persistence."""
from pathlib import Path
from typing import Any
import json
import os
import statistics
import time
import uuid

import storage as st
from lifecycle import ACTIVE
import attempts as at

FIELDS = ("uncached_input", "cached_input", "cache_write", "output")
READ_LIMIT = 2 * 1024 * 1024


def normalize(raw: dict[str, Any] | None, kind: str = "claude") -> dict[str, int] | None:
  """Normalize counters without counting cached/reasoning subsets twice."""
  if not isinstance(raw, dict):
    return None
  if kind == "codex":
    if not all(k in raw for k in ("inputTokens", "outputTokens")):
      return None
    values = [raw["inputTokens"] - raw.get("cachedInputTokens", 0) - raw.get("cacheWriteInputTokens", 0),
              raw.get("cachedInputTokens", 0), raw.get("cacheWriteInputTokens", 0), raw["outputTokens"]]
  else:
    if not all(k in raw for k in ("input_tokens", "output_tokens")):
      return None
    values = [raw["input_tokens"], raw.get("cache_read_input_tokens", 0),
              raw.get("cache_creation_input_tokens", 0), raw["output_tokens"]]
  if any(type(n) is not int or n < 0 for n in values):
    return None
  return dict(zip(FIELDS, values))


def totals(rows: list[dict[str, int]]) -> dict[str, int]:
  """Report raw token volume separately from a cache-excluding proxy, never as money."""
  result = {key: sum(row.get(key, 0) for row in rows) for key in FIELDS}
  result["processed_tokens"] = sum(result.values())
  result["uncached_plus_output_tokens"] = result["uncached_input"] + result["cache_write"] + result["output"]
  return result


def load(task_id: str) -> dict[str, Any]:
  """Read one known task record."""
  task = st.read("task-" + st.key(task_id) + ".json")
  if task is None:
    raise ValueError("Unknown task")
  return task


def save(task: dict[str, Any]) -> None:
  """Persist only accounting metadata, never copied transcript bodies."""
  st.write("task-" + st.key(task["id"]) + ".json", task)


def begin(session_id: str, label: str = "Current user task", category: str = "general",
          complexity: str = "standard", case_key: str = "", arm: str = "optimized",
          token_budget: int = 0, max_attempts: int = 3) -> dict[str, Any]:
  """Start a root task; explicit baseline measurement does not enable delegation."""
  if complexity not in {"trivial", "simple", "standard", "complex"} or arm not in {"baseline", "optimized"}:
    raise ValueError("Invalid task complexity or comparison arm")
  if type(token_budget) is not int or token_budget < 0 or type(max_attempts) is not int or not 1 <= max_attempts <= 10:
    raise ValueError("Invalid task budget or attempt limit")
  if len(label) > 300 or len(category) > 80 or len(case_key) > 200:
    raise ValueError("Use compact task labels and case keys")
  with st.locked():
    state = st.session(session_id)
    if state.get("active_task"):
      return load(state["active_task"])
    context = st.read("meter-" + st.key(session_id) + ".json", {})
    task = {"id": "t" + uuid.uuid4().hex[:16], "session_id": session_id, "label": label,
            "category": category, "complexity": complexity, "case_key": case_key, "arm": arm,
            "created_at": time.time(), "status": "open", "verdict": "unrated", "evidence": "",
            "token_budget": token_budget, "max_attempts": max_attempts, "jobs": [], "requests": {},
            "native": {}, "issues": [], "transcript_path": context.get("path"),
            "offset": context.get("turn_start", context.get("offset", 0)), "parent_observed": False}
    task.update(parent_complete=False, unmetered_messages={}, source_identity={})
    if not task["transcript_path"]:
      task["issues"].append("parent transcript unavailable")
    save(task)
    state["active_task"] = task["id"]
    st.save_session(session_id, state)
    index = st.read("task-index.json", [])
    st.write("task-index.json", (index + [task["id"]])[-1000:])
    return task


def scan(task: dict[str, Any], path: str, source: str, offset: int = 0,
         max_bytes: int = READ_LIMIT, end_offset: int | None = None) -> tuple[int, bool]:
  """Read one bounded transcript suffix, retaining only IDs, models and token counters."""
  consumed = 0
  complete = False
  try:
    with Path(path).open("rb") as stream:
      info = os.fstat(stream.fileno())
      identity = [info.st_dev, info.st_ino]
      previous_identity = task.setdefault("source_identity", {}).get(source)
      if previous_identity and previous_identity != identity:
        task["issues"].append(source + " transcript replaced")
        return offset, False
      task["source_identity"][source] = identity
      size = stream.seek(0, 2)
      if end_offset is not None and size < end_offset:
        task["issues"].append(source + " frozen transcript tail was truncated")
        return offset, False
      if size < offset:
        task["issues"].append(source + " transcript truncated/rotated")
        return offset, False
      stream.seek(offset)
      end = min(size, end_offset) if end_offset is not None else size
      while consumed < max_bytes and offset < end:
        start = stream.tell()
        line = stream.readline(min(1024 * 1024, max_bytes - consumed, end - offset))
        if not line:
          complete = True
          break
        consumed += len(line)
        if not line.endswith(b"\n"):
          stream.seek(start)
          task["issues"].append(source + " transcript read bound or partial line")
          break
        offset = stream.tell()
        try:
          event = json.loads(line)
        except ValueError:
          task["issues"].append(source + " invalid transcript JSON")
          continue
        if not isinstance(event, dict):
          task["issues"].append(source + " invalid transcript event")
          continue
        if event.get("type") != "assistant" or (source == "parent" and event.get("isSidechain")):
          continue
        message = event.get("message", {})
        if not isinstance(message, dict):
          task["issues"].append(source + " invalid assistant message")
          continue
        message_id = message.get("id") or event.get("request_id") or event.get("requestId")
        raw = normalize(message.get("usage"))
        if not raw:
          if message_id and message.get("model") != "<synthetic>":
            task.setdefault("unmetered_messages", {})[source + ":" + message_id] = True
          continue
        if not message_id:
          task["issues"].append(source + " usage without deduplication ID")
          continue
        key = source + ":" + message_id
        task.setdefault("unmetered_messages", {}).pop(key, None)
        old = task["requests"].get(key, {})
        # Streaming fragments may repeat input usage and grow output usage.
        task["requests"][key] = {"source": source, "model": message.get("model", "unknown"),
          "usage": {k: max(raw[k], old.get("usage", {}).get(k, 0)) for k in FIELDS}}
        if source == "parent":
          task["parent_observed"] = True
      complete = offset >= end
  except OSError:
    task["issues"].append(source + " transcript unreadable")
  if complete:
    task["issues"] = [i for i in task["issues"] if i not in {
      source + " transcript read bound or partial line", source + " transcript unreadable"}]
  task["issues"] = list(dict.fromkeys(task["issues"]))
  return offset, complete


def collect(task: dict[str, Any]) -> None:
  """Update an open task from known transcript paths only."""
  if task.get("transcript_path") and (task["status"] != "closed" or not task.get("parent_complete")):
    task["offset"], task["parent_complete"] = scan(task, task["transcript_path"], "parent", task["offset"],
                                                 end_offset=task.get("end_offset"))
  for agent_id, child in task["native"].items():
    if child.get("path") and not child.get("complete"):
      child["offset"], child["complete"] = scan(task, child["path"], "native:" + agent_id,
                                               child.get("offset", 0))
      child["observed"] = any(r["source"] == "native:" + agent_id for r in task["requests"].values())


def hook(payload: dict[str, Any]) -> None:
  """Meter opt-in tasks and observe final responses without blocking Stop or spawning inference."""
  sid, event = payload.get("session_id"), payload.get("hook_event_name")
  if not sid:
    return
  if payload.get("agent_id") and event not in {"SubagentStart", "SubagentStop"}:
    return
  state = st.session(sid)
  path = payload.get("transcript_path")
  if event in {"SessionStart", "UserPromptSubmit"} and path:
    try:
      size = Path(path).stat().st_size
    except OSError:
      size = 0
    st.write("meter-" + st.key(sid) + ".json", {"path": path, "turn_start": size, "offset": size})
  if not state.get("active_task") and state["mode"] == "auto" and event in {"UserPromptSubmit", "PostToolUse"}:
    begin(sid)
    state = st.session(sid)
  task_id = state.get("active_task")
  if not task_id:
    return
  with st.locked():
    task = load(task_id)
    if not task.get("transcript_path") and path:
      task["transcript_path"] = path
      task["offset"] = st.read("meter-" + sid + ".json", {}).get("turn_start", 0)
      task["issues"] = [i for i in task["issues"] if i != "parent transcript unavailable"]
    if event == "SubagentStart" and payload.get("agent_id"):
      task["native"].setdefault(payload["agent_id"], {"complete": False})
    if event == "SubagentStop" and payload.get("agent_id"):
      task["native"].setdefault(payload["agent_id"], {}).update(path=payload.get("agent_transcript_path"))
    collect(task)
    if event == "StopFailure":
      task["issues"].append("parent API failure; unreported partial usage may be missing")
    if event in {"Stop", "StopFailure"}:
      live = (any(st.job(j)["status"] in ACTIVE and st.job(j).get("await_completion", True)
                  and not (st.job(j)["status"] == "orphaned" and st.job(j).get("collected_at"))
                  for j in task["jobs"])
              or any(not child.get("complete") for child in task["native"].values()))
      if not live:
        if task.get("transcript_path"):
          try:
            task["end_offset"] = Path(task["transcript_path"]).stat().st_size
          except OSError:
            task["issues"].append("parent transcript unreadable at close")
          task["parent_complete"] = task.get("parent_complete", False) and task["offset"] == task.get("end_offset")
        task.update(status="closed", finished_at=time.time())
        if event == "StopFailure":
          task["verdict"] = "failed"
        state = st.session(sid)
        if state.get("active_task") == task_id:
          state.pop("active_task", None)
          st.save_session(sid, state)
    save(task)
    calibrate_locked(task)


def job_usage(job: dict[str, Any]) -> dict[str, int] | None:
  """Use per-turn Codex deltas and per-invocation Claude totals, including failed attempts."""
  if not at.model_attempted(job):
    return {field: 0 for field in FIELDS}
  if job["provider"]["kind"] == "codex":
    return normalize(job.get("usage_delta"), "codex")
  return normalize(job.get("usage"))


def report(task: dict[str, Any]) -> dict[str, Any]:
  """Aggregate parent, native and delegated work with explicit completeness boundaries."""
  groups: dict[str, list] = {}
  issues = list(task["issues"])
  for request in task["requests"].values():
    name = "claude:" + request["model"]
    groups.setdefault(name, []).append(request["usage"])
  children = []
  for job_id in task["jobs"]:
    job = st.job(job_id)
    usage = job_usage(job)
    if usage is None:
      issues.append("missing usage for " + job_id)
    else:
      groups.setdefault(job["provider_name"] + ":" + job["model"], []).append(usage)
    children.append({"id": job_id, "status": job["status"], "review": job.get("review"),
                     "work_item": job.get("work_item", "main"), "model": job["model"], "effort": job["effort"],
                     "complexity": job.get("complexity", task["complexity"]),
                     "category": job.get("category", task["category"]), **at.describe(job)})
  if not task["parent_observed"]:
    issues.append("parent usage not observed")
  if not task.get("parent_complete"):
    issues.append("parent transcript not completely metered")
  if task.get("unmetered_messages"):
    issues.append("assistant messages with missing usage")
  if any(not child.get("complete") for child in task["native"].values()):
    issues.append("native subagent usage incomplete")
  if any(not child.get("observed") for child in task["native"].values()):
    issues.append("native subagent usage not observed")
  if any(child["status"] in ACTIVE for child in children):
    issues.append("jobs still active")
  by_model = {name: totals(rows) for name, rows in groups.items()}
  parent = totals([r["usage"] for r in task["requests"].values() if r["source"] == "parent"])
  native_parent = st.session(task["session_id"]).get("host_kind") in {"codex", "dsh"}
  if native_parent:
    parent = None
  return {k: task[k] for k in ("id", "session_id", "label", "category", "complexity", "case_key", "arm",
                               "status", "verdict", "evidence", "token_budget", "max_attempts")} | {
    "parent": parent, "parent_metering": "unavailable" if native_parent else "transcript",
    "total_scope": "observed worker usage; native parent excluded" if native_parent else "observed parent and worker usage",
    "by_provider_model": by_model,
    "total": totals([r for rows in groups.values() for r in rows]), "jobs": children,
    "elapsed_seconds": round(task.get("finished_at", time.time()) - task["created_at"], 3),
    "measurement_complete": not issues and task["status"] == "closed", "issues": sorted(set(issues)),
    "final_response_pending": task["status"] != "closed",
    "units": "observed tokens; cache subsets separate; not billed dollars or equivalent quota credits",
    "record_path": str(st.root() / ("task-" + task["id"] + ".json"))}


def calibrate_locked(task: dict[str, Any]) -> None:
  """Maintain a compact sample index at task close, avoiding history scans during routing."""
  if task["status"] != "closed":
    return
  result = report(task)
  rows = []
  for job_id in task["jobs"]:
    job = st.job(job_id)
    usage = job_usage(job)
    failed = job["status"] in {"failed", "timed_out", "cancelled"} or job.get("review") in {"needs-work", "blocked"}
    rows.append({"agent": job["agent"], "model": job["model"], "effort": job["effort"],
                 "category": job.get("category", task["category"]), "complexity": job.get("complexity", task["complexity"]),
                 "tokens": totals([usage])["processed_tokens"] if usage else None,
                 "accepted": job.get("review") == "accepted",
                 "quality_known": failed or job.get("review") == "accepted", "model_attempted": at.model_attempted(job)})
  history = st.read("optimization-samples.json", {})
  model_attempts = sum(row["model_attempted"] for row in rows)
  history[task["id"]] = {"category": task["category"], "complexity": task["complexity"], "rows": rows,
    "parent_share": result["parent"]["processed_tokens"] / model_attempts
      if result["measurement_complete"] and model_attempts else None,
    "main_models": sorted({r["model"] for r in task["requests"].values() if r["source"] == "parent"}),
    "measurement_complete": result["measurement_complete"],
    "total": result["total"]["processed_tokens"], "arm": task["arm"], "verdict": task["verdict"]}
  st.write("optimization-samples.json", dict(list(history.items())[-500:]))


def task_tool(session_id: str, action: str = "report", task_id: str = "", label: str = "Current user task",
              category: str = "general", complexity: str = "standard", case_key: str = "",
              arm: str = "optimized", token_budget: int = 0, max_attempts: int = 3,
              verdict: str = "", evidence: str = "", accepted_jobs: list[str] | None = None,
              reference_task_id: str = "") -> Any:
  """Begin/update/finish/report tasks, or compare accepted complete matched cases."""
  if action == "begin":
    return report(begin(session_id, label, category, complexity, case_key, arm, token_budget, max_attempts))
  if action == "compare":
    return compare(case_key, reference_task_id)
  state = st.session(session_id)
  if action == "list":
    return [{k: load(t)[k] for k in ("id", "label", "status", "verdict")} for t in st.read("task-index.json", [])[-100:]
            if load(t)["session_id"] == session_id]
  task_id = task_id or state.get("active_task", "")
  if not task_id:
    raise ValueError("No active task; specify task_id or begin one")
  with st.locked():
    state = st.session(session_id)
    task = load(task_id)
    if task["session_id"] != session_id:
      raise ValueError("Task belongs to another session")
    collect(task)
    if action == "update":
      if task["jobs"] or task["status"] != "open":
        raise ValueError("Classify the task before dispatching jobs")
      if complexity not in {"trivial", "simple", "standard", "complex"} or arm not in {"baseline", "optimized"}:
        raise ValueError("Invalid task classification")
      task.update(label=label[:300], category=category[:80], complexity=complexity, case_key=case_key[:200], arm=arm)
    elif action == "finish":
      if verdict not in {"accepted", "needs-work", "blocked"} or not evidence.strip():
        raise ValueError("Finish requires a verdict and actual validation evidence")
      if any(st.job(j)["status"] in ACTIVE for j in task["jobs"]):
        raise ValueError("Collect all task jobs before finishing")
      if any(not child.get("complete") for child in task["native"].values()):
        raise ValueError("Native subagent accounting is still pending; collect/report before finishing")
      if accepted_jobs:
        for job_id in accepted_jobs:
          if job_id not in task["jobs"]:
            raise ValueError("Accepted job is outside this task")
          job = st.job(job_id)
          if job["status"] != "completed" or job.get("error"):
            raise ValueError("Only successfully completed jobs can be accepted")
        for job_id in accepted_jobs:
          job = st.job(job_id)
          job.update(review="accepted", review_evidence=evidence[:4000])
          st.write("job-" + job_id + ".json", job)
      task.update(verdict=verdict, evidence=evidence[:4000])
      if task["status"] != "closed":
        if state.get("host_kind") in {"codex", "dsh"}:
          task.update(status="closed", finished_at=time.time())
          task["issues"].append("Native parent usage/final response not metered by this adapter")
          if state.get("active_task") == task["id"]:
            state.pop("active_task", None)
            st.save_session(session_id, state)
        else:
          task["status"] = "closing"  # Stop hook includes the parent's final response.
    elif action != "report":
      raise ValueError("Unknown task action")
    save(task)
    calibrate_locked(task)
  return report(task)


def compare(case_key: str, reference_task_id: str = "") -> dict[str, Any]:
  """Compare only declared matching frozen cases with acceptance and complete measurements."""
  if not case_key:
    raise ValueError("A shared frozen case_key is required")
  arms: dict[str, list] = {"baseline": [], "optimized": []}
  if reference_task_id:
    reference = report(load(reference_task_id))
    if reference["case_key"] != case_key or not reference["measurement_complete"] or reference["verdict"] != "accepted":
      raise ValueError("Reference task must be a complete accepted run of the same frozen case")
  excluded = []
  for task_id in st.read("task-index.json", [])[-200:]:
    task = load(task_id)
    if task["case_key"] != case_key:
      continue
    result = report(task)
    if not result["measurement_complete"] or result["verdict"] != "accepted":
      excluded.append(task_id)
    else:
      arm = ("baseline" if task_id == reference_task_id else "optimized") if reference_task_id else task["arm"]
      arms[arm].append(result)
  summary = {name: {"n": len(rows), "task_ids": [r["id"] for r in rows],
    "median_processed_tokens": statistics.median(r["total"]["processed_tokens"] for r in rows) if rows else None,
    "median_uncached_plus_output_tokens": statistics.median(r["total"]["uncached_plus_output_tokens"] for r in rows) if rows else None,
    "median_parent_tokens": statistics.median(r["parent"]["processed_tokens"] for r in rows) if rows else None}
    for name, rows in arms.items()}
  base, opt = summary["baseline"]["median_processed_tokens"], summary["optimized"]["median_processed_tokens"]
  fresh_base, fresh_opt = (summary[arm]["median_uncached_plus_output_tokens"] for arm in ("baseline", "optimized"))
  consistent = len({(r["category"], r["complexity"]) for rows in arms.values() for r in rows}) <= 1
  return {"case_key": case_key, "reference_task_id": reference_task_id or None,
          "arms": summary, "excluded_incomplete_or_unaccepted": excluded,
          "consistent_classification": consistent,
          "total_token_reduction_percent": round(100 * (base - opt) / base, 2) if consistent and base and opt is not None else None,
          "uncached_proxy_reduction_percent": round(100 * (fresh_base - fresh_opt) / fresh_base, 2)
            if consistent and fresh_base and fresh_opt is not None else None,
          "billed_or_quota_savings": "unknown; cache mix and provider charging differ",
          "evidence": "matched-case observations, not a general savings guarantee; verify equivalent frozen inputs and quality"}

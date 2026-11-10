"""Versioned, retryable handoffs to am; neither component owns the other's task state."""
from pathlib import Path
from typing import Any
import hashlib
import json
import subprocess
import time

import storage as st
from lifecycle import ACTIVE


def _sync(session_id: str, scope: str = "") -> dict[str, Any]:
  """Persist the newest snapshot before attempting delivery; failures remain retryable."""
  with st.locked():
    state = st.session(session_id)
    binding = state.get("am_bindings", {}).get(scope) if scope else state.get("am_binding")
    if not binding:
      return {"linked": False}
    jobs = []
    selected = [jid for jid in state["jobs"] if not scope or (
      st.job(jid).get("task_id") == binding["delegate_task_id"] and
      (not binding.get("work_item") or st.job(jid).get("work_item") == binding["work_item"]))]
    for jid in selected[-100:]:
      j = st.job(jid)
      jobs.append({k: j.get(k) for k in ("id", "task_id", "work_item", "status", "model", "effort",
        "review", "review_evidence", "collected_at", "source_cwd", "cwd", "base_commit", "resume_job", "git_commit")}
        | {"record_path": str(st.root() / ("job-" + jid + ".json")),
           "review_evidence": str(j.get("review_evidence") or "")[:1000],
           "acceptance": str(j.get("acceptance", ""))[:500],
           "error": str(j.get("error") or "")[:1000],
           "conversation": {k:v for k,v in (j.get("cli_session") or j.get("codex_session") or {}).items()
                            if k in {"status", "archive_path", "recoverable"}},
           "execution_terminal": j["status"] not in ACTIVE})
    source = hashlib.sha256((str(st.root().resolve()) + ":" + session_id + (":" + scope if scope else "")).encode()).hexdigest()
    packet = {"schema_version": 1, "source_id": source, "session_id": session_id,
              "am_task_id": binding["task_id"], "next_step": state.get("handoff_steps", {}).get(scope, "") if scope else state.get("handoff_next", ""), "jobs": jobs,
              "jobs_total": len(selected), "jobs_omitted": max(0, len(selected) - len(jobs)),
              "session_record_path": str(st.root() / ("session-" + session_id + ".json"))}
    while len(json.dumps(packet)) > 900_000 and packet["jobs"]:
      packet["jobs"].pop(0)
      packet["jobs_omitted"] += 1
    name = "handoff-" + st.key(session_id) + ("-" + scope if scope else "") + ".json"
    outbox = st.read(name, {})
    prior = outbox.get("packet", {})
    if {k:v for k,v in prior.items() if k != "revision"} != packet:
      packet["revision"] = prior.get("revision", 0) + 1
      outbox = {"packet": packet, "acked_revision": outbox.get("acked_revision", 0)}
      st.write(name, outbox)
    else:
      packet = prior
    if outbox.get("acked_revision", 0) >= packet["revision"]:
      return {"linked": True, "delivered": True, "revision": packet["revision"]}
  try:
    proc = subprocess.run(["am", "--root", binding["root"], "delegate", "import"],
      input=json.dumps(packet), text=True, capture_output=True, timeout=5, check=True)
    ack = json.loads(proc.stdout)
    if (ack.get("source_id") != source or ack.get("am_task_id") != binding["task_id"]
        or ack.get("revision", 0) < packet["revision"]):
      raise ValueError("am did not acknowledge this snapshot")
    error = None
  except (OSError, subprocess.SubprocessError, ValueError) as exc:
    error = type(exc).__name__  # Never copy subprocess stdout/credentials into hooks.
  with st.locked():
    current = st.read(name)
    if not error:
      current["acked_revision"] = max(current.get("acked_revision", 0), packet["revision"])
    current["last_error"] = error
    current["last_attempt_at"] = time.time()
    st.write(name, current)
  return {"linked": True, "delivered": error is None, "revision": packet["revision"],
          "error": error, "record_path": str(st.root() / name)}


def sync(session_id: str) -> dict[str, Any]:
  """Sync legacy and individually scoped bindings without merging unrelated tasks."""
  result = _sync(session_id)
  scopes = st.session(session_id).get("am_bindings", {})
  if scopes:
    result["scopes"] = {key: _sync(session_id, key) for key in scopes}
    legacy_ok = not result.get("linked") or result.get("delivered", False)
    result["linked"] = True
    result["delivered"] = legacy_ok and all(v.get("delivered", False) for v in result["scopes"].values())
  return result


def link(session_id: str, action: str = "report", am_task_id: str = "", am_root: str = "",
         next_step: str = "", delegate_task_id: str = "", work_item: str = "") -> dict[str, Any]:
  """Bind a session to an existing am task or export/read its structured handoff."""
  st.key(session_id)
  scope = ""
  if work_item and not delegate_task_id:
    raise ValueError("work_item requires delegate_task_id")
  if delegate_task_id:
    task = st.read("task-" + st.key(delegate_task_id) + ".json")
    if not task or task["session_id"] != session_id:
      raise ValueError("Task does not belong to this session")
    if work_item:
      st.key(work_item)
    scope = hashlib.sha256((delegate_task_id + ":" + work_item).encode()).hexdigest()[:24]
  if action == "bind":
    st.key(am_task_id)
    if not am_root:
      am_root = subprocess.run(["am", "where"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    root = Path(am_root).expanduser().resolve()
    if not (root / "config.json").is_file() or not (root / "tasks" / (am_task_id + ".json")).is_file():
      raise ValueError("Bind requires an existing am store and task")
    with st.locked():
      state = st.session(session_id)
      binding = {"root": str(root), "task_id": am_task_id}
      if scope:
        if state.get("am_binding"):
          raise ValueError("Legacy session binding exists; use a new session for task-scoped links")
        for prior in state.get("am_bindings", {}).values():
          if prior["delegate_task_id"] == delegate_task_id and (not prior.get("work_item") or not work_item or prior["work_item"] == work_item):
            if prior.get("work_item", "") != work_item or prior["task_id"] != am_task_id or prior["root"] != str(root):
              raise ValueError("Overlapping task scopes cannot target different am tasks")
        binding.update(delegate_task_id=delegate_task_id, work_item=work_item)
        existing = state.setdefault("am_bindings", {}).get(scope)
        if existing and existing != binding:
          raise ValueError("This task scope is already bound")
        state["am_bindings"][scope] = binding
      elif state.get("am_bindings"):
        raise ValueError("Task-scoped links exist; do not add a session-wide binding")
      elif state.get("am_binding") and state["am_binding"] != binding:
        raise ValueError("Session already linked; use a new session for a different am task")
      if not scope:
        state["am_binding"] = binding
      st.save_session(session_id, state)
  elif action == "next":
    if not next_step.strip() or len(next_step) > 4000:
      raise ValueError("Provide a bounded next step; it is recorded, never executed")
    with st.locked():
      state = st.session(session_id)
      if scope:
        state.setdefault("handoff_steps", {})[scope] = next_step
      else:
        state["handoff_next"] = next_step
      st.save_session(session_id, state)
  elif action not in {"report", "sync"}:
    raise ValueError("action must be bind/next/report/sync")
  result = _sync(session_id, scope) if scope else sync(session_id)
  if action == "report":
    result["snapshot"] = st.read("handoff-" + session_id + ("-" + scope if scope else "") + ".json", {}).get("packet")
  return result

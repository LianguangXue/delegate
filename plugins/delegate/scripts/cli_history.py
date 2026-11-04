"""Recoverable archival of exact plugin-owned Claude/Anthropic worker conversations."""
from pathlib import Path
from typing import Any
import os
import re
import time

import storage as st
from lifecycle import ACTIVE, alive, group_running


def movable(value: dict[str, Any], finishing: bool = False) -> None:
  """Reject archival while any registered attempt can still use this conversation."""
  for jid in st.session(value["session_id"])["jobs"]:
    other = st.job(jid)
    if other.get("thread_id") != value["thread_id"]:
      continue
    own_finish = finishing and jid == value["id"]
    if (other.get("backend_starting") or group_running(other.get("backend_identity"))
        or (not own_finish and (other["status"] in ACTIVE or alive(other.get("worker_identity"))))):
      raise ValueError("Conversation is still in use; leave its history untouched")


def locations(value: dict[str, Any]) -> list[Path]:
  """Address exact known session files, including the CLI's canonicalized cwd spelling."""
  home = Path(value.get("backend_config_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
  paths = []
  for cwd in dict.fromkeys([value["cwd"], str(Path(value["cwd"]).resolve())]):
    project = home / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    path = project / (st.key(value["thread_id"]) + ".jsonl")
    if path.is_symlink() or project.is_symlink():
      raise ValueError("Refusing symlinked conversation history")
    if path.exists():
      paths.append(path)
  return list(dict.fromkeys(p.resolve() for p in paths))


def move(entries: list[dict[str, str]], restore: bool = False) -> None:
  """Move a journaled unit idempotently, refusing ambiguous source/destination pairs."""
  for entry in entries:
    source, target = Path(entry["source"]), Path(entry["archive"])
    if restore:
      source, target = target, source
    if source.is_symlink() or target.is_symlink():
      raise ValueError("Refusing symlinked history entry")
    if source.exists() and target.exists():
      raise ValueError("Both history locations exist; preserve both for inspection")
    if source.exists():
      target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
      os.rename(source, target)
    elif not target.exists():
      raise ValueError("Conversation history entry is missing from both locations")


def new_journal(value: dict[str, Any]) -> dict[str, Any]:
  """Inventory the exact JSONL and companion before the first archive or prepared resume."""
  tid = st.key(value["thread_id"])
  found = locations(value)
  if len(found) != 1:
    raise ValueError("Expected one exact conversation history; found " + str(len(found)))
  source = found[0]
  destination = st.root() / "history-archive" / "cli" / tid
  entries = [{"source": str(source), "archive": str(destination / source.name)}]
  companion = source.with_suffix("")
  if companion.exists() or companion.is_symlink():
    entries.append({"source": str(companion), "archive": str(destination / companion.name)})
  return {"thread_id": tid, "session_id": value["session_id"], "entries": entries}


def archive(job_id: str, finishing: bool = False) -> dict[str, Any]:
  """Hide a finished conversation from live project lists while retaining exact resume files."""
  import thread_cleanup
  value = st.job(job_id)
  tid = st.key(value["thread_id"])
  with thread_cleanup.guard(tid):
    value = st.job(job_id)
    movable(value, finishing)
    name = "cli-history-" + tid + ".json"
    journal = st.read(name)
    if journal and time.time() - journal.get("resume_prepared_at", 0) < 900:
      attempts = [jid for jid in st.session(value["session_id"])["jobs"]
                  if st.job(jid).get("thread_id") == tid]
      if not set(attempts) - set(journal.get("prepared_jobs", [])):
        raise ValueError("Resume is prepared; keep the conversation available for dispatch")
    if journal is None:
      journal = new_journal(value)
    # A resumed CLI can create new per-thread tool/subagent files.
    primary = Path(journal["entries"][0]["source"])
    companion = primary.with_suffix("")
    if (companion.exists() or companion.is_symlink()) and not any(
        entry["source"] == str(companion) for entry in journal["entries"]):
      destination = Path(journal["entries"][0]["archive"]).parent
      journal["entries"].append({"source": str(companion), "archive": str(destination / companion.name)})
    if journal["session_id"] != value["session_id"]:
      raise ValueError("Conversation archive belongs to another parent session")
    journal["status"] = "archiving"
    st.write(name, journal)  # Recover partial moves using these exact paths.
    try:
      move(journal["entries"])
    except Exception as exc:
      st.update_job(job_id, cli_session={"status": "pending", "error": str(exc)})
      raise
    journal.pop("resume_prepared_at", None)
    journal.pop("prepared_jobs", None)
    journal.update(status="archived", archived_at=time.time())
    st.write(name, journal)
    receipt = {"status": "archived", "thread_id": tid, "results_preserved": True,
               "recoverable": True, "archive_path": str(st.root() / "history-archive" / "cli" / tid)}
    for jid in st.session(value["session_id"])["jobs"]:
      if st.job(jid).get("thread_id") == tid:
        st.update_job(jid, cli_session=receipt)
    return receipt


def restore(value: dict[str, Any], prepare: bool = False) -> None:
  """Restore only a registered resumed job's journal, before the backend launches."""
  import thread_cleanup
  if not value.get("resume_job") or not value.get("thread_id"):
    return
  tid = st.key(value["thread_id"])
  with thread_cleanup.guard(tid, wait_seconds=15):
    movable(value, finishing=True)
    name = "cli-history-" + tid + ".json"
    journal = st.read(name)
    if journal is None:
      if not prepare:
        return  # Legacy active histories are still directly resumable.
      journal = new_journal(value)
    if journal["session_id"] != value["session_id"]:
      raise ValueError("Conversation archive belongs to another parent session")
    journal["status"] = "restoring"
    st.write(name, journal)
    move(journal["entries"], restore=True)
    journal["status"] = "restored"
    if prepare:
      journal["resume_prepared_at"] = time.time()
      journal["prepared_jobs"] = [jid for jid in st.session(value["session_id"])["jobs"]
                                  if st.job(jid).get("thread_id") == tid]
    st.write(name, journal)
    st.update_job(value["id"], cli_session={"status": "restored", "thread_id": tid})


def prepare_resume(payload: dict[str, Any]) -> None:
  """Restore an exact authorized parent-owned resume before even a legacy MCP executes."""
  if payload.get("agent_id"):
    return
  name = str(payload.get("tool_name", ""))
  args = payload.get("tool_input", {})
  if not name.startswith("mcp__") or not name.endswith("__delegate_run") or not isinstance(args, dict):
    return
  sid = payload.get("session_id")
  if not sid or args.get("session_id") != sid or not args.get("resume_job"):
    return
  previous = st.job(args["resume_job"])
  if previous["session_id"] != sid:
    raise ValueError("Resume belongs to another parent session")
  if previous["provider"]["kind"] == "codex":
    return
  if previous["status"] in ACTIVE:
    raise ValueError("Cannot restore an active conversation")
  if args.get("agent") != previous["agent"] or args.get("cwd") != previous["source_cwd"]:
    return  # Let the service reject the unmatched role/workspace.
  one_shot = args.get("one_shot", False)
  state, cfg = st.session(sid), st.config()
  role = cfg["agents"].get(args.get("agent"), {})
  provider = cfg["providers"].get(role.get("provider"), {})
  if (type(one_shot) is not bool or not role.get("enabled") or not provider.get("enabled")
      or (not one_shot and (state["mode"] != "auto" or not role.get("automatic")))
      or (state.get("agents") is not None and args["agent"] not in state["agents"])):
    return
  restore({**previous, "resume_job": previous["id"]}, prepare=True)

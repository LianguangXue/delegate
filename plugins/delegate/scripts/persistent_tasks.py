"""Durable, bounded controllers for supervised steps and opt-in automatic loops."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator
import fcntl
import hashlib
import inspect
import json
import os
import subprocess
import sys
import time
import uuid

import accounting as ac
import attempts
import runtime_image
import source_inputs
import runtime
import storage as st
from lifecycle import ACTIVE, alive, group_running, identity, reconcile

TERMINAL = {"completed", "cancelled", "stopped"}
IDLE = TERMINAL | {"paused", "awaiting_review", "blocked"}
LAUNCH: ContextVar[dict[str, Any] | None] = ContextVar("delegate_loop_launch", default=None)


def load(loop_id: str) -> dict[str, Any]:
  """Load one exact durable controller, never scan directories."""
  value = st.read("loop-" + st.key(loop_id) + ".json")
  if value is None:
    raise ValueError("Unknown persistent task")
  return value


def update(loop_id: str, **values: Any) -> dict[str, Any]:
  """Update controller fields atomically without overwriting concurrent supervision."""
  with st.locked():
    value = load(loop_id)
    if "ack_seq" in values:
      values["ack_seq"] = max(value.get("ack_seq", 0), values["ack_seq"])
    value.update(values, updated_at=time.time())
    st.write("loop-" + loop_id + ".json", value)
    return value


def event(loop_id: str, status: str, note: str, **values: Any) -> dict[str, Any]:
  """Publish one durable supervision event; this is never acceptance."""
  with st.locked():
    value = load(loop_id)
    value.update(values, status=status, note=note[:2000], updated_at=time.time(), event_seq=value.get("event_seq", 0) + 1)
    st.write("loop-" + loop_id + ".json", value)
    return value


def context(session_id: str) -> dict[str, Any] | None:
  """Return a program-owned launch binding, unavailable to model tool arguments."""
  value = LAUNCH.get()
  if value and value["session_id"] != session_id:
    raise ValueError("Persistent-task launch belongs to another session")
  return value


def task_for(session_id: str) -> dict[str, Any] | None:
  """Pin child registration to its controller ledger rather than the parent's current turn."""
  value = context(session_id)
  return ac.load(value["task_id"]) if value else None


def check_child_launch(job: dict[str, Any]) -> None:
  """Fence a late worker from a failed/superseded intent before it can start inference."""
  if not job.get("loop_id"):
    return
  loop = load(job["loop_id"])
  current = st.job(job["id"])
  if (loop["session_id"] != job["session_id"] or (loop.get("pending") or {}).get("key") != job.get("loop_launch_key")
      or loop.get("requested") == "cancel" or current["status"] not in {"queued", "running"} or current.get("cancel_requested")):
    raise InterruptedError("Persistent child launch was cancelled or superseded; no new model request authorized")


def validate_launch_locked(binding: dict[str, Any], job: dict[str, Any]) -> None:
  """Recheck cancellation, ownership and the controller identity at dispatch admission."""
  loop = load(binding["loop_id"])
  if (loop["session_id"] != job["session_id"] or loop["task_id"] != job["task_id"]
      or (loop.get("pending") or {}).get("key") != binding["key"]
      or loop.get("requested") != "run" or loop.get("controller_identity") != identity(os.getpid())):
    raise ValueError("Persistent-task launch was superseded or its controller identity cannot be verified")
  if job["agent"] not in loop["authorized_agents"]:
    raise ValueError("Agent is outside the persistent task's original authorized pool")
  if binding["cycle"] > loop["max_cycles"]:
    raise ValueError("Persistent cycle limit changed before dispatch admission")
  spent = usage(loop)
  if spent["unknown_attempts"] or (loop["token_budget"] and spent["tokens"]["processed_tokens"] >= loop["token_budget"]):
    raise ValueError("Persistent usage is unknown or its current token budget is exhausted")
  owner = st.session(job["session_id"])
  if owner.get("mode_revision", 0) != loop["mode_revision"] and owner["mode"] != "auto":
    raise ValueError("Parent disabled new delegation; pause before another persistent iteration")


@contextmanager
def launch_binding(loop: dict[str, Any]) -> Iterator[None]:
  """Bind an idempotent cycle/attempt to the existing accountable dispatch path."""
  token = LAUNCH.set({"loop_id": loop["id"], "task_id": loop["task_id"], "session_id": loop["session_id"],
    "cycle": loop["cycle"], "attempt": loop["attempt"], "key": loop["pending"]["key"],
    "authorized_agents": loop["authorized_agents"]})
  try:
    yield
  finally:
    LAUNCH.reset(token)


def usage(loop: dict[str, Any]) -> dict[str, Any]:
  """Count all attempted children, including failures; unknown usage blocks automatic continuation."""
  jobs = [st.job(j) for j in ac.load(loop["task_id"])["jobs"]]
  values = [ac.job_usage(j) for j in jobs if attempts.model_attempted(j)]
  return {"tokens": ac.totals([v for v in values if v]), "unknown_attempts": sum(v is None for v in values),
          "model_attempts": len(values), "scope": "All loop worker attempts; parent coordination is separate, not zero or equivalent money"}


def child(loop: dict[str, Any], job_id: str = "") -> dict[str, Any] | None:
  """Never steer/cancel/collect a job unless its durable loop ownership matches."""
  jid = job_id or loop.get("current_job")
  if not jid:
    return None
  job = st.job(jid)
  if job.get("loop_id") != loop["id"] or job["session_id"] != loop["session_id"]:
    raise ValueError("Persistent task does not own this child")
  return job


def gate(loop: dict[str, Any], name: str, stop: bool = False) -> str | None:
  """Read one exact small regular manifest without symlink traversal or arbitrary commands."""
  if not name:
    return None
  if name.startswith("workspace:"):
    relative = name[len("workspace:"):]
    if not stop or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
      raise ValueError("workspace: supports only an exact relative stop receipt inside the owned child cwd")
    current = child(loop) or (child(loop, loop["previous_job"]) if loop.get("previous_job") else None)
    if current is None:
      return None
    root = Path(current["cwd"]).resolve()
    name = relative
  else:
    root = Path(loop["run_spec"]["cwd"]).resolve()
  path = Path(name) if Path(name).is_absolute() else root / name
  lexical = Path(os.path.abspath(path))
  if not lexical.is_relative_to(root):
    raise ValueError("Loop manifest must stay inside its declared source/owned worker cwd")
  item = source_inputs.read(root, str(lexical.relative_to(root)))
  if item is None:
    return None
  if len(item[0]) > 65536:
    raise ValueError("Loop manifest exceeds64KiB")
  if stop:
    try:
      value = json.loads(item[0])
    except ValueError:
      return None  # A partially published receipt is not a completion signal.
    if not isinstance(value, dict) or value.get("done") is not True:
      return None
  return hashlib.sha256(item[0]).hexdigest()


def validate_spec(session_id: str, spec: dict[str, Any], one_shot: bool, mode: str) -> list[str]:
  """Validate bounded user intent, without querying models/accounts or inferring commit permission."""
  import service
  forbidden = {"session_id", "resume_job", "work_item", "plan_id", "one_shot", "wait_seconds", "background", "notify",
               "await_completion", "allow_commit", "commit_message", "commit_paths"}
  if not isinstance(spec, dict) or forbidden & set(spec):
    raise ValueError("run_spec uses normal task/model/effort/cwd fields; no session/resume/work-item/commit overrides")
  try:
    inspect.signature(service.run).bind(session_id=session_id, **spec)
  except TypeError as exc:
    raise ValueError("Invalid run_spec: " + str(exc)) from None
  for key in ("agent", "model", "effort", "task", "acceptance", "rationale", "cwd"):
    if not isinstance(spec.get(key), str) or not spec[key].strip():
      raise ValueError("run_spec requires nonempty " + key)
  if any(len(spec[k]) > 200 for k in ("agent", "model", "effort")):
    raise ValueError("Persistent model/role identifiers must be at most200 characters")
  if not Path(spec["cwd"]).is_dir():
    raise ValueError("run_spec cwd must exist")
  cfg, state = st.config(), st.session(session_id)
  if not one_shot and state["mode"] != "auto":
    raise ValueError("Persistent execution requires session opt-in or an explicit one_shot request")
  if spec["agent"] == "auto":
    if state["mode"] != "auto" or spec["model"] != "auto" or spec["effort"] != "auto":
      raise ValueError("Automatic persistent selection requires auto session and agent/model/effort=auto")
    names = [n for n, a in cfg["agents"].items() if a["enabled"] and a["automatic"] and cfg["providers"][a["provider"]]["enabled"]]
  else:
    role = cfg["agents"].get(spec["agent"])
    if not role or not role["enabled"] or not cfg["providers"][role["provider"]]["enabled"]:
      raise ValueError("Persistent-task agent/provider is missing or disabled")
    if (mode == "loop" or not one_shot) and not role["automatic"]:
      raise ValueError("Automatic repetition cannot enable a manual-only role; use supervised one-shot steps")
    names = [spec["agent"]]
  if state["agents"] is not None:
    names = [n for n in names if n in state["agents"]]
  if not names:
    raise ValueError("No agent in the authorized session pool")
  return names


def describe(loop: dict[str, Any]) -> dict[str, Any]:
  """Return supervision state, spending and the current child without repeating prompts."""
  import service
  current = child(loop) or (child(loop, loop["previous_job"]) if loop.get("previous_job") else None)
  summary = service.summary(current) if current else {}
  public_child = {k: v for k, v in summary.items() if k in {
    "id", "agent", "model", "effort", "status", "review", "error", "runtime", "attempt", "record_path", "job_runtime_version"}}
  if public_child.get("error") and len(str(public_child["error"])) > 2000:
    public_child.update(error=str(public_child["error"])[:2000], error_truncated=True)
  host = st.session(loop["session_id"]).get("host_kind", "claude")
  return {k: loop.get(k) for k in ("id", "session_id", "mode", "status", "requested", "note", "task_id", "cycle",
    "attempt", "completed_cycles", "max_cycles", "token_budget", "interval_seconds", "next_run_at", "current_job",
    "event_seq", "review", "goal_acceptance", "controller_identity", "heartbeat", "runtime_image")} | {
    "controller_alive": alive(loop.get("controller_identity")), "usage": usage(loop),
    "controller_heartbeat_age_seconds": round(max(0, time.time() - loop["heartbeat"]), 1) if loop.get("heartbeat") else None,
    "goal_acceptance": loop["goal_acceptance"][:4000], "goal_acceptance_truncated": len(loop["goal_acceptance"]) > 4000,
    "child": public_child or None, "child_is_previous": bool(current and not loop.get("current_job")),
    "notice_after": loop.get("ack_seq", 0),
    "wait_command": "delegate-agent loop-wait" + (" " + host if host in {"codex", "dsh"} else ""),
    "wait_stdin": {"session_id": loop["session_id"], "loop_id": loop["id"], "after": loop.get("ack_seq", 0)},
    "record_path": str(st.root() / ("loop-" + loop["id"] + ".json")),
    "controller_log": str(st.root() / ("loop-stderr-" + loop["id"] + ".txt")),
    "acceptance_note": "Cycle completion, stop conditions and cycle limits are not acceptance; collect results and review evidence"}


def ensure_controller(loop_id: str) -> None:
  """Start a frozen owned controller; the OS lock prevents duplicate launch after crashes."""
  with st.locked():
    loop = load(loop_id)
    if alive(loop.get("controller_identity")):
      return
    starting = loop.get("controller_starting") or {}
    if alive(starting.get("launcher")) or alive(starting.get("child")):
      return
    if loop["requested"] == "run":
      other = [load(i) for i in st.session(loop["session_id"]).get("loops", []) if i != loop_id]
      if sum(item["status"] not in IDLE for item in other) >= 8:
        raise ValueError("Eight persistent controllers are already scheduled; pause one before resuming")
    token = uuid.uuid4().hex
    loop["controller_starting"] = {"token": token, "launcher": identity(os.getpid()), "at": time.time()}
    st.write("loop-" + loop_id + ".json", loop)
  path = st.root() / ("loop-stderr-" + loop_id + ".txt")
  try:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as error:
      proc = subprocess.Popen([sys.executable, str(Path(loop["runtime_image"]["path"]) / "delegate.py"), "loop-worker", loop_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error, start_new_session=True)
    with st.locked():
      loop = load(loop_id)
      if (loop.get("controller_starting") or {}).get("token") == token:
        loop["controller_starting"] = {"token": token, "child": identity(proc.pid, include_zombies=True), "at": time.time()}
        st.write("loop-" + loop_id + ".json", loop)
  except BaseException:
    update(loop_id, controller_starting=None)
    raise


def _adopt(loop: dict[str, Any]) -> dict[str, Any] | None:
  """Find only the durable child of this exact launch intent; never replay ambiguous inference."""
  import registration
  key = (loop.get("pending") or {}).get("key")
  with st.locked():
    registration.recover_locked()
    matches = [st.job(j) for j in ac.load(loop["task_id"])["jobs"] if st.job(j).get("loop_launch_key") == key]
  if len(matches) > 1:
    raise ValueError("Multiple children for one launch intent; inspect before recovery")
  return matches[0] if matches else None


def tick(loop_id: str) -> bool:
  """Advance deterministic supervision once; return false when the controller should exit."""
  import service
  loop = load(loop_id)
  current = child(loop)
  if current is None and loop.get("pending"):
    adopted = _adopt(loop)
    if adopted:
      loop = update(loop_id, current_job=adopted["id"], resolved_agent=adopted.get("agent"),
        last_trigger=(loop.get("pending") or {}).get("trigger", loop.get("last_trigger")))
      current = child(loop)
  owner = st.session(loop["session_id"])
  disabled = owner.get("mode_revision", 0) != loop["mode_revision"] and owner["mode"] != "auto"
  if disabled and loop["requested"] == "run":
    loop = update(loop_id, requested="pause", note="Parent disabled new delegation; current child may finish")
  if current and current["status"] in ACTIVE - {"orphaned"}:
    current = reconcile(current["id"])
    if loop["requested"] == "cancel" and current["status"] in ACTIVE:
      service.jobs(loop["session_id"], "cancel", current["id"])
    if loop["requested"] == "run":
      observed = ac.job_usage(current)
      total = loop.get("spent_before_dispatch", 0) + (ac.totals([observed])["processed_tokens"] if observed else 0)
      alert_key = current["id"] + ":" + str(loop["token_budget"])
      if loop["token_budget"] and observed and total >= loop["token_budget"] and loop.get("budget_alert") != alert_key:
        event(loop_id, "running", "Observed token threshold reached during the current call; inspect/steer/cancel if needed. This is not a hard in-flight cap.", budget_alert=alert_key)
      warnings = [w for w in runtime.observe(current)["warnings"] if "heartbeat stale" in w or "no recent observable progress" in w]
      signature = hashlib.sha256((current["id"] + str(warnings)).encode()).hexdigest() if warnings else None
      if signature != loop.get("supervision_alert"):
        if warnings:
          event(loop_id, "running", "Runtime observation: " + "; ".join(warnings) + "; inspect, do not assume a stall or replay",
                supervision_alert=signature)
        else:
          update(loop_id, supervision_alert=None)
    return True
  if current and current["status"] == "orphaned":
    event(loop_id, "blocked", "Child cleanup is unconfirmed; inspect/cancel it before recovery")
    return False
  if current and (alive(current.get("worker_identity")) or group_running(current.get("backend_identity"))):
    return True
  if loop["requested"] == "cancel":
    event(loop_id, "cancelled", "Scheduling cancelled; owned child stopped", pending=loop.get("pending") if current else None,
      resolved_no_child=(loop.get("pending") or {}).get("key") if not current else None)
    return False
  if loop["requested"] == "pause":
    event(loop_id, "paused", "Paused without starting another model call", pending=loop.get("pending") if current else None,
      resolved_no_child=(loop.get("pending") or {}).get("key") if not current else None)
    return False
  if current and (current["status"] != "completed" or current.get("error") or current.get("review") in {"blocked", "needs-work"}):
    event(loop_id, "blocked", "Child failed or needs work; explicitly retry the same cycle after inspection",
          settled_job=current["id"])
    return False
  if current and loop.get("settled_job") != current["id"]:
    cycles = sorted(set(loop.get("completed_cycle_ids", []) + [loop["cycle"]]))
    count = len(cycles)
    status = "completed" if count >= loop["max_cycles"] else "awaiting_review" if loop["mode"] == "persistent" else "waiting"
    loop = event(loop_id, status, "Cycle ended; result collection and acceptance remain required", completed_cycles=count,
      completed_cycle_ids=cycles,
      settled_job=current["id"], next_run_at=time.time() + loop["interval_seconds"])
    if status in IDLE:
      return False
    settled_usage = usage(loop)
    if settled_usage["unknown_attempts"] or (loop["token_budget"] and settled_usage["tokens"]["processed_tokens"] >= loop["token_budget"]):
      event(loop_id, "blocked", "Unknown usage or token budget reached; no additional inference authorized")
      return False
  if loop["status"] in IDLE:
    return False
  if (current and loop["completed_cycles"] >= loop["max_cycles"]) or loop["cycle"] > loop["max_cycles"]:
    event(loop_id, "completed", "Cycle limit reached; no new inference authorized")
    return False
  if current and loop["mode"] == "persistent" and loop.get("settled_job") == current["id"]:
    event(loop_id, "awaiting_review", "Supervised steps require explicit collected/accepted advance, not resume")
    return False
  if current and current.get("review") in {"needs-work", "blocked"}:
    event(loop_id, "blocked", "Parent review blocks automatic continuation")
    return False
  if gate(loop, loop["stop_file"], stop=True):
    event(loop_id, "stopped", "Stop receipt observed; scheduling stopped, goal still needs parent acceptance")
    return False
  if loop.get("next_run_at", 0) > time.time():
    return True
  trigger = gate(loop, loop["trigger_file"]) if loop["trigger_file"] else None
  if loop["trigger_file"] and (trigger is None or trigger == loop.get("last_trigger")):
    return True
  spent = usage(loop)
  if spent["unknown_attempts"] or (loop["token_budget"] and spent["tokens"]["processed_tokens"] >= loop["token_budget"]):
    event(loop_id, "blocked", "Unknown usage or token budget reached; no additional inference authorized")
    return False
  active = [reconcile(j) for j in st.read("active.json", [])]
  if sum(j["status"] in ACTIVE for j in active) >= st.config()["max_concurrency"]:
    return True
  if current:
    loop = update(loop_id, cycle=loop["cycle"] + 1, attempt=1, previous_job=current["id"], current_job=None, pending=None)
  if not loop.get("pending"):
    loop = update(loop_id, pending={"key": uuid.uuid4().hex, "trigger": trigger}, status="dispatching")
  loop = update(loop_id, spent_before_dispatch=spent["tokens"]["processed_tokens"])
  adopted = _adopt(loop)
  if adopted:
    update(loop_id, current_job=adopted["id"], resolved_agent=adopted.get("agent"), status="running",
      last_trigger=loop["pending"].get("trigger", loop.get("last_trigger")))
    return True
  if loop["pending"].get("trigger") != trigger:
    loop = update(loop_id, pending={**loop["pending"], "trigger": trigger})
  spec = dict(loop["run_spec"])
  previous = child(loop, loop.get("previous_job", "")) if loop.get("previous_job") else None
  if loop.get("resolved_agent"):
    spec["agent"] = loop["resolved_agent"]
  if previous and previous.get("thread_id"):
    spec["resume_job"] = previous["id"]
  validate_spec(loop["session_id"], {k: v for k, v in spec.items() if k != "resume_job"}, loop["one_shot"], loop["mode"])
  with launch_binding(loop):
    receipt = service.run(session_id=loop["session_id"], one_shot=loop["one_shot"],
      work_item=loop_id + "-c" + str(loop["cycle"]), notify=True, **spec)
  if not receipt.get("id") or receipt.get("action"):
    event(loop_id, "blocked", "Planner returned " + str(receipt.get("action")) + "; parent action required; no forced worker")
    return False
  update(loop_id, current_job=receipt["id"], resolved_agent=receipt["agent"], status="running", last_trigger=trigger)
  return True


def run(loop_id: str) -> None:
  """Own one restartable supervisor process; no model calls while sleeping or awaiting parent review."""
  os.umask(0o077)
  lock = st.root() / ("loop-controller-" + st.key(loop_id) + ".lock")
  with lock.open("a") as stream:
    try:
      fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return
    runtime_image.require_worker(load(loop_id), Path(__file__).resolve().parent)
    update(loop_id, controller_identity=identity(os.getpid()), controller_starting=None, heartbeat=time.time())
    try:
      last_heartbeat = time.monotonic()
      while tick(loop_id):
        if time.monotonic() - last_heartbeat >= 10:
          update(loop_id, heartbeat=time.time())
          last_heartbeat = time.monotonic()
        time.sleep(5)
    except Exception as exc:
      event(loop_id, "blocked", type(exc).__name__ + ": " + str(exc)[:1500])
    finally:
      update(loop_id, heartbeat=time.time())


def notice(loop: dict[str, Any], force: bool = False) -> str:
  """Deliver a stable supervision event without collecting a child or accepting a goal."""
  name = "loop-notice-" + loop["id"] + ".json"
  with st.locked():
    loop = load(loop["id"])
    seq = loop.get("event_seq", 0)
    if seq <= loop.get("ack_seq", 0):
      return ""
    delivery = st.read(name, {})
    if not force and delivery.get("seq") == seq and time.time() - delivery.get("at", 0) < 60:
      return ""
    st.write(name, {"seq": seq, "at": time.time()})
  return ("persistent-" + loop["id"] + "-" + str(seq) + ": " + loop["status"] + "; " + loop.get("note", "") +
    ". Inspect delegate_loop status/result, collect all pages and review. Current child=" + str(loop.get("current_job")) +
    "; no goal was automatically accepted.")


def wait(payload: dict[str, Any]) -> dict[str, Any]:
  """Wait for the next durable supervision event in code; no inference or acknowledgement."""
  sid, lid = payload["session_id"], payload["loop_id"]
  after = payload.get("after", 0)
  if type(after) is not int or after < 0:
    raise ValueError("after must be a non-negative event sequence")
  while True:
    loop = load(lid)
    if loop["session_id"] != sid:
      raise ValueError("Persistent task belongs to another session")
    if loop.get("event_seq", 0) > after or loop["status"] in TERMINAL:
      return {"id": lid, "session_id": sid, "event_seq": loop.get("event_seq", 0),
        "status": loop["status"], "current_job": loop.get("current_job"), "note": loop.get("note"),
        "next_action": "delegate_loop status/result/verify; notification is not collection or goal acceptance"}
    controller = loop.get("controller_identity")
    starting = loop.get("controller_starting") or {}
    if loop["status"] not in IDLE and not alive(controller) and not alive(starting.get("child")) and not alive(starting.get("launcher")):
      return {"id": lid, "session_id": sid, "status": "controller_lost", "next_action": "delegate_loop recover; existing child will be adopted, not replayed"}
    time.sleep(2)


def returned_loop(value: Any) -> dict[str, Any] | None:
  """Decode only structured receipts, not textual mentions of a persistent-task ID."""
  if isinstance(value, str):
    try:
      return returned_loop(json.loads(value))
    except ValueError:
      return None
  if isinstance(value, list):
    return next((r for item in value if (r := returned_loop(item)) is not None), None)
  if isinstance(value, dict) and not value.get("isError") and not value.get("is_error"):
    if isinstance(value.get("id"), str) and value["id"].startswith("l") and value.get("status"):
      return value
    for key in ("content", "text", "result", "structuredContent"):
      if key in value:
        found = returned_loop(value[key])
        if found:
          return found
  return None


def watch(payload: dict[str, Any]) -> bool:
  """Use the host's asyncRewake hook for the next persistent supervision event."""
  from rewake import returned_job
  name = str(payload.get("tool_name", ""))
  args = payload.get("tool_input", {})
  sid = payload.get("session_id")
  if (payload.get("agent_id") or not name.endswith("__delegate_loop") or not isinstance(args, dict) or not sid
      or args.get("session_id") != sid or args.get("action", "list") in {"list", "finish"}):
    return False
  response = returned_loop(payload.get("tool_response")) or returned_job(payload.get("tool_response"))
  if response is None:
    return False
  lid = args.get("loop_id") or (response or {}).get("id", "")
  if not isinstance(lid, str) or not lid.startswith("l"):
    return False
  loop = load(lid)
  if loop["session_id"] != sid:
    return False
  lock = st.root() / ("loop-rewake-" + st.key(lid) + ".lock")
  with lock.open("a") as stream:
    try:
      fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return False
    after = (response or {}).get("notice_after", loop.get("ack_seq", 0))
    notification = wait({"session_id": sid, "loop_id": lid, "after": after})
    if notification["status"] == "controller_lost":
      print("Persistent controller stopped; inspect/recover " + lid + ". No model was automatically replayed.", file=sys.stderr, flush=True)
      return True
    note = notice(load(lid))
    if note:
      print(note, file=sys.stderr, flush=True)
      return True
    return False


def manage(session_id: str, action: str = "list", loop_id: str = "", mode: str = "persistent",
           run_spec: dict[str, Any] | None = None, max_cycles: int | None = None,
           interval_seconds: int | None = None, token_budget: int | None = None,
           trigger_file: str = "", stop_file: str = "", one_shot: bool = False,
           job_id: str = "", message: str = "", verdict: str = "", evidence: str = "",
           result_offset: int = 0, result_limit: int = 24000, result_sha256: str = "", result_view: str = "auto") -> Any:
  """Create/control explicit bounded persistent work; only dispatching a child can consume inference."""
  import service
  st.key(session_id)
  if type(one_shot) is not bool:
    raise ValueError("one_shot must be boolean")
  if action == "list":
    rows = [load(i) for i in st.session(session_id).get("loops", [])[-50:]]
    return [{k: row.get(k) for k in ("id", "mode", "status", "requested", "current_job", "completed_cycles", "max_cycles")}
      | {"controller_alive": alive(row.get("controller_identity"))} for row in rows]
  if action == "start":
    if mode not in {"persistent", "loop"} or type(one_shot) is not bool:
      raise ValueError("mode must be persistent/loop and one_shot boolean")
    max_cycles = 3 if max_cycles is None else max_cycles
    interval_seconds = 300 if interval_seconds is None else interval_seconds
    token_budget = 0 if token_budget is None else token_budget
    if type(max_cycles) is not int or not 1 <= max_cycles <= 100:
      raise ValueError("max_cycles must be1..100; automatic infinite inference is not supported")
    if type(interval_seconds) is not int or not 30 <= interval_seconds <= 86400:
      raise ValueError("interval_seconds must be30..86400")
    if type(token_budget) is not int or token_budget < 0 or (mode == "loop" and not token_budget):
      raise ValueError("loop mode requires a positive token_budget; supervised persistent mode may use0")
    spec = dict(run_spec or {})
    agents = validate_spec(session_id, spec, one_shot, mode)
    spec["cwd"] = str(Path(spec["cwd"]).resolve(strict=True))
    for name in (trigger_file, stop_file):
      if not isinstance(name, str) or len(name) > 4096 or any(c in name for c in ("\0", "\n", "\r")):
        raise ValueError("Manifest paths must be bounded strings")
    gate({"run_spec": spec}, trigger_file)
    gate({"run_spec": spec}, stop_file, stop=True)
    lid = "l" + uuid.uuid4().hex[:16]
    ledger = ac.begin(session_id, label="Persistent: " + spec["task"].splitlines()[0][:180],
      category=spec.get("category") or "persistent", complexity=spec.get("complexity") if spec.get("complexity") in {"trivial","simple","standard","complex"} else "standard",
      activate=False, token_budget=token_budget)
    image = runtime_image.freeze(Path(__file__).resolve().parent)
    value = {"id": lid, "session_id": session_id, "mode": mode, "status": "ready", "requested": "run",
      "run_spec": dict(spec), "goal_acceptance": spec["acceptance"], "one_shot": one_shot, "authorized_agents": agents,
      "mode_revision": st.session(session_id).get("mode_revision", 0), "task_id": ledger["id"], "max_cycles": max_cycles,
      "interval_seconds": interval_seconds, "token_budget": token_budget, "cycle": 1, "attempt": 1,
      "completed_cycles": 0, "trigger_file": trigger_file, "stop_file": stop_file, "event_seq": 0, "ack_seq": 0,
      "runtime_image": image, "created_at": time.time(), "review": "pending", "current_job": None}
    gate(value, trigger_file)
    gate(value, stop_file, stop=True)
    with st.locked():
      state = st.session(session_id)
      ids = state.get("loops", [])
      if sum(load(i)["status"] not in IDLE for i in ids) >= 8 or len(ids) >= 1000:
        raise ValueError("Persistent-task limit reached; pause/finish existing controllers")
      st.write("loop-" + lid + ".json", value)
      state["loops"] = ids + [lid]
      if state.get("active_task"):
        parent = ac.load(state["active_task"])
        parent["persistent_task_ids"] = parent.get("persistent_task_ids", []) + [ledger["id"]]
        ac.save(parent)
      st.save_session(session_id, state)
    ensure_controller(lid)
    return describe(load(lid))
  loop = load(loop_id)
  if loop["session_id"] != session_id:
    raise ValueError("Persistent task belongs to another session; recover with its original session_id")
  if action != "watch" and loop.get("ack_seq", 0) != loop.get("event_seq", 0):
    loop = update(loop_id, ack_seq=loop["event_seq"])
  if action in {"status", "watch"}:
    return describe(loop)
  if action == "limits":
    values = {}
    if max_cycles is not None:
      if type(max_cycles) is not int or not max(1, loop["completed_cycles"]) <= max_cycles <= 100:
        raise ValueError("max_cycles must cover existing cycles and be at most100")
      values["max_cycles"] = max_cycles
    if token_budget is not None:
      if type(token_budget) is not int or token_budget < 0 or (loop["mode"] == "loop" and not token_budget):
        raise ValueError("Automatic loops require a positive token budget")
      values["token_budget"] = token_budget
    if interval_seconds is not None:
      if type(interval_seconds) is not int or not 30 <= interval_seconds <= 86400:
        raise ValueError("interval_seconds must be30..86400")
      values["interval_seconds"] = interval_seconds
    with st.locked():
      loop = load(loop_id)
      if max_cycles is not None and max_cycles < loop["completed_cycles"]:
        raise ValueError("Cannot lower max_cycles below completed cycles")
      loop.update(values, updated_at=time.time())
      st.write("loop-" + loop_id + ".json", loop)
      if token_budget is not None:
        ledger = ac.load(loop["task_id"])
        ledger["token_budget"] = token_budget
        ac.save(ledger)
    return describe(loop)
  if action in {"result", "verify", "tell", "inspect"}:
    current = child(loop, job_id or loop.get("current_job") or loop.get("previous_job", ""))
    if not current:
      raise ValueError("No child has started")
    return service.jobs(session_id, action, current["id"], message=message, verdict=verdict, evidence=evidence,
      result_offset=result_offset, result_limit=result_limit, result_sha256=result_sha256, result_view=result_view)
  if action == "pause":
    update(loop_id, requested="pause")
    ensure_controller(loop_id)
  elif action == "cancel":
    update(loop_id, requested="cancel")
    current = child(loop)
    if current and current["status"] in ACTIVE:
      service.jobs(session_id, "cancel", current["id"])
    ensure_controller(loop_id)
  elif action in {"resume", "recover", "advance", "retry"}:
    current = child(loop)
    if alive(loop.get("controller_identity")):
      if action in {"resume", "recover"} and loop["requested"] == "run" and not run_spec:
        return {**describe(loop), "recovery": "Reattached existing controller; no model replay"}
      raise ValueError("Controller is still supervising/settling; pause and wait for its exit before changing steps")
    if action in {"advance", "retry"}:
      if current and (current["status"] in ACTIVE or alive(current.get("worker_identity")) or group_running(current.get("backend_identity"))):
        raise ValueError("Current child has not stopped; supervise/cancel before advancing")
      if action == "advance" and (not current or current.get("review") != "accepted" or not current.get("collected_at")):
        raise ValueError("Advance requires collected and accepted step evidence; goal acceptance remains separate")
      if action == "advance" and loop["completed_cycles"] >= loop["max_cycles"]:
        raise ValueError("Cycle limit reached; explicitly increase limits before advancing")
      if action == "retry" and (not current or (current["status"] == "completed" and current.get("review") != "needs-work")):
        raise ValueError("Retry requires failed execution or needs-work review; it retains the same cycle/work_item")
    spec = {**loop["run_spec"], **(run_spec or {})}
    spec["cwd"] = str(Path(spec["cwd"]).resolve(strict=True))
    if loop.get("resolved_agent") and spec["cwd"] != loop["run_spec"]["cwd"]:
      raise ValueError("Persistent conversation retains its original source cwd")
    if loop.get("resolved_agent") and spec["agent"] not in {"auto", loop["resolved_agent"]}:
      raise ValueError("Persistent conversation retains its resolved role; use a separate task for another role/provider")
    validate_spec(session_id, spec, loop["one_shot"] or one_shot, loop["mode"])
    values = {"requested": "run", "status": "ready", "run_spec": spec,
              "mode_revision": st.session(session_id).get("mode_revision", 0), "one_shot": loop["one_shot"] or one_shot}
    if action in {"advance", "retry"}:
      values.update(previous_job=current["id"], current_job=None, pending=None, next_run_at=0,
        cycle=loop["cycle"] + (action == "advance"), attempt=1 if action == "advance" else loop["attempt"] + 1)
    update(loop_id, **values)
    ensure_controller(loop_id)
  elif action == "finish":
    if (alive(loop.get("controller_identity")) or (loop.get("pending") and not loop.get("current_job"))
        or (loop["requested"] == "run" and loop["status"] not in IDLE)):
      raise ValueError("Pause/recover the controller and resolve its pending launch before finishing")
    current = child(loop) or (child(loop, loop["previous_job"]) if loop.get("previous_job") else None)
    if current and (current["status"] in ACTIVE or alive(current.get("worker_identity")) or group_running(current.get("backend_identity"))):
      raise ValueError("Stop the child before finishing this persistent task")
    if verdict not in {"accepted", "needs-work", "blocked"} or not evidence.strip():
      raise ValueError("Finish requires a verdict and actual goal evidence")
    if verdict == "accepted" and current and (current["status"] != "completed" or current.get("error")
        or not current.get("collected_at") or current.get("review") != "accepted"):
      raise ValueError("Goal acceptance requires collecting/accepting the latest step and actual goal evidence")
    event(loop_id, "completed", "Parent recorded goal verdict", requested="pause", review=verdict, evidence=evidence[:4000])
    with st.locked():
      ledger = ac.load(loop["task_id"])
      ledger.update(status="closed", verdict=verdict, evidence=evidence[:4000], finished_at=time.time())
      ac.save(ledger)
  else:
    raise ValueError("Unknown persistent-task action")
  return describe(load(loop_id))

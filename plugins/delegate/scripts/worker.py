"""Owned background workers with durable results and isolated write worktrees."""
from pathlib import Path
from typing import Any
import json
import os
import signal
import subprocess
import sys
import time
import uuid
import tempfile

import storage as st
from codex_rpc import CodexRPC
from lifecycle import identity, group_running, stop_owned
import thread_cleanup
import credentials


def git(cwd: str, *args: str) -> str:
  """Run a bounded Git operation using argument arrays, never a shell."""
  return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                        text=True, timeout=30).stdout.strip()


def prepare(value: dict[str, Any]) -> str:
  """Require a clean source before creating a detached write worktree."""
  if value.get("resume_job") or value["role"]["workspace"] == "read-only":
    return value["cwd"]
  source = value["source_cwd"]
  top = Path(git(source, "rev-parse", "--show-toplevel")).resolve()
  if git(str(top), "status", "--porcelain", "--untracked-files=normal"):
    raise ValueError("Write delegation requires a clean source repository. Preserve current changes first; "
                     "or use a read-only agent to analyze the current dirty tree.")
  if git(str(top), "ls-files", "--stage").find("160000 ") >= 0:
    raise ValueError("Choose the actual nested repository; automatic submodule checkout is not supported")
  worktree = st.root() / "worktrees" / value["id"]
  worktree.parent.mkdir(mode=0o700, exist_ok=True)
  base = git(str(top), "rev-parse", "HEAD")
  git(str(top), "worktree", "add", "--detach", str(worktree), base)
  # Keep the original relative cwd when delegating a repository subdirectory.
  cwd = str(worktree / Path(source).relative_to(top))
  st.update_job(value["id"], worktree=str(worktree), base_commit=base, cwd=cwd)
  return cwd


def packet(value: dict[str, Any]) -> str:
  """Carry explicit context and ancestor instruction files into every backend."""
  if value.get("resume_job"):
    return "\n\n".join(["Continue the prior role and constraints in the same workspace. "
      "Read any project instructions changed since the previous turn. Do not spawn other agents.",
      "FOLLOW-UP\n" + value["task"], "ACCEPTANCE\n" + value["acceptance"], "NEW CONTEXT\n" + value["context"]])
  instruction_parts = []
  source = Path(value["source_cwd"])
  for directory in reversed([source, *source.parents]):
    for name in ("AGENTS.md", "CLAUDE.md"):
      path = directory / name
      if path.is_file():
        text = path.read_text()
        if len(text) > 40000:
          raise ValueError("Instruction file too large for automatic handoff: " + str(path))
        instruction_parts.append(str(path) + "\n" + text)
  return "\n\n".join([
    "You are a delegated worker. Complete only this task. Do not spawn more agents. "
    "Do not commit, push, merge, send messages, change credentials/configuration, or delete user files. "
    "Do not recursively scan home or filesystem roots. Read applicable child AGENTS.md/CLAUDE.md. "
    "The parent will verify your work. Report changed paths, exact tests and results, unresolved issues. "
    "If permissions prevent a check, report the limitation; do not bypass permissions.",
    "ROLE\n" + value["role"]["instructions"],
    "INHERITED PROJECT INSTRUCTIONS\n" + "\n\n".join(instruction_parts),
    "TASK\n" + value["task"], "ACCEPTANCE\n" + value["acceptance"],
    "CONTEXT\n" + value["context"],
  ])


def message_status(job_id: str, index: int, status: str, error: str = "") -> None:
  """Update just one queued steering message without losing later messages."""
  with st.locked():
    value = st.job(job_id)
    value["messages"][index].update(status=status, error=error)
    st.write("job-" + job_id + ".json", value)


def codex_worker(value: dict[str, Any], prompt: str) -> dict[str, Any]:
  """Run or resume a Codex turn, capturing final status, output and token usage."""
  job_id = value["id"]
  deadline = time.monotonic() + value["timeout_seconds"]
  sandbox = "read-only" if value["role"]["workspace"] == "read-only" else "workspace-write"
  st.update_job(job_id, backend_starting=True)
  def registered(pid: int) -> None:
    st.update_job(job_id, backend_identity=identity(pid, include_zombies=True), backend_starting=False)
  try:
    connection = CodexRPC(value["provider"]["command"], on_spawn=registered)
  except FileNotFoundError:
    st.update_job(job_id, backend_starting=False)
    raise
  with connection as rpc:
    account = rpc.call("account/read", {"refreshToken": False}).get("account")
    if not account or account.get("type") != "chatgpt":
      raise ValueError("ChatGPT authentication required; paid API fallback is disabled")
    params = {"model": value["model"], "cwd": value["cwd"], "sandbox": sandbox,
              "approvalPolicy": "never", "modelProvider": "openai",
              "config": {"model_reasoning_effort": value["effort"]}}
    if value.get("thread_id"):
      thread_cleanup.restore(rpc, value["thread_id"], value["session_id"])
      params.update(threadId=value["thread_id"], excludeTurns=True)
      response = rpc.call("thread/resume", params)
    else:
      params["serviceName"] = "claude_delegate"
      params["threadSource"] = "claude_delegate"
      response = rpc.call("thread/start", params)
    thread_id = response["thread"]["id"]
    st.update_job(job_id, thread_id=thread_id)
    if not value.get("resume_job"):
      rpc.call("thread/name/set", {"threadId": thread_id, "name": "[delegate] " + value["agent"] + " " + job_id})
    if st.job(job_id)["cancel_requested"]:
      return {"status": "cancelled"}
    policy = {"type": "readOnly"} if sandbox == "read-only" else {
      "type": "workspaceWrite", "writableRoots": [value["cwd"]], "networkAccess": False}
    response = rpc.call("turn/start", {"threadId": thread_id, "model": value["model"],
      "effort": value["effort"], "cwd": value["cwd"], "approvalPolicy": "never", "sandboxPolicy": policy,
      "serviceTierForTurn": "default", "input": [{"type": "text", "text": prompt}]})
    turn_id = response["turn"]["id"]
    st.update_job(job_id, turn_id=turn_id)
    final_messages: dict[str, str] = {}
    last_control = 0.0
    last_heartbeat = 0.0
    stop_reason = None
    stop_at = None
    while True:
      now = time.monotonic()
      if now - last_control >= 1:
        current = st.job(job_id)
        last_control = now
        if not stop_reason and (current["cancel_requested"] or now >= deadline):
          stop_reason = "cancelled" if current["cancel_requested"] else "timed_out"
          stop_at = now
          st.update_job(job_id, status="cancelling")
          rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
        if not stop_reason:
          for i, message in enumerate(current["messages"]):
            if message["status"] == "queued":
              try:
                rpc.call("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                         "input": [{"type": "text", "text": message["text"]}]}, timeout=10)
                message_status(job_id, i, "delivered")
              except Exception as exc:
                message_status(job_id, i, "not-delivered", str(exc))
        if now - last_heartbeat > 10:
          st.update_job(job_id, heartbeat=time.time())
          last_heartbeat = now
      if stop_at and now - stop_at > 15:
        outcome = {"status": stop_reason, "result": "\n\n".join(final_messages.values())}
        break
      event = rpc.receive(timeout=0.5)
      if not event:
        continue
      if "id" in event and "method" in event:
        rpc.deny(event)
        continue
      method = event.get("method", "")
      data = event.get("params", {})
      if data.get("threadId") not in {None, thread_id}:
        continue
      if data.get("turnId") not in {None, turn_id}:
        continue
      if method == "item/completed":
        item = data.get("item", {})
        if item.get("type") == "agentMessage":
          final_messages[item.get("id", str(len(final_messages)))] = item.get("text", "")
        st.update_job(job_id, last_event=item.get("type", "item"), heartbeat=time.time())
      elif method == "thread/tokenUsage/updated":
        usage = data.get("tokenUsage") or {}
        before = value.get("usage_before", {})
        delta = {k: n - before.get(k, 0) for k, n in usage.get("total", {}).items()
                 if isinstance(n, (int, float))} if isinstance(before, dict) else None
        st.update_job(job_id, usage=usage, usage_delta=delta)
      elif method == "turn/completed":
        turn = data["turn"]
        if turn["id"] != turn_id:
          continue
        status = stop_reason or {"completed": "completed", "interrupted": "cancelled",
                                 "failed": "failed"}.get(turn["status"], "failed")
        outcome = {"status": status, "result": "\n\n".join(final_messages.values()), "error": turn.get("error")}
        break
    # No extra quota fetch per token/event: one bounded read when the task ends.
    try:
      raw = rpc.call("account/rateLimits/read", timeout=10)
      raw.pop("accountId", None)
      st.update_job(job_id, quota_after={"observed_at": time.time(), "data": raw})
    except Exception:
      pass
  return outcome


def claude_command(value: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
  """Build a scoped Claude/Anthropic-compatible CLI worker without recursive plugins."""
  env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
  for name in ("CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_OAUTH_TOKEN",
               "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
               "CLAUDE_CODE_API_TYPE", "CLAUDE_CODE_HEADERS", "CLAUDE_CODE_EFFORT_LEVEL"):
    env.pop(name, None)
  env.update(CLAUDE_CODE_SAFE_MODE="1", DELEGATE_WORKER="1")
  provider = value["provider"]
  if provider["kind"] == "anthropic":
    env.update(credentials.environment(provider))
    # New API jobs keep CLI histories/settings in plugin state, not the main profile.
    # Legacy resumes retain their original home to keep their session accessible.
    if not value.get("resume_job") or value.get("backend_config_dir"):
      env["CLAUDE_CONFIG_DIR"] = value.get("backend_config_dir") or str(
        st.root() / "backend-state" / st.key(value.get("provider_name", provider.get("bridge_account", "anthropic"))))
  tools = "Read,Glob,Grep"
  if value["role"]["workspace"] == "worktree":
    tools += ",Edit,Write"
  command = [provider["command"], "-p", "--output-format", "json", "--model", value["model"],
             "--effort", value["effort"], "--restricted", "--permission-mode", "dontAsk",
             "--tools", tools, "--allowedTools", tools, "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}']
  if value.get("thread_id"):
    command.extend(["--resume", value["thread_id"]])
  else:
    command.extend(["--session-id", str(uuid.uuid4())])
  return command, env


def claude_worker(value: dict[str, Any], prompt: str) -> dict[str, Any]:
  """Execute a bounded file-tool worker; retain its session ID for follow-up."""
  job_id = value["id"]
  import cli_history
  cli_history.restore(value)
  command, env = claude_command(value)
  config_dir = env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
  Path(config_dir).mkdir(parents=True, mode=0o700, exist_ok=True)
  st.update_job(job_id, backend_config_dir=config_dir)
  thread_id = command[-1]
  st.update_job(job_id, thread_id=thread_id)
  output_path = st.root() / ("output-" + job_id + ".json")
  error_path = st.root() / ("stderr-" + job_id + ".txt")
  with tempfile.TemporaryFile(mode="w+") as output, tempfile.TemporaryFile(mode="w+") as error, tempfile.TemporaryFile(mode="w+") as input_file:
    input_file.write(prompt)
    input_file.seek(0)
    st.update_job(job_id, backend_starting=True)
    try:
      proc = subprocess.Popen(command, cwd=value["cwd"], env=env, stdin=input_file,
                              stdout=output, stderr=error, text=True, start_new_session=True)
    except OSError:
      st.update_job(job_id, backend_starting=False)
      raise
    process_identity = identity(proc.pid, include_zombies=True)
    try:
      st.update_job(job_id, backend_identity=process_identity, backend_starting=False)
      deadline = time.monotonic() + value["timeout_seconds"]
      while proc.poll() is None:
        current = st.job(job_id)
        if current["cancel_requested"] or time.monotonic() >= deadline:
          return {"status": "cancelled" if current["cancel_requested"] else "timed_out"}
        st.update_job(job_id, heartbeat=time.time())
        time.sleep(2)
      output.flush()
      output.seek(0)
      raw = credentials.redact(output.read(), env)
      try:
        result = credentials.redact_data(json.loads(raw), env)
      except ValueError:
        error.seek(0)
        raise RuntimeError("Worker failed: " + credentials.redact(error.read() + raw, env)[-4000:])
      if result.get("api_error") == "usage_limit_reached":
        quota_name = "claude-quota.json" if value["provider"]["kind"] == "claude" else "quota-" + value["provider_name"] + ".json"
        st.write(quota_name, {"status": "limited", "observed_at": time.time(), "model": value["model"],
                             "source": "provider inference response", "reason": result.get("result"),
                             "data": {"api_error": "usage_limit_reached"}})
      return {"status": "failed" if result.get("is_error") or proc.returncode else "completed",
              "result": result.get("result", ""), "usage": result.get("usage"),
              "model_usage": result.get("modelUsage"),
              "error": result.get("api_error"),
              "estimated_cost_usd": result.get("total_cost_usd"), "thread_id": result.get("session_id", thread_id)}
    finally:
      try:
        stop_owned(proc, process_identity)
      finally:
        for stream, path in ((output, output_path), (error, error_path)):
          stream.seek(0)
          raw = stream.read()
          try:
            safe = json.dumps(credentials.redact_data(json.loads(raw), env), ensure_ascii=False)
          except ValueError:
            safe = credentials.redact(raw, env)
          path.write_text(safe)


def main(job_id: str) -> None:
  """Run exactly one job and always leave a terminal record on handled failures."""
  os.umask(0o077)
  outcome = {"status": "failed", "error": "Worker stopped before completion"}
  def interrupted(signum: int, frame: Any) -> None:
    raise InterruptedError("Worker interrupted by signal " + str(signum))
  signal.signal(signal.SIGTERM, interrupted)
  try:
    value = st.job(job_id)
    if value["cancel_requested"]:
      outcome = {"status": "cancelled"}
      return
    st.update_job(job_id, status="running", heartbeat=time.time(), worker_pid=os.getpid(), worker_identity=identity(os.getpid()))
    cwd = prepare(value)
    value = st.job(job_id)
    value["cwd"] = cwd
    prompt = packet(value)
    if value["provider"]["kind"] == "codex":
      outcome = codex_worker(value, prompt)
    else:
      outcome = claude_worker(value, prompt)
  except Exception as exc:
    outcome = {"status": "failed", "error": str(exc)}
  finally:
    current = st.job(job_id)
    if current.get("backend_starting") or group_running(current.get("backend_identity")):
      st.update_job(job_id, status="orphaned", error="Backend cleanup is unconfirmed; inspect before resuming")
    else:
      if current.get("thread_id"):
        st.update_job(job_id, status="archiving")
        try:
          thread_cleanup.archive(job_id, finishing=True)
        except Exception as exc:
          field = "codex_session" if current["provider"]["kind"] == "codex" else "cli_session"
          st.update_job(job_id, **{field: {"status": "pending", "error": str(exc)}})
      current = st.job(job_id)
      if current.get("backend_starting") or group_running(current.get("backend_identity")):
        st.update_job(job_id, status="orphaned", error="Cleanup backend is unconfirmed; inspect before resuming")
      else:
        st.update_job(job_id, **outcome, finished_at=time.time())
    try:
      import handoff
      handoff.sync(st.job(job_id)["session_id"])
    except Exception:
      pass  # Durable job state remains the recovery source for the next sync.


if __name__ == "__main__":
  main(sys.argv[1])

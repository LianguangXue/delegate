"""Owned, cancellable worktree preparation with durable recovery receipts."""
from pathlib import Path
from typing import Any
from contextvars import ContextVar
import os
import ast
import subprocess
import time

import storage as st
from lifecycle import identity, stop_owned, group_running

DEFAULT_TIMEOUT = 600
_CLEANUP = ContextVar("delegate_workspace_cleanup", default=False)


def run(job_id: str, cwd: str, *args: str, timeout: float | None = None,
        phase: str = "preparing-worktree") -> str:
  """Run one owned Git process with configurable timeout, heartbeats and cancellation."""
  seconds = timeout if timeout is not None else st.config().get("worktree_timeout_seconds", DEFAULT_TIMEOUT)
  if _CLEANUP.get():
    seconds = min(seconds, 60)
  if not _CLEANUP.get() and st.job(job_id).get("cancel_requested"):
    raise InterruptedError("Workspace preparation cancelled")
  env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
  command = ["git", "-C", cwd, *args]
  st.update_job(job_id, backend_starting=True, phase=phase, heartbeat=time.time())
  try:
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, text=True, start_new_session=True)
  except OSError:
    st.update_job(job_id, backend_starting=False)
    raise
  record = identity(proc.pid, include_zombies=True)
  try:
    st.update_job(job_id, backend_starting=False, backend_identity=record, workspace_git_identity=record)
    if args[:2] == ("worktree", "add"):
      receipt(job_id, checkout_identity=record)
    until = time.monotonic() + seconds
    last_heartbeat = time.monotonic()
    while True:
      if not _CLEANUP.get() and st.job(job_id).get("cancel_requested"):
        raise InterruptedError("Workspace preparation cancelled")
      remaining = until - time.monotonic()
      if remaining <= 0:
        raise subprocess.TimeoutExpired(command, seconds)
      try:
        output, error = proc.communicate(timeout=min(2, remaining))
        break
      except subprocess.TimeoutExpired:
        if time.monotonic() - last_heartbeat >= 10:
          st.update_job(job_id, heartbeat=time.time())
          last_heartbeat = time.monotonic()
    if proc.returncode:
      raise subprocess.CalledProcessError(proc.returncode, command, output, error)
    return output.strip()
  finally:
    stop_owned(proc, record)
    proc.stdout.close()
    proc.stderr.close()


def registered(job_id: str, source: str, path: Path) -> dict[str, str] | None:
  """Locate only the exact target in Git's administrative registry."""
  raw = run(job_id, source, "-c", "core.quotePath=false", "worktree", "list", "--porcelain")
  for block in raw.split("\n\n"):
    row = {}
    for field in block.splitlines():
      name, _, value = field.partition(" ")
      row[name] = value
    if row.get("worktree", "").startswith('"'):
      row["worktree"] = ast.literal_eval(row["worktree"])
    if row.get("worktree") and Path(row["worktree"]).resolve() == path.resolve():
      return row
  return None


def receipt(job_id: str, **updates: Any) -> None:
  """Preserve setup ownership/stage independently of the eventual model outcome."""
  with st.locked():
    value = st.job(job_id)
    value["workspace_setup"] = {**value.get("workspace_setup", {}), **updates, "updated_at": time.time()}
    st.write("job-" + job_id + ".json", value)


def unlock(job_id: str, source: str, path: Path) -> None:
  """Unlock only this operation's known lock, once its checkout group has stopped."""
  setup = st.job(job_id)["workspace_setup"]
  if group_running(setup.get("checkout_identity")):
    raise ValueError("Checkout group still running; preserve worktree lock")
  row = registered(job_id, source, path)
  if not row or "locked" not in row:
    receipt(job_id, lock_released=True)
    return
  if row["locked"] not in {setup["lock_reason"], "initializing"}:
    receipt(job_id, lock_released=False)
    raise ValueError("Worktree has an unexpected lock owner; preserve it for inspection")
  run(job_id, source, "worktree", "unlock", str(path))
  receipt(job_id, lock_released=True)


def verify(job_id: str, source: str, path: Path, base: str) -> bool:
  """Require a complete clean detached checkout linked to the expected source."""
  if path.is_symlink() or path.resolve() != Path(st.job(job_id)["workspace_setup"]["path"]):
    return False
  row = registered(job_id, source, path)
  if not row or row.get("HEAD") != base or "detached" not in row or not path.is_dir():
    return False
  if Path(run(job_id, str(path), "rev-parse", "--show-toplevel")).resolve() != path.resolve():
    return False
  source_common = (Path(source) / run(job_id, source, "rev-parse", "--git-common-dir")).resolve()
  target_common = (path / run(job_id, str(path), "rev-parse", "--git-common-dir")).resolve()
  gitdir = Path(run(job_id, str(path), "rev-parse", "--absolute-git-dir")).resolve()
  if target_common != source_common or not gitdir.is_relative_to(source_common / "worktrees"):
    return False
  if (gitdir / "index.lock").exists() or run(job_id, str(path), "rev-parse", "HEAD") != base:
    return False
  return not run(job_id, str(path), "status", "--porcelain", "--untracked-files=all")


def rollback(job_id: str, source: str, path: Path) -> None:
  """Remove only a clean owned checkout; preserve partial/changed artifacts explicitly."""
  setup = st.job(job_id)["workspace_setup"]
  if not setup.get("created_by_job"):
    return
  if path.is_symlink() or path.resolve() != Path(setup["path"]):
    receipt(job_id, status="retained", cleanup_error="Workspace path identity changed; preserve it for inspection")
    return
  token = _CLEANUP.set(True)
  try:
    unlock(job_id, source, path)
    row = registered(job_id, source, path)
    if row:
      if run(job_id, str(path), "ls-files", "--others", "--directory"):
        raise ValueError("Untracked or ignored workspace artifacts retained for inspection")
      # Never force removal of modified, untracked or incomplete files. The exact
      # path stays in the failed receipt instead of silently leaking a locked tree.
      run(job_id, source, "worktree", "remove", str(path))
    if path.exists():
      receipt(job_id, status="retained", cleanup_error="Unregistered files retained for inspection", lock_released=True)
    else:
      receipt(job_id, status="removed", lock_released=True)
  except Exception as exc:
    receipt(job_id, status="retained", cleanup_error=str(exc), recovery="Inspect this exact path; do not retry a write there")
  finally:
    _CLEANUP.reset(token)


def prepare(value: dict[str, Any]) -> str:
  """Create one isolated worktree and recover a completed checkout after timeout."""
  job_id = value["id"]
  source = value["source_cwd"]
  top = Path(run(job_id, source, "rev-parse", "--show-toplevel")).resolve()
  if run(job_id, str(top), "status", "--porcelain", "--untracked-files=normal"):
    raise ValueError("Write delegation requires a clean source repository. Preserve current changes first; "
                     "or use a read-only agent to analyze the current dirty tree.")
  if "160000 " in run(job_id, str(top), "ls-files", "--stage"):
    raise ValueError("Choose the actual nested repository; automatic submodule checkout is not supported")
  target = st.root() / "worktrees" / job_id
  target.parent.mkdir(mode=0o700, exist_ok=True)
  if target.is_symlink():
    raise ValueError("Workspace target is a symlink; preserve and inspect it")
  target = target.resolve()
  if target.exists() or registered(job_id, str(top), target):
    raise ValueError("Workspace target already exists or is registered; preserve and inspect it")
  base = run(job_id, str(top), "rev-parse", "HEAD")
  cwd = target / Path(source).resolve().relative_to(top)
  reason = "delegate-workspace-" + job_id
  st.update_job(job_id, worktree=str(target), base_commit=base)
  receipt(job_id, status="creating", path=str(target), source_root=str(top), base_commit=base,
          created_by_job=True, lock_reason=reason,
          timeout_seconds=st.config().get("worktree_timeout_seconds", DEFAULT_TIMEOUT))
  try:
    try:
      # Git 2.31 has no add --reason or list -z; the setup receipt owns the exact
      # fresh target, including Git's temporary "initializing" lock on failure.
      run(job_id, str(top), "worktree", "add", "--detach", str(target), base)
    except subprocess.TimeoutExpired:
      # run() has already stopped the entire owned Git group before validation.
      receipt(job_id, checkout_identity=st.job(job_id).get("workspace_git_identity"), timed_out=True, status="checking-timeout")
      if not verify(job_id, str(top), target, base):
        raise
      receipt(job_id, recovered_after_timeout=True)
    else:
      receipt(job_id, checkout_identity=st.job(job_id).get("workspace_git_identity"))
      if not verify(job_id, str(top), target, base):
        raise ValueError("Worktree checkout did not pass identity/HEAD/cleanliness validation")
    unlock(job_id, str(top), target)
    if not cwd.is_dir():
      raise ValueError("Original source subdirectory is missing in the prepared worktree")
    receipt(job_id, status="ready", lock_released=True)
    st.update_job(job_id, cwd=str(cwd), phase="workspace-ready")
    return str(cwd)
  except BaseException:
    current = st.job(job_id)
    if current.get("backend_starting") or group_running(current.get("backend_identity")):
      receipt(job_id, status="cleanup-unconfirmed", recovery="Checkout process still running; inspect before cleanup")
    else:
      rollback(job_id, str(top), target)
    raise

"""Explicit, scoped local commits for isolated delegate worktrees."""
from pathlib import Path, PurePosixPath
from typing import Any
import subprocess
import os
from contextvars import ContextVar
from functools import wraps

_CURRENT_JOB = ContextVar("delegate_commit_job", default=None)


def owned(function):
  """Make each Git subprocess visible to normal worker crash recovery."""
  @wraps(function)
  def wrapped(value):
    token = _CURRENT_JOB.set(value["id"])
    try:
      return function(value)
    finally:
      _CURRENT_JOB.reset(token)
  return wrapped


def validate(allowed: bool, message: str, paths: list[str] | None, workspace: str) -> None:
  """Validate authorization before dispatch, never broaden to source or remote writes."""
  if not isinstance(message, str) or (paths is not None and not isinstance(paths, list)):
    raise ValueError("commit_message must be text and commit_paths must be a list")
  if type(allowed) is not bool:
    raise ValueError("allow_commit must be boolean")
  if not allowed:
    if message or paths:
      raise ValueError("Commit options require explicit allow_commit=true")
    return
  if workspace != "worktree" or not message.strip() or len(message) > 1000 or not paths or len(paths) > 50:
    raise ValueError("Commit requires a write worktree, message and 1..50 explicit relative paths")
  for value in paths:
    if not isinstance(value, str) or not value.strip():
      raise ValueError("Invalid commit path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in {"..", ".git"} for p in path.parts) or '\\' in value:
      raise ValueError("Commit paths must stay within the worktree and exclude Git metadata")


def git(cwd: Path, *args: str) -> str:
  """Run Git without shell expansion; preserve author identity configured by the user."""
  env = {k:v for k,v in os.environ.items() if not k.startswith("GIT_") or k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
  from lifecycle import identity, stop_owned
  import storage as st
  job_id = _CURRENT_JOB.get()
  if job_id:
    st.update_job(job_id, backend_starting=True)
  try:
    proc = subprocess.Popen(["git", "--literal-pathspecs", "-C", str(cwd), *args],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
      text=True, env=env, start_new_session=True)
  except OSError:
    if job_id:
      st.update_job(job_id, backend_starting=False)
    raise
  record = identity(proc.pid, include_zombies=True)
  try:
    if job_id:
      st.update_job(job_id, backend_identity=record, backend_starting=False)
    output, error = proc.communicate(timeout=30)
    if proc.returncode:
      raise ValueError("Git commit helper failed: " + error[-2000:])
    return output.strip()
  finally:
    if record:
      stop_owned(proc, record)



def workspace(value: dict[str, Any]) -> Path:
  """Require a registered isolated worktree distinct from the source checkout."""
  if not value.get("worktree"):
    raise ValueError("Commit requires a registered worktree")
  cwd = Path(value["worktree"]).resolve()
  if not cwd.is_dir() or cwd == Path(value["source_cwd"]).resolve():
    raise ValueError("Refusing to commit in the source checkout")
  if Path(git(cwd, "rev-parse", "--show-toplevel")).resolve() != cwd:
    raise ValueError("Worktree identity changed")
  source = Path(value["source_cwd"]).resolve()
  source_common = (source / git(source, "rev-parse", "--git-common-dir")).resolve()
  target_common = (cwd / git(cwd, "rev-parse", "--git-common-dir")).resolve()
  source_top = Path(git(source, "rev-parse", "--show-toplevel")).resolve()
  target_gitdir = Path(git(cwd, "rev-parse", "--absolute-git-dir")).resolve()
  if (source_top == cwd or not target_gitdir.is_relative_to(source_common / "worktrees")
      or git(cwd, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"):
    raise ValueError("Commit target must be a detached registered worktree")
  if source_common != target_common:
    raise ValueError("Worktree is not linked to the source repository")
  return cwd


@owned
def prepare(value: dict[str, Any]) -> None:
  """Commit-enabled attempts start clean so existing edits cannot be captured."""
  if value.get("allow_commit"):
    cwd = workspace(value)
    if git(cwd, "status", "--porcelain", "--untracked-files=normal"):
      import source_inputs
      changed = set(git(cwd, "diff", "HEAD", "--name-only", "-z").split("\0")) | set(
        git(cwd, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
      expected = {r["path"] for r in (value.get("source_snapshot") or {}).get("files", [])}
      if (not source_inputs.verify(value, cwd) or changed - {""} - expected
          or git(cwd, "diff", "--cached", "--name-only")):
        raise ValueError("Commit-enabled task requires clean or verified source_inputs-only worktree; preserve earlier edits")
    import storage as st
    st.update_job(value["id"], commit_base_head=git(cwd, "rev-parse", "HEAD"))


@owned
def create(value: dict[str, Any]) -> dict[str, Any]:
  """Commit only allowed paths, leaving other changes intact for parent inspection."""
  validate(value.get("allow_commit", False), value.get("commit_message", ""), value.get("commit_paths"), value["role"]["workspace"])
  if not value.get("allow_commit"):
    return {"status": "disabled"}
  import storage as st
  if st.job(value["id"]).get("cancel_requested"):
    raise InterruptedError("Commit cancelled")
  cwd = workspace(value)
  for name in value["commit_paths"]:
    target = (cwd / name).resolve()
    if not target.is_relative_to(cwd):
      raise ValueError("Commit path escapes through a symlink")
  if git(cwd, "diff", "--cached", "--name-only"):
    raise ValueError("Unexpected staged changes; leave index untouched for inspection")
  before = git(cwd, "rev-parse", "HEAD")
  if before != value.get("commit_base_head"):
    raise ValueError("Worktree HEAD changed during the task; inspect before committing")
  git(cwd, "add", "--", *value["commit_paths"])
  if not git(cwd, "diff", "--cached", "--name-only"):
    return {"status": "no-changes", "head": before}
  if st.job(value["id"]).get("cancel_requested"):
    raise InterruptedError("Commit cancelled; staged work is preserved for inspection")
  git(cwd, "commit", "--only", "-m", value["commit_message"], "--", *value["commit_paths"])
  after = git(cwd, "rev-parse", "HEAD")
  return {"status": "committed", "commit": after, "parent": before,
          "paths": value["commit_paths"], "files": [p for p in git(cwd, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", after).split("\0") if p], "worktree": str(cwd), "pushed": False}

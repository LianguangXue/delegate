"""Small atomic JSON store; no database or recursive filesystem scans."""
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import copy
import fcntl
import json
import os
import re
import tempfile

DEFAULT_CONFIG = {
  "version": 1,
  "max_concurrency": 2,
  "max_session_jobs": 20,
  "max_timeout_seconds": 1800,
  "worktree_timeout_seconds": 600,
  "quota_reserve_percent": 5,
  "routing": {"claude_low_remaining_percent": 20, "claude_critical_remaining_percent": 5,
              "prefer_external_when_unknown": True},
  "providers": {
    "codex": {"kind": "codex", "enabled": True, "command": "codex"},
    "claude": {"kind": "claude", "enabled": True, "command": "claude",
               "models": ["haiku", "sonnet", "opus"], "efforts": ["low", "medium", "high"]},
    "deepseek": {"kind": "anthropic", "enabled": False, "command": "claude",
                 "base_url": "https://api.deepseek.com/anthropic", "api_key_env": "DEEPSEEK_API_KEY",
                 "models": [], "efforts": ["low", "medium", "high"]},
  },
  "agents": {
    "claude-explorer": {"provider": "claude", "enabled": True, "automatic": True,
                        "workspace": "read-only", "instructions": "Explore and explain the relevant code."},
    "codex-builder": {"provider": "codex", "enabled": True, "automatic": True,
                      "workspace": "worktree", "instructions": "Implement the smallest complete change; run focused tests."},
    "codex-reviewer": {"provider": "codex", "enabled": True, "automatic": True,
                       "workspace": "read-only", "instructions": "Review independently. Report actionable defects with evidence."},
    "deepseek-docs": {"provider": "deepseek", "enabled": False, "automatic": False,
                      "workspace": "read-only", "instructions": "Analyze documentation and propose precise improvements."},
  },
}


def root() -> Path:
  """Return the private state directory, overridable for isolated tests."""
  path = Path(os.environ.get("DELEGATE_STATE_DIR", str(Path.home() / ".claude" / "delegate")))
  path.mkdir(mode=0o700, parents=True, exist_ok=True)
  return path.resolve()


def key(value: str) -> str:
  """Validate an identifier before using it as a filename."""
  if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
    raise ValueError("Invalid identifier")
  return value


def read(name: str, default: Any = None) -> Any:
  """Read one known JSON file without enumerating directories."""
  try:
    return json.loads((root() / name).read_text())
  except FileNotFoundError:
    return copy.deepcopy(default)


def write(name: str, value: Any) -> None:
  """Replace a JSON file atomically using a same-directory temporary file."""
  directory = root()
  fd, temp = tempfile.mkstemp(prefix=".write-", dir=directory)
  try:
    with os.fdopen(fd, "w") as stream:
      json.dump(value, stream, ensure_ascii=False)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temp, directory / name)
  finally:
    if os.path.exists(temp):
      os.unlink(temp)


@contextmanager
def locked() -> Iterator[None]:
  """Serialize short read-modify-write operations across plugin processes."""
  with (root() / "state.lock").open("a") as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    yield


def lock_path(name: str) -> Path:
  """Return a dedicated lock-file path inside ``locks/`` for per-entity coordination.

  Args:
    name: Safe single-segment lock identifier.

  Returns:
    Path to the lock file inside ``root() / "locks"``.

  Raises:
    ValueError: If ``name`` is not a safe identifier.
  """
  locks = root() / "locks"
  locks.mkdir(mode=0o700, exist_ok=True)
  return locks / (key(name) + ".lock")


def config() -> dict[str, Any]:
  """Return installed configuration or the conservative built-in defaults."""
  return read("config.json", DEFAULT_CONFIG)


def session(session_id: str) -> dict[str, Any]:
  """Return explicit session state or the configured default for new sessions."""
  return read("session-" + key(session_id) + ".json",
              {"mode": config().get("default_mode", "off"), "agents": None, "strategy": "balanced", "jobs": []})


def save_session(session_id: str, value: dict[str, Any]) -> None:
  """Persist explicit session settings and job references."""
  write("session-" + key(session_id) + ".json", value)


def job(job_id: str) -> dict[str, Any]:
  """Read a known job, rejecting missing identifiers."""
  value = read("job-" + key(job_id) + ".json")
  if value is None:
    raise ValueError("Unknown job")
  return value


def update_job(job_id: str, **updates: Any) -> dict[str, Any]:
  """Apply a small atomic job update without losing concurrent controls."""
  with locked():
    value = job(job_id)
    value.update(updates)
    write("job-" + key(job_id) + ".json", value)
    return value

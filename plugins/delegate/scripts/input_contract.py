"""Bounded file-access contracts checked before inference, never permission expansion."""
from pathlib import Path
from typing import Any
import json
import os
import re
import stat
import sys


def normalize(paths: list[str] | None, field: str) -> list[str]:
  """Accept exact file paths, without directory traversal, globbing or recursive scans."""
  if paths is None:
    return []
  if not isinstance(paths, list) or len(paths) > 32:
    raise ValueError(field + " must be a list of at most32 exact file paths")
  if any(not isinstance(p, str) or not p or len(p) > 4096 or any(c in p for c in ("\0", "\n", "\r")) for p in paths):
    raise ValueError(field + " contains an invalid file path")
  if sum(len(p) for p in paths) > 8192:
    raise ValueError(field + " exceeds8192 total path characters")
  return list(dict.fromkeys(paths))


def discover(text: str) -> list[str]:
  """Notice existing absolute file references in legacy task packets; never infer write permission."""
  matches = list(dict.fromkeys(m.group(0).rstrip(".,;:!?，。；：") for m in re.finditer(
    r'''(?<![\w:/])/(?!/)[^\s`"'<>\[\](){}|]+''', text)))
  if len(matches) > 32:
    raise ValueError("Too many absolute path references; provide a focused task packet and required_files")
  result = []
  for name in matches:
    try:
      if Path(name).is_file():
        result.append(name)
    except OSError:
      pass
  return result


def check(provider: dict[str, Any], workspace: str, cwd: str, required: list[str], outputs: list[str],
          probe: bool = False) -> dict[str, Any]:
  """Reject unreachable inputs and incompatible output contracts without modifying files."""
  root = Path(cwd).resolve()
  file_tools = provider["kind"] != "codex"
  if outputs and workspace != "worktree":
    raise ValueError("Requested output_files require a writable worktree role; a read-only reviewer must return its report as text")
  resolved = []
  for name in required:
    if file_tools and workspace == "worktree" and Path(name).is_absolute():
      raise ValueError("File-tool builders need cwd-relative required_files/task references for their future worktree. "
        "Use relative paths with source_inputs for selected drafts; absolute source paths remain outside the new restricted cwd")
    path = (root / name).resolve()
    if file_tools and not path.is_relative_to(root):
      raise ValueError("Required input is outside this file-tool role's cwd: " + str(path) +
        ". Use a containing cwd, a relative source_inputs snapshot, or a shell-capable Codex role; no model was started")
    try:
      info = path.stat()
      if not stat.S_ISREG(info.st_mode):
        raise ValueError("required_files must identify regular files: " + str(path))
      if probe:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
          if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Required input changed file type")
          stream.read(1)
    except OSError as exc:
      raise ValueError("Required input is unavailable: " + str(path) + " (" + type(exc).__name__ +
        "). In a builder, include selected drafts using source_inputs or return analysis with a read-only role") from None
    resolved.append(str(path))
  for name in outputs:
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root:
      raise ValueError("output_files must stay inside the actual worker cwd; use relative artifact paths or return report text")
    if path.exists() and not path.is_file():
      raise ValueError("output_files must name regular files or new file paths, not directories/special files")
    for parent in path.parents:
      if parent.exists():
        if not parent.is_dir():
          raise ValueError("output_files parent is not a directory")
        break
  return {"status": "passed", "required_files": resolved, "output_files": outputs,
    "write_access_verified": False,
    "scope": "Declared inputs/legacy absolute references only. Outputs checked for confinement/type, not actual writes; no permission expansion"}


PROBE = '''import json,os,stat,sys
rows=[]
for path in json.loads(sys.argv[1]):
 row={'path':path,'ok':False}
 try:
  fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
  with os.fdopen(fd,'rb') as stream:
   if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('not a regular file')
   stream.read(1)
  row['ok']=True
 except (OSError,ValueError) as exc:row['error']=type(exc).__name__
 rows.append(row)
print(json.dumps({'files':rows}))
'''


def preflight(rpc: Any, job: dict[str, Any], sandbox_policy: dict[str, Any]) -> dict[str, Any]:
  """Check exact required files under the same Codex execution policy before any model turn."""
  report = check(job["provider"], job["role"]["workspace"], job["cwd"], job.get("required_files", []),
                 job.get("output_files", []))
  if not report["required_files"]:
    return report
  response = rpc.call("command/exec", {"command": [sys.executable, "-I", "-S", "-B", "-c", PROBE,
    json.dumps(report["required_files"])], "cwd": job["cwd"], "sandboxPolicy": sandbox_policy, "timeoutMs": 10000}, timeout=15)
  raw = response.get("stdout", "")
  if response.get("exitCode") != 0 or not isinstance(raw, str) or len(raw) > 65536:
    raise ValueError("Required-file sandbox preflight could not complete; no model was started")
  try:
    rows = json.loads(raw)["files"]
    valid = (isinstance(rows, list) and len(rows) == len(report["required_files"]) and
      all(isinstance(row, dict) and row.get("path") == path and row.get("ok") is True
          for row, path in zip(rows, report["required_files"])))
  except (ValueError, KeyError, TypeError):
    valid = False
  if not valid:
    raise ValueError("Required files are not accessible under the selected sandbox; no model was started")
  return {**report, "files": rows, "execution_policy_verified": True}

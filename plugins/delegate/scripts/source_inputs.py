"""Explicit, bounded snapshots of selected draft files for isolated builder worktrees."""
from pathlib import Path, PurePosixPath
from typing import Any
import hashlib
import os
import stat
import uuid

import storage as st

MAX_FILE = 1024 * 1024
MAX_TOTAL = 8 * MAX_FILE


def validate(paths: list[str] | None) -> list[str]:
  """Require exact relative file paths; never enumerate directories or ignored artifacts."""
  if paths is None:
    return []
  if not isinstance(paths, list) or not 1 <= len(paths) <= 64:
    raise ValueError("source_inputs must contain 1..64 exact repository-root-relative file paths")
  result = []
  for value in paths:
    if not isinstance(value, str) or not value or len(value) > 1024 or any(c in value for c in ("\\", "\0", "\n", "\r")):
      raise ValueError("Invalid source_inputs file path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in {"..", ".git"} for p in path.parts):
      raise ValueError("source_inputs paths must stay inside the repository and exclude .git")
    if any(p.startswith(".env") or p in {".ssh", ".aws", ".kube", ".netrc", ".npmrc", ".pypirc",
           ".git-credentials", "auth.json", "credentials.json", "credentials", ".credentials.json", "id_rsa", "id_ed25519"}
           or p.endswith((".pem", ".key")) for p in path.parts):
      raise ValueError("source_inputs cannot include credential files")
    result.append(str(path))
  return list(dict.fromkeys(result))


def parent(root: Path, name: str, create: bool = False) -> tuple[int, str]:
  """Pin every directory component without following symlinks; return an owned descriptor."""
  fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
  parts = PurePosixPath(name).parts
  try:
    for part in parts[:-1]:
      if create:
        try:
          os.mkdir(part, 0o700, dir_fd=fd)
        except FileExistsError:
          pass
      next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
      os.close(fd)
      fd = next_fd
    return fd, parts[-1]
  except BaseException:
    os.close(fd)
    raise


def read(root: Path, name: str) -> tuple[bytes, int] | None:
  """Read one stable regular file, with no-follow/nonblocking opens and bounded bytes."""
  try:
    directory, leaf = parent(root, name)
  except FileNotFoundError:
    return None
  try:
    try:
      fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
      return None
    with os.fdopen(fd, "rb") as stream:
      before = os.fstat(stream.fileno())
      if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
        raise ValueError("source_inputs must be regular files of at most 1 MiB each")
      data = stream.read(MAX_FILE + 1)
      after = os.fstat(stream.fileno())
      if len(data) > MAX_FILE or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
          after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("source_inputs changed during capture; retry after the edit finishes")
      return data, 0o755 if before.st_mode & 0o111 else 0o644
  finally:
    os.close(directory)


def capture(value: dict[str, Any], source: Path, base: str, tracked: set[str]) -> dict[str, Any]:
  """Freeze selected current contents without staging, committing or modifying the source."""
  paths = validate(value.get("source_inputs"))
  captured, total = [], 0
  for name in paths:
    try:
      item = read(source, name)
    except OSError:
      raise ValueError("source_inputs refuses symlinks or inaccessible file components: " + name) from None
    if item is None and name not in tracked:
      raise ValueError("source_inputs path is missing and is not a tracked deletion: " + name)
    total += len(item[0]) if item else 0
    if total > MAX_TOTAL:
      raise ValueError("source_inputs exceeds the 8 MiB total snapshot limit")
    captured.append((name, item))
  for name, item in captured:
    if read(source, name) != item:
      raise ValueError("source_inputs changed during capture; retry after the edit finishes")
  directory = st.root() / "input-snapshots" / st.key(value["id"])
  directory.mkdir(mode=0o700, parents=True, exist_ok=False)
  files = []
  for index, (name, item) in enumerate(captured):
    row = {"path": name, "deleted": item is None}
    if item is not None:
      data, mode = item
      blob = str(index)
      (directory / blob).write_bytes(data)
      (directory / blob).chmod(0o600)
      row.update(blob=blob, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), mode=mode)
    files.append(row)
  report = {"status": "captured", "base_commit": base, "directory": str(directory), "files": files,
    "bytes": total, "scope": "Selected file contents at worker launch; other source edits are not copied"}
  st.update_job(value["id"], source_snapshot=report)
  return report


def apply(value: dict[str, Any], target: Path, report: dict[str, Any]) -> None:
  """Overlay only selected blobs/deletions, preserving the worktree's original index and HEAD."""
  st.update_job(value["id"], source_snapshot={**report, "status": "applying"})
  for row in report["files"]:
    directory, leaf = parent(target, row["path"], create=not row["deleted"])
    try:
      try:
        existing = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
      except FileNotFoundError:
        existing = None
      if existing and not stat.S_ISREG(existing.st_mode):
        raise ValueError("source_inputs target is not a regular file: " + row["path"])
      if row["deleted"]:
        if existing:
          os.unlink(leaf, dir_fd=directory)
        continue
      item = read(Path(report["directory"]), row["blob"])
      if item is None or hashlib.sha256(item[0]).hexdigest() != row["sha256"]:
        raise ValueError("source_inputs snapshot identity changed; preserve workspace for inspection")
      temp = ".delegate-input-" + uuid.uuid4().hex
      try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
          stream.write(item[0])
          os.fchmod(stream.fileno(), row["mode"])
        os.replace(temp, leaf, src_dir_fd=directory, dst_dir_fd=directory)
      finally:
        try:
          os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
          pass
    finally:
      os.close(directory)
  st.update_job(value["id"], source_snapshot={**report, "status": "applied"})


def verify(value: dict[str, Any], target: Path) -> bool:
  """Verify inherited draft contents before allowing an explicitly scoped local commit."""
  report = value.get("source_snapshot") or {}
  if report.get("status") != "applied":
    return False
  for row in report["files"]:
    item = read(target, row["path"])
    if row["deleted"]:
      if item is not None:
        return False
    elif item is None or hashlib.sha256(item[0]).hexdigest() != row["sha256"] or item[1] != row["mode"]:
      return False
  return True

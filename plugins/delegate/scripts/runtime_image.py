"""Content-addressed Python runtimes shared by a server and its workers."""
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import shutil
import sys
import tempfile
import re
import stat

import storage as st

VERSION = "0.23.0"
_VERSIONS: dict[str, str] = {}


def job_version(job: dict[str, Any]) -> str:
  """Read the version of a job's immutable code image, never infer the active server from an old job."""
  version = job.get("delegate_version")
  if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
    return version
  image = job.get("runtime_image") or {}
  digest = image.get("digest", "")
  if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
    return "unknown"
  if digest in _VERSIONS:
    return _VERSIONS[digest]
  try:
    fd = os.open(Path(image["path"]) / "delegate.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
      info = os.fstat(stream.fileno())
      if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 131072:
        return "unknown"
      text = stream.read(131073).decode(errors="replace")
    match = re.search(r'"serverInfo":\s*\{[^}]*"version":\s*"(\d+\.\d+\.\d+)"', text)
    version = match[1] if match else "unknown"
  except (OSError, ValueError, KeyError, TypeError):
    version = "unknown"
  _VERSIONS[digest] = version
  return version


def digest(files: dict[str, bytes]) -> str:
  """Hash names and contents without relying on timestamps or release labels."""
  h = hashlib.sha256()
  for name, data in sorted(files.items()):
    h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
  return h.hexdigest()


def verify(directory: Path, expected: str) -> None:
  """Reject incomplete, changed or mismatched runtime images before execution."""
  manifest = json.loads((directory / "runtime-image.json").read_text())
  files = {name: (directory / name).read_bytes() for name in manifest["files"]
           if Path(name).name == name and name.endswith(".py")}
  if len(files) != len(manifest["files"]) or digest(files) != expected or manifest["digest"] != expected:
    raise ValueError("Server/worker runtime mismatch; restart the parent on the installed release")


def freeze(source: Path) -> dict[str, Any]:
  """Capture stable, compilable sources, then publish a read-only image atomically."""
  source = source.resolve()
  if (source / "runtime-image.json").exists():
    value = json.loads((source / "runtime-image.json").read_text())
    verify(source, value["digest"])
    return {"path": str(source), "digest": value["digest"]}
  files = {p.name: p.read_bytes() for p in sorted(source.glob("*.py"))}
  for name, data in files.items():
    compile(data, str(source / name), "exec")
  again = {p.name: p.read_bytes() for p in sorted(source.glob("*.py"))}
  if files != again:
    raise ValueError("Delegate sources changed during startup; retry after the edit finishes")
  fingerprint = digest(files)
  root = st.root() / "runtimes"
  root.mkdir(mode=0o700, exist_ok=True)
  target = root / fingerprint
  if not target.exists():
    temp = Path(tempfile.mkdtemp(prefix=".image-", dir=root))
    try:
      for name, data in files.items():
        (temp / name).write_bytes(data)
      (temp / "runtime-image.json").write_text(json.dumps({"digest": fingerprint, "files": sorted(files)}))
      try:
        os.rename(temp, target)
      except OSError:
        if not target.exists():
          raise
    finally:
      if temp.exists():
        shutil.rmtree(temp)
    for path in target.iterdir():
      path.chmod(0o400)
    target.chmod(0o500)
  verify(target, fingerprint)
  return {"path": str(target), "digest": fingerprint}


def bootstrap() -> None:
  """Re-exec entrypoints from a frozen image before importing the service graph."""
  source = Path(__file__).resolve().parent
  image = freeze(source)
  if Path(image["path"]) != source:
    os.execv(sys.executable, [sys.executable, str(Path(image["path"]) / "delegate.py"), *sys.argv[1:]])


def require_worker(job: dict[str, Any], directory: Path) -> None:
  """Require positive server provenance, including when a legacy server launches new code."""
  image = job.get("runtime_image")
  if not isinstance(image, dict) or not image.get("path") or not image.get("digest"):
    raise ValueError("Legacy parent has no frozen runtime identity; no model was started. "
      "Do not repeat dispatch through this MCP server. /reload-plugins may defer MCP changes to the next session. "
      "Use the installed delegate-agent call CLI with the SAME session_id, or start a new session on the installed release")
  if Path(image["path"]).resolve() != directory.resolve():
    raise ValueError("Server/worker runtime mismatch; restart the parent on the installed release")
  verify(directory, image["digest"])

"""Content-addressed Python runtimes shared by a server and its workers."""
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import shutil
import sys
import tempfile

import storage as st


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

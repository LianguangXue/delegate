"""Bounded static delivery checks; never import project code, copy helpers or run tests."""
from pathlib import Path
from typing import Any
import ast
import os
import re
import shlex
import sys
import time

import workspaces

SKIP = {".git", "node_modules", "__pycache__", "dist", "build", ".venv", "venv", "data", "checkpoints", "output", "outputs"}


def is_test(path: Path) -> bool:
  """Recognize Python test/conftest paths without scanning their directories."""
  return path.suffix == ".py" and (path.name.startswith("test_") or path.name.endswith("_test.py")
    or path.name == "conftest.py" or "tests" in path.parts or "test" in path.parts)


def test_targets(command: str, cwd: str) -> list[str]:
  """Extract literal Python test targets from validation commands without shell execution."""
  if len(command) > 16384 or not any(name in command for name in ("pytest", "unittest", "py.test")):
    return []
  try:
    tokens = shlex.split(command)
    if any(Path(t).name in {"bash", "sh", "zsh"} for t in tokens[:2]) and ("-c" in tokens or "-lc" in tokens):
      tokens = shlex.split(tokens[-1])
  except ValueError:
    return []
  if not any(Path(t).name in {"pytest", "py.test", "unittest"} for t in tokens):
    return []
  result = []
  for index, token in enumerate(tokens[:256]):
    if index and tokens[index-1] in {"--ignore", "--ignore-glob", "--deselect"}:
      continue
    value = token.split("::", 1)[0]
    if value.endswith(".py") and not value.startswith("-") and not any(c in value for c in "$*?\n"):
      result.append(str(Path(cwd) / value))
  return result[:32]


def _name(node: ast.AST, values: dict[str, Any]) -> str:
  """Resolve imported aliases for the explicitly supported standard-library loaders."""
  name = ast.unparse(node)
  head, dot, tail = name.partition(".")
  return values.get("__alias__" + head, head) + (dot + tail if dot else "")


def _value(node: ast.AST, values: dict[str, Any], cwd: Path) -> Any:
  """Evaluate a small set of literal path expressions, not arbitrary Python."""
  if isinstance(node, ast.Constant) and type(node.value) in {str, int}:
    return node.value
  if isinstance(node, ast.Name):
    return values.get(node.id)
  if isinstance(node, ast.Attribute):
    base = _value(node.value, values, cwd)
    if isinstance(base, Path) and node.attr in {"parent", "parents"}:
      return getattr(base, node.attr)
  if isinstance(node, ast.Subscript):
    base, index = _value(node.value, values, cwd), _value(node.slice, values, cwd)
    if type(index) is int and 0 <= index <= 32:
      try:
        return base[index]
      except (TypeError, IndexError):
        pass
  if isinstance(node, ast.BinOp):
    left, right = _value(node.left, values, cwd), _value(node.right, values, cwd)
    if isinstance(node.op, ast.Div) and isinstance(left, Path) and isinstance(right, (str, Path)):
      return left / right
    if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
      return left + right
  if isinstance(node, ast.Call):
    name = _name(node.func, values)
    args = [_value(arg, values, cwd) for arg in node.args]
    if name in {"Path", "pathlib.Path"} and args and all(isinstance(a, (str, Path)) for a in args):
      return Path(*args)
    if name in {"str", "os.fspath"} and len(args) == 1 and isinstance(args[0], (str, Path)):
      return str(args[0])
    if name == "os.path.join" and args and all(isinstance(a, (str, Path)) for a in args):
      return str(Path(*args))
    if name == "os.path.dirname" and len(args) == 1 and isinstance(args[0], (str, Path)):
      return str(Path(args[0]).parent)
    if isinstance(node.func, ast.Attribute):
      base = _value(node.func.value, values, cwd)
      if isinstance(base, Path) and node.func.attr in {"resolve", "absolute"} and not args:
        return Path(os.path.abspath(cwd / base))
      if isinstance(base, Path) and node.func.attr == "with_name" and len(args) == 1 and isinstance(args[0], str):
        return base.with_name(args[0])
  return None


def check(job: dict[str, Any]) -> dict[str, Any]:
  """Report local test dependencies that a clean checkout cannot obtain from Git."""
  result: dict[str, Any] = {"status": "not_applicable", "advisory": True, "inference_calls": 0,
    "tests_checked": 0, "files_checked": 0, "findings": [], "limitations": [],
    "scope": "Static Python imports/literal loader paths; up to 64 tests, 128 files, 1024 path probes, 30s soft budget. No test execution."}
  if not job.get("worktree") or job.get("role", {}).get("workspace") != "worktree":
    return result
  until = time.monotonic() + 30
  probes = 0
  def budget() -> None:
    if time.monotonic() >= until:
      raise TimeoutError("Static delivery check time budget reached")
  def probe(path: Path, directory: bool = False) -> bool:
    nonlocal probes
    budget()
    if probes >= 1024:
      raise TimeoutError("Static delivery metadata probe limit reached")
    probes += 1
    return path.is_dir() if directory else path.is_file()
  def git(cwd: Path, *args: str) -> str:
    remaining = until - time.monotonic()
    if remaining <= 0:
      raise TimeoutError("Delivery check time budget reached")
    return workspaces.run(job["id"], str(cwd), *args, timeout=min(10, remaining), phase="checking-delivery")
  try:
    cwd = Path(job["cwd"]).resolve()
    top = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
    names = git(top, "ls-files", "-z").split("\0")
    tracked = {name for name in names if name}
    staged = set(git(top, "diff", "--cached", "--name-only", "--diff-filter=A", "-z").split("\0"))
    committed = tracked - staged
    base = job.get("base_commit") or job.get("commit_base_head") or "HEAD"
    if base != "HEAD" and not re.fullmatch(r"[0-9a-fA-F]{40,64}", base):
      raise ValueError("Invalid delivery base commit")
    scope = cwd.relative_to(top)
    changed = git(top, "diff", "--name-only", "--diff-filter=ACMR", "-z", base, "--", str(scope)).split("\0")
    candidates = [Path(p) for p in job.get("validation_test_paths", [])[:64]]
    candidates += [top / p for p in changed if p and is_test(Path(p))]
    indexed = [top / p for p in names if p and is_test(Path(p)) and (top / p).is_relative_to(cwd)
               and not SKIP.intersection(Path(p).parts)]
    # Only inspect a bounded single directory level for new tests. No recursive
    # untracked walk through output/data/checkpoints or the user's home.
    directories = dict.fromkeys([cwd / "tests", cwd / "test", *[p.parent for p in indexed]])
    for directory in list(directories)[:8]:
      budget()
      if not directory.resolve().is_relative_to(top):
        result["limitations"].append("External/symlink test directory not enumerated")
        continue
      try:
        with os.scandir(directory) as entries:
          for index, entry in enumerate(entries):
            budget()
            if index >= 128:
              result["limitations"].append("Untracked test directory entry limit reached")
              break
            path = Path(entry.path)
            if is_test(path):
              candidates.append(path)
      except FileNotFoundError:
        pass
    candidates += indexed
    tests = list(dict.fromkeys(candidates))
    if len(tests) > 64:
      result["limitations"].append("Test sample limited to 64 files")
    queue = [(p, p) for p in tests[:64]]
    seen = set()
    def finding(test: Path, dependency: Path, line: int, reason: str, source: Path | None = None) -> None:
      source = source or test
      entry = {"test": str(test.relative_to(top)) if test.is_relative_to(top) else str(test),
        "referenced_by": str(source.relative_to(top)) if source.is_relative_to(top) else str(source),
        "dependency": str(dependency.relative_to(top)) if dependency.is_relative_to(top) else str(dependency),
        "line": line, "reason": reason, "action": "Include required helper in the reviewed delivery/approved commit, or remove this dependency; do not silently copy ignored output."}
      if entry not in result["findings"]:
        if len(result["findings"]) < 20:
          result["findings"].append(entry)
        else:
          result["findings_truncated"] = True
    def dependency(test: Path, path: Path, line: int, source: Path | None = None) -> None:
      path = Path(os.path.abspath(cwd / path))
      if not path.is_relative_to(top) or not path.resolve().is_relative_to(top):
        finding(test, path, line, "outside_worktree", source)
        return
      if not probe(path):
        finding(test, path, line, "missing_local_file", source)
        return
      if str(path.relative_to(top)) not in committed:
        finding(test, path, line, "not_committed_in_git", source)
      queue.append((path, test))
    while queue and len(seen) < 128:
      if time.monotonic() >= until:
        result["limitations"].append("Static check time budget reached")
        break
      path, test = queue.pop(0)
      path = Path(os.path.abspath(cwd / path))
      if path in seen:
        continue
      seen.add(path)
      if not path.is_relative_to(top) or not path.resolve().is_relative_to(top):
        finding(test, path, 0, "outside_worktree")
        continue
      try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
          import stat
          if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Test dependency is not a regular file")
          raw = stream.read(65537)
        if len(raw) > 65536:
          raise ValueError("Python file exceeds static check size limit")
        tree = ast.parse(raw, filename=str(path))
      except (OSError, ValueError, SyntaxError, RecursionError) as exc:
        result["limitations"].append(str(path.relative_to(top)) + ": " + type(exc).__name__)
        continue
      result["files_checked"] += 1
      if path == test:
        result["tests_checked"] += 1
      values: dict[str, Any] = {"__file__": str(path)}
      search = [path.parent, cwd, top]
      for node in ast.walk(tree):
        budget()
        if isinstance(node, ast.Import):
          for alias in node.names:
            values["__alias__" + (alias.asname or alias.name.split(".")[0])] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
          for alias in node.names:
            values["__alias__" + (alias.asname or alias.name)] = node.module + "." + alias.name
        if isinstance(node, ast.Assign):
          value = _value(node.value, values, cwd)
          for target in node.targets:
            if isinstance(target, ast.Name) and value is not None:
              values[target.id] = value
        if isinstance(node, ast.Call):
          name = _name(node.func, values)
          if name in {"sys.path.insert", "sys.path.append"} and node.args:
            value = _value(node.args[-1], values, cwd)
            if isinstance(value, (str, Path)):
              search.insert(0, Path(os.path.abspath(cwd / value)))
          loaders = {"importlib.util.spec_from_file_location": 1, "spec_from_file_location": 1,
                     "runpy.run_path": 0, "SourceFileLoader": 1, "importlib.machinery.SourceFileLoader": 1}
          if name in loaders:
            index = loaders[name]
            value = _value(node.args[index], values, cwd) if len(node.args) > index else None
            if isinstance(value, (str, Path)):
              dependency(test, Path(value), node.lineno, path)
            else:
              result["limitations"].append(str(path.relative_to(top)) + f":{node.lineno} unresolved dynamic loader")
        imports = []
        if isinstance(node, ast.Import):
          imports = [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
          imports = [(node.module, node.level)] if node.module else [(a.name, node.level) for a in node.names]
        for module, level in imports:
          if not level and module.split(".")[0] in sys.stdlib_module_names:
            continue
          bases = [path.parents[level-1]] if level and level <= len(path.parents) else search if not level else []
          parts = Path(*module.split("."))
          found = False
          for directory in dict.fromkeys(bases):
            for candidate in (directory / parts.with_suffix(".py"), directory / parts / "__init__.py"):
              if probe(candidate):
                dependency(test, candidate, node.lineno, path)
                found = True
                break
            if not found and probe(directory / parts, directory=True):
              found = True  # A namespace package need not have __init__.py.
            if found:
              break
          if not found and (level or module.split(".")[0] in {"output", "outputs", "generated"}):
            dependency(test, (bases[0] if bases else cwd) / parts.with_suffix(".py"), node.lineno, path)
          if isinstance(node, ast.ImportFrom) and node.module and (found or module.split(".")[0] in {"output", "outputs", "generated"}):
            for alias in node.names:
              if alias.name == "*":
                continue
              for directory in dict.fromkeys(bases):
                candidate = directory / parts / (alias.name + ".py")
                if probe(candidate):
                  dependency(test, candidate, node.lineno, path)
                  break
    if queue:
      result["limitations"].append("Python dependency graph limited to 128 files")
    result["status"] = "warning" if result["findings"] else "incomplete" if result["limitations"] else "passed" if result["tests_checked"] else "not_applicable"
    result["checked_commit"] = git(top, "rev-parse", "HEAD")
  except Exception as exc:
    result["status"] = "warning" if result["findings"] else "incomplete"
    result["limitations"].append(type(exc).__name__ + ": static delivery check did not complete")
  result["limitations"] = result["limitations"][:12]
  result["metadata_probes"] = probes
  return result

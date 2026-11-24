#!/usr/bin/env python3
"""Install local host adapters without credentials, account changes or source rewrites."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"))
import runtime_image

_VALID_PROFILE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def backup(path: Path) -> Path | None:
  """Create a 0o600 timestamped backup and keep at most three recent backups per file.

  Args:
    path: Existing target file to back up before overwriting.

  Returns:
    Path to the created backup file, or None if ``path`` does not exist.
  """
  if path.exists():
    target = path.with_name(path.name + ".delegate-backup-" + str(time.time_ns()))
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    backups = sorted(
      (p for p in path.parent.glob(path.name + ".delegate-backup-*") if p.is_file() and not p.is_symlink()),
      key=lambda p: p.stat().st_mtime_ns,
      reverse=True,
    )
    for stale in backups[3:]:
      stale.unlink(missing_ok=True)
    return target
  return None


def link(source: Path, target: Path) -> None:
  """Symlink ``target`` to ``source`` idempotently without overwriting foreign files.

  Args:
    source: Repository skill directory.
    target: Host skill symlink destination.

  Raises:
    ValueError: If ``target`` already exists and points elsewhere.
  """
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.is_symlink() and target.resolve() == source.resolve():
    return
  if target.exists() or target.is_symlink():
    raise ValueError("Skill/launcher target already exists: " + str(target))
  target.symlink_to(source)


def profile_prefix(text: str) -> str:
  """Normalize a supported empty patch list before appending a block-style overlay.

  Args:
    text: Existing YAML patch file contents.

  Returns:
    Normalized patch prefix text ready for overlay insertion.

  Raises:
    ValueError: If the patch file is not a block-style YAML sequence or empty list.
  """
  meaningful = [line.split("#", 1)[0].strip() for line in text.splitlines()]
  meaningful = [line for line in meaningful if line and line != "---"]
  if not meaningful:
    return text
  if meaningful == ["[]"]:
    return "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#")) + "\n"
  if not meaningful[0].startswith("- ") or meaningful[-1] == "...":
    raise ValueError("Use a block-style DSH patch list (or []) before installation")
  return text


def parse_dsh_profiles(raw: str) -> tuple[list[str], bool]:
  """Parse and validate single or comma-separated ``--dsh-profile`` names.

  Args:
    raw: Profile specification string (e.g. ``web`` or ``web,tui``).

  Returns:
    Tuple of ``(deduplicated_profile_names, is_multi_profile)``.

  Raises:
    ValueError: If ``raw`` is empty or contains invalid profile names.
  """
  if not isinstance(raw, str) or not raw.strip():
    raise ValueError("Invalid profile name")
  parts = [segment.strip() for segment in raw.split(",")]
  if any(not segment or any(ch not in _VALID_PROFILE_CHARS for ch in segment) for segment in parts):
    raise ValueError("Invalid profile name")
  multi = len(parts) > 1
  return list(dict.fromkeys(parts)), multi


def select_dsh_patches(dsh_root: Path, raw_profiles: str) -> list[Path]:
  """Select DSH ``cordis.patch.yml`` files to update, skipping missing/installed in multi-profile mode.

  Args:
    dsh_root: Root DSH directory (``~/.dsh`` or ``$DSH_HOME``).
    raw_profiles: Single or comma-separated profile specification.

  Returns:
    List of validated ``cordis.patch.yml`` paths eligible for overlay insertion.

  Raises:
    ValueError: If profile names are invalid, YAML format is unsupported, or single-profile checks fail.
  """
  profiles, multi = parse_dsh_profiles(raw_profiles)
  selected: list[Path] = []
  for name in profiles:
    patch = dsh_root / "profiles" / name / "cordis.patch.yml"
    if not patch.is_file():
      if multi:
        continue
      raise ValueError("Initialize the selected DSH profile before installation")
    text = patch.read_text(encoding="utf-8")
    if "delegate-host-mcp" in text:
      if multi:
        continue
      raise ValueError("DSH delegate overlay already exists")
    profile_prefix(text)
    selected.append(patch)
  return selected


def _load_mcp_config(path: Path) -> dict[str, object]:
  """Read an Antigravity ``mcp_config.json`` file safely, treating empty files as ``{}``.

  Args:
    path: Target ``mcp_config.json`` path.

  Returns:
    Parsed JSON dictionary with ``mcpServers`` validated as a dictionary if present.

  Raises:
    ValueError: If existing non-empty content is not valid JSON object or ``mcpServers`` is invalid.
  """
  if not path.exists():
    return {}
  raw = path.read_text(encoding="utf-8").strip()
  if not raw:
    return {}
  data = json.loads(raw)
  if not isinstance(data, dict):
    raise ValueError("Antigravity mcp_config.json must be a JSON object")
  servers = data.get("mcpServers")
  if servers is not None and not isinstance(servers, dict):
    raise ValueError("Antigravity mcp_config.json mcpServers must be a JSON object")
  return data


def preflight(repo: Path, args: argparse.Namespace, home: Path, binary: Path) -> None:
  """Validate every selected host before changing even the shared launcher.

  Args:
    repo: Repository root directory.
    args: Parsed CLI arguments.
    home: User home directory.
    binary: Target launcher binary path.

  Raises:
    ValueError: If host prerequisites fail or conflicting adapters already exist.
  """
  if binary.is_symlink():
    raise ValueError("Refusing a symlinked launcher")
  if binary.exists() and "# managed by delegate host installer" not in binary.read_text():
    raise ValueError("Launcher already exists")
  if args.host in {"codex", "all"}:
    if not shutil.which("codex"):
      raise ValueError("Install/authenticate Codex first")
    codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    config = codex / "config.toml"
    if config.exists() and "delegate" in tomllib.loads(config.read_text()).get("mcp_servers", {}):
      raise ValueError("Codex delegate MCP already exists; inspect before replacing")
    target = codex / "skills/delegate"
    if ((target.exists() or target.is_symlink())
        and not (target.is_symlink() and target.resolve() == (repo / "hosts/codex").resolve())):
      raise ValueError("Codex delegate skill already exists")
  if args.host in {"dsh", "all"}:
    if not shutil.which("dsh"):
      raise ValueError("Install DeepSeek Harness first")
    dsh = Path(os.environ.get("DSH_HOME", str(home / ".dsh")))
    select_dsh_patches(dsh, args.dsh_profile)
    target = dsh / "skills/delegate"
    if ((target.exists() or target.is_symlink())
        and not (target.is_symlink() and target.resolve() == (repo / "hosts/dsh").resolve())):
      raise ValueError("DSH delegate skill already exists")
  if args.host in {"antigravity", "all"}:
    gemini_cfg = home / ".gemini/config"
    mcp_cfg = _load_mcp_config(gemini_cfg / "mcp_config.json")
    servers = mcp_cfg.get("mcpServers") or {}
    if isinstance(servers, dict) and "delegate" in servers:
      raise ValueError("Antigravity delegate MCP already exists; inspect before replacing")
    target = gemini_cfg / "skills/delegate"
    if ((target.exists() or target.is_symlink())
        and not (target.is_symlink() and target.resolve() == (repo / "hosts/antigravity").resolve())):
      raise ValueError("Antigravity delegate skill already exists")


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
  """Atomically write ``content`` to ``path`` with restrictive permissions.

  Args:
    path: Destination file path.
    content: UTF-8 text payload.
    mode: POSIX permission bits applied before replacing target.
  """
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
      stream.write(content)
      stream.flush()
      os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
  finally:
    if os.path.exists(temporary):
      os.unlink(temporary)


def build_launcher_text(
  python_exe: str,
  resolver: Path,
  entry: Path,
  home: Path,
  native_state: str,
  native_codex: str,
  native_claude: str,
) -> str:
  """Render the managed ``~/.local/bin/delegate-agent`` POSIX shell launcher.

  Args:
    python_exe: Python interpreter path.
    resolver: Installed ``runtime_entry.py`` path.
    entry: Frozen fallback ``delegate.py`` path.
    home: User home directory.
    native_state: Resolved ``DELEGATE_STATE_DIR`` path string.
    native_codex: Resolved ``CODEX_HOME`` path string.
    native_claude: Resolved ``CLAUDE_CONFIG_DIR`` path string.

  Returns:
    Complete shell script text for ``delegate-agent``.
  """
  local_bin = shlex.quote(str(home / ".local/bin"))
  return (
    "#!/bin/sh\n# managed by delegate host installer\n"
    'case ":$PATH:" in *:' + local_bin + ':*) ;; *) export PATH=' + local_bin + ':"$PATH" ;; esac\n'
    'if [ -z "${DELEGATE_STATE_DIR:-}" ]; then export DELEGATE_STATE_DIR=' + shlex.quote(native_state) + "; fi\n"
    'if [ -z "${CODEX_HOME:-}" ]; then export CODEX_HOME=' + shlex.quote(native_codex) + "; fi\n"
    'if [ -z "${CLAUDE_CONFIG_DIR:-}" ]; then export CLAUDE_CONFIG_DIR=' + shlex.quote(native_claude) + "; fi\n"
    'case "$1:$2" in\n'
    "  host-mcp:codex|call:codex|wait:codex|loop-wait:codex|"
    "host-mcp:dsh|call:dsh|wait:dsh|loop-wait:dsh|"
    "host-mcp:antigravity|call:antigravity|wait:antigravity|loop-wait:antigravity|"
    "host-mcp:agy|call:agy|wait:agy|loop-wait:agy)\n"
    '    if [ -z "${DELEGATE_CALLER_HOST:-}" ]; then export DELEGATE_CALLER_HOST="$2"; fi ;;\n'
    "esac\n"
    "exec " + shlex.quote(python_exe) + " -B " + shlex.quote(str(resolver))
    + " --fallback " + shlex.quote(str(entry)) + ' "$@"\n'
  )


def main() -> None:
  """Install or refresh managed Codex, DSH, and Antigravity host adapters for delegate."""
  parser = argparse.ArgumentParser()
  parser.add_argument("host", choices=["codex", "dsh", "antigravity", "all"])
  parser.add_argument(
    "--dsh-profile",
    default="web",
    help="DSH profile name or comma-separated list (e.g. web,tui)",
  )
  parser.add_argument(
    "--refresh-launcher",
    action="store_true",
    help="Update only the managed launcher to a frozen runtime; preserve existing host configuration",
  )
  args = parser.parse_args()
  repo = Path(__file__).resolve().parents[1]
  home = Path.home()
  binary = home / ".local/bin/delegate-agent"
  if args.refresh_launcher:
    if binary.is_symlink() or not binary.is_file() or "# managed by delegate host installer" not in binary.read_text():
      raise ValueError("Only an existing managed launcher can be refreshed")
  else:
    preflight(repo, args, home, binary)
  image = runtime_image.freeze(repo / "plugins/delegate/scripts")
  entry = Path(image["path"]) / "delegate.py"
  binary.parent.mkdir(parents=True, exist_ok=True)
  resolver = home / ".local/lib/delegate/runtime_entry.py"
  resolver.parent.mkdir(parents=True, exist_ok=True)
  if resolver.is_symlink() or (
    resolver.exists() and "# managed by delegate runtime resolver" not in resolver.read_text()
  ):
    raise ValueError("Refusing to replace an unmanaged runtime resolver")
  resolver_text = (repo / "plugins/delegate/scripts/runtime_entry.py").read_text()
  _atomic_write(resolver, resolver_text, 0o700)
  state = Path(os.environ.get("DELEGATE_STATE_DIR", str(home / ".claude/delegate"))).resolve()
  native_codex = str(Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).resolve())
  native_claude = str(Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))).resolve())
  text = build_launcher_text(
    sys.executable, resolver, entry, home, str(state), native_codex, native_claude
  )
  if binary.exists() and binary.read_text() != text:
    if "# managed by delegate host installer" not in binary.read_text():
      raise ValueError("Launcher already exists")
    backup(binary)
  _atomic_write(binary, text, 0o700)
  if args.refresh_launcher:
    print(
      "Updated stable CLI resolver; each invocation follows the installed user plugin. Native fallback "
      + image["digest"]
      + ". Already-connected MCP processes keep their engine; old conversations can use this CLI without restart."
    )
    return
  if args.host in {"codex", "all"}:
    codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    config = codex / "config.toml"
    old = tomllib.loads(config.read_text()) if config.exists() else {}
    if "delegate" in old.get("mcp_servers", {}):
      raise ValueError("Codex delegate MCP already exists; inspect before replacing")
    backup(config)
    subprocess.run(
      [
        "codex", "mcp", "add", "delegate",
        "--env", "DELEGATE_STATE_DIR=" + str(state),
        "--env", "CODEX_HOME=" + str(codex.resolve()),
        "--env", "CLAUDE_CONFIG_DIR=" + native_claude,
        "--", str(binary), "host-mcp", "codex",
      ],
      check=True,
    )
    new = tomllib.loads(config.read_text())
    new_without = {k: v for k, v in new.items() if k != "mcp_servers"}
    old_without = {k: v for k, v in old.items() if k != "mcp_servers"}
    other_servers = {k: v for k, v in new.get("mcp_servers", {}).items() if k != "delegate"}
    if new_without != old_without or other_servers != old.get("mcp_servers", {}):
      raise ValueError("Unexpected Codex config changes; inspect backup")
    cfg_text = config.read_text()
    header = re.search(r"^\[mcp_servers\.delegate\]\s*$", cfg_text, re.M)
    if not header:
      raise ValueError("Cannot locate installed MCP table")
    cfg_text = (
      cfg_text[:header.end()]
      + "\nstartup_timeout_sec = 30\ntool_timeout_sec = 180"
      + cfg_text[header.end():]
    )
    config.write_text(cfg_text)
    link(repo / "hosts/codex", codex / "skills/delegate")
  if args.host in {"dsh", "all"}:
    dsh = Path(os.environ.get("DSH_HOME", str(home / ".dsh")))
    patches = select_dsh_patches(dsh, args.dsh_profile)
    overlay = (
      "\n# delegate-host-mcp: shared executor, no keys copied\n"
      "- insert:\n"
      "    - id: delegate-host-mcp\n"
      "      name: '@deepseek-ai/dsh-mcp-client'\n"
      "      config:\n"
      "        serverName: delegate\n"
      "        transport: stdio\n"
      "        command: " + json.dumps(str(binary)) + "\n"
      "        args: ['host-mcp', 'dsh']\n"
      "        env:\n"
      "          DELEGATE_STATE_DIR: " + json.dumps(str(state)) + "\n"
      "          CODEX_HOME: " + json.dumps(native_codex) + "\n"
      "          CLAUDE_CONFIG_DIR: " + json.dumps(native_claude) + "\n"
      "        toolCallTimeoutMs: 120000\n"
    )
    for patch in patches:
      old_patch = patch.read_text(encoding="utf-8")
      backup(patch)
      patch.write_text(profile_prefix(old_patch) + overlay, encoding="utf-8")
    link(repo / "hosts/dsh", dsh / "skills/delegate")
  if args.host in {"antigravity", "all"}:
    gemini_cfg = home / ".gemini/config"
    mcp_path = gemini_cfg / "mcp_config.json"
    mcp_cfg = _load_mcp_config(mcp_path)
    servers = dict(mcp_cfg.get("mcpServers") or {}) if isinstance(mcp_cfg.get("mcpServers"), dict) else {}
    if "delegate" in servers:
      raise ValueError("Antigravity delegate MCP already exists; inspect before replacing")
    servers["delegate"] = {
      "command": str(binary),
      "args": ["host-mcp", "antigravity"],
      "env": {
        "DELEGATE_STATE_DIR": str(state),
        "CODEX_HOME": native_codex,
        "CLAUDE_CONFIG_DIR": native_claude,
      },
    }
    mcp_cfg["mcpServers"] = servers
    backup(mcp_path)
    _atomic_write(mcp_path, json.dumps(mcp_cfg, ensure_ascii=False, indent=2) + "\n", 0o600)
    link(repo / "hosts/antigravity", gemini_cfg / "skills/delegate")
  print(
    "Installed requested adapters; restart Codex / start new DSH or Antigravity session to discover tools. "
    "No account keys or provider settings changed."
  )


if __name__ == "__main__":
  main()

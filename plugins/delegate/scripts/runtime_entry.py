"""Stable CLI resolver: follow the installed user plugin without restarting the parent conversation."""
# managed by delegate runtime resolver
from pathlib import Path
from typing import Any
import json
import os
import re
import stat
import sys


def read_json(path: Path) -> Any:
  """Read only bounded, owned, regular metadata; malformed active installations fail closed."""
  fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
  with os.fdopen(fd, 'r', encoding='utf-8') as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 1048576:
      raise ValueError('Runtime metadata must be an owned regular file of at most1MiB')
    raw = stream.read(1048577)
  if len(raw) > 1048576:
    raise ValueError('Runtime metadata exceeds1MiB')
  return json.loads(raw)


def entry_file(path: Path) -> Path:
  """Require an owned regular engine entry; its own bootstrap verifies/fixes the frozen image."""
  info = path.lstat()
  if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
    raise ValueError('Delegate engine entry must be an owned regular file')
  return path


def resolve(fallback: Path) -> Path:
  """Select the unique installed user-scoped delegate, or the explicitly installed native fallback."""
  root = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude').expanduser()
  registry = root/'plugins/installed_plugins.json'
  try:
    value = read_json(registry)
  except FileNotFoundError:
    return entry_file(fallback)
  if not isinstance(value, dict) or not isinstance(value.get('plugins'), dict):
    raise ValueError('Invalid installed plugin registry; refusing a silent fallback to older code')
  matches = []
  for name, rows in value['plugins'].items():
    if name.split('@')[0] != 'delegate':
      continue
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
      raise ValueError('Invalid delegate plugin registry entries')
    matches.extend(row for row in rows if row.get('scope') == 'user')
  if not matches:
    return entry_file(fallback)
  if len(matches) != 1:
    raise ValueError('Multiple user delegate installations; select one before using the stable CLI')
  selected = matches[0]
  version, location = selected.get('version'), selected.get('installPath')
  if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
      or tuple(map(int, version.split('.'))) < (0, 26, 0)):
    raise ValueError('The installed delegate requires version>=0.26.0 for this CLI; refusing an older policy engine')
  if not isinstance(location, str) or not Path(location).is_absolute():
    raise ValueError('Installed delegate path must be absolute')
  package = Path(location)
  manifest = read_json(package/'.claude-plugin/plugin.json')
  if not isinstance(manifest, dict) or manifest.get('name') != 'delegate' or manifest.get('version') != version:
    raise ValueError('Installed delegate manifest/version mismatch; finish installation before retrying')
  return entry_file(package/'scripts/delegate.py')


def main(argv: list[str]) -> int:
  """Replace this short-lived resolver without consuming stdin or changing host/session identity."""
  try:
    if len(argv) < 2 or argv[0] != '--fallback':
      raise ValueError('Managed entry requires --fallback ENGINE before user arguments')
    target = resolve(Path(argv[1]))
    os.environ['DELEGATE_MANAGED_ENTRY'] = '1'
    os.execv(sys.executable, [sys.executable, '-B', str(target), 'cli', *argv[2:]])
  except (OSError, ValueError, TypeError) as exc:
    print(json.dumps({'error':str(exc), 'help':'Repair/refresh the managed delegate installation; do not rerun completed jobs'}), file=sys.stderr)
    return 2
  return 0


if __name__ == '__main__':
  raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Install local host adapters without credentials, account changes or source rewrites."""
import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import time
import tomllib
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/delegate/scripts'))
import runtime_image


def backup(path):
  if path.exists():
    target=path.with_name(path.name+'.delegate-backup-'+str(time.time_ns()))
    shutil.copy2(path,target)
    os.chmod(target,0o600)
    return target


def link(source,target):
  target.parent.mkdir(parents=True,exist_ok=True)
  if target.is_symlink() and target.resolve()==source.resolve():return
  if target.exists() or target.is_symlink():raise ValueError('Skill/launcher target already exists: '+str(target))
  target.symlink_to(source)


def profile_prefix(text):
  """Normalize a supported empty patch list before appending a block-style overlay."""
  meaningful = [line.split('#',1)[0].strip() for line in text.splitlines()]
  meaningful = [line for line in meaningful if line and line != '---']
  if not meaningful:
    return text
  if meaningful == ['[]']:
    return '\n'.join(line for line in text.splitlines() if line.lstrip().startswith('#')) + '\n'
  if not meaningful[0].startswith('- ') or meaningful[-1] == '...':
    raise ValueError('Use a block-style DSH patch list (or []) before installation')
  return text


def preflight(repo, args, home, binary):
  """Validate every selected host before changing even the shared launcher."""
  if binary.is_symlink():raise ValueError('Refusing a symlinked launcher')
  if binary.exists() and '# managed by delegate host installer' not in binary.read_text():
    raise ValueError('Launcher already exists')
  if args.host in {'codex','all'}:
    if not shutil.which('codex'):raise ValueError('Install/authenticate Codex first')
    codex=Path(os.environ.get('CODEX_HOME',str(home/'.codex')))
    config=codex/'config.toml'
    if config.exists() and 'delegate' in tomllib.loads(config.read_text()).get('mcp_servers',{}):
      raise ValueError('Codex delegate MCP already exists; inspect before replacing')
    target=codex/'skills/delegate'
    if (target.exists() or target.is_symlink()) and not (target.is_symlink() and target.resolve()==(repo/'hosts/codex').resolve()):
      raise ValueError('Codex delegate skill already exists')
  if args.host in {'dsh','all'}:
    if not shutil.which('dsh'):raise ValueError('Install DeepSeek Harness first')
    if not args.dsh_profile or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in args.dsh_profile):
      raise ValueError('Invalid profile name')
    dsh=Path(os.environ.get('DSH_HOME',str(home/'.dsh')))
    patch=dsh/'profiles'/args.dsh_profile/'cordis.patch.yml'
    if not patch.is_file():raise ValueError('Initialize the selected DSH profile before installation')
    if 'delegate-host-mcp' in patch.read_text():raise ValueError('DSH delegate overlay already exists')
    profile_prefix(patch.read_text())
    target=dsh/'skills/delegate'
    if (target.exists() or target.is_symlink()) and not (target.is_symlink() and target.resolve()==(repo/'hosts/dsh').resolve()):
      raise ValueError('DSH delegate skill already exists')


def main():
  parser=argparse.ArgumentParser()
  parser.add_argument('host',choices=['codex','dsh','all'])
  parser.add_argument('--dsh-profile',default='web')
  parser.add_argument('--refresh-launcher',action='store_true',help='Update only the managed launcher to a frozen runtime; preserve existing host configuration')
  args=parser.parse_args()
  repo=Path(__file__).resolve().parents[1]
  home=Path.home()
  binary=home/'.local/bin/delegate-agent'
  if args.refresh_launcher:
    if binary.is_symlink() or not binary.is_file() or '# managed by delegate host installer' not in binary.read_text():
      raise ValueError('Only an existing managed launcher can be refreshed')
  else:
    preflight(repo,args,home,binary)
  image=runtime_image.freeze(repo/'plugins/delegate/scripts')
  entry=Path(image['path'])/'delegate.py'
  binary.parent.mkdir(parents=True,exist_ok=True)
  text='#!/bin/sh\n# managed by delegate host installer\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(entry))+' "$@"\n'
  if binary.exists() and binary.read_text()!=text:
    if '# managed by delegate host installer' not in binary.read_text():raise ValueError('Launcher already exists')
    backup(binary)
  binary.write_text(text);os.chmod(binary,0o700)
  if args.refresh_launcher:
    print('Updated managed launcher to frozen runtime '+image['digest']+'. Existing MCP servers require restart; host settings preserved.')
    return
  state=Path(os.environ.get('DELEGATE_STATE_DIR',str(home/'.claude/delegate'))).resolve()
  if args.host in {'codex','all'}:
    codex=Path(os.environ.get('CODEX_HOME',str(home/'.codex')))
    config=codex/'config.toml'
    old=tomllib.loads(config.read_text()) if config.exists() else {}
    if 'delegate' in old.get('mcp_servers',{}):raise ValueError('Codex delegate MCP already exists; inspect before replacing')
    backup(config)
    subprocess.run(['codex','mcp','add','delegate','--env','DELEGATE_STATE_DIR='+str(state),'--',str(binary),'host-mcp','codex'],check=True)
    new=tomllib.loads(config.read_text())
    new_without={k:v for k,v in new.items() if k!='mcp_servers'}
    old_without={k:v for k,v in old.items() if k!='mcp_servers'}
    if new_without!=old_without or {k:v for k,v in new.get('mcp_servers',{}).items() if k!='delegate'}!=old.get('mcp_servers',{}):
      raise ValueError('Unexpected Codex config changes; inspect backup')
    text = config.read_text()
    header = re.search(r'^\[mcp_servers\.delegate\]\s*$', text, re.M)
    if not header:raise ValueError('Cannot locate installed MCP table')
    text = text[:header.end()] + '\nstartup_timeout_sec = 30\ntool_timeout_sec = 180' + text[header.end():]
    config.write_text(text)
    link(repo/'hosts/codex',codex/'skills/delegate')
  if args.host in {'dsh','all'}:
    if not args.dsh_profile or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in args.dsh_profile):
      raise ValueError('Invalid profile name')
    dsh=Path(os.environ.get('DSH_HOME',str(home/'.dsh')))
    patch=dsh/'profiles'/args.dsh_profile/'cordis.patch.yml'
    old=patch.read_text()
    if 'delegate-host-mcp' in old:raise ValueError('DSH delegate overlay already exists')
    overlay='''\n# delegate-host-mcp: shared executor, no keys copied
- insert:
    - id: delegate-host-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: delegate
        transport: stdio
        command: %s
        args: ['host-mcp', 'dsh']
        env:
          DELEGATE_STATE_DIR: %s
        toolCallTimeoutMs: 120000
''' % (json.dumps(str(binary)),json.dumps(str(state)))
    native_codex=str(Path(os.environ.get('CODEX_HOME',str(home/'.codex'))).resolve())
    native_claude=str(Path(os.environ.get('CLAUDE_CONFIG_DIR',str(home/'.claude'))).resolve())
    overlay=overlay.replace('        toolCallTimeoutMs:', '          CODEX_HOME: '+json.dumps(native_codex)+'\n          CLAUDE_CONFIG_DIR: '+json.dumps(native_claude)+'\n        toolCallTimeoutMs:')
    backup(patch);patch.write_text(profile_prefix(old)+overlay)
    link(repo/'hosts/dsh',dsh/'skills/delegate')
  print('Installed requested adapters; restart Codex/new DSH session to discover tools. No account keys or provider settings changed.')


if __name__=='__main__':main()

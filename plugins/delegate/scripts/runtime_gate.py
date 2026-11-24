"""Reject new MCP launches until that connection has demonstrated the required policy capabilities."""
from typing import Any
import json
import re
import time

import storage as st
from lifecycle import alive

CAPABILITIES = ['model-deny', 'sol-first', 'snapshot-required-files']
OPTIONAL_CAPABILITIES = ['mcp-hot-reload']
MINIMUM = (0, 26, 0)


def live_identity(value: Any) -> bool:
  """Require a well-formed local boot/start identity; malformed status replies fail closed."""
  if (not isinstance(value, dict) or type(value.get('pid')) is not int or value['pid'] < 1
      or not isinstance(value.get('start'), str) or not isinstance(value.get('boot'), str)):
    return False
  try:
    return alive(value)
  except (OSError, ValueError, KeyError, TypeError):
    return False


def response(value: Any, depth: int = 0) -> dict[str, Any] | None:
  """Decode bounded successful MCP envelopes without accepting tool errors as a handshake."""
  if depth > 6:
    return None
  if isinstance(value, str):
    if len(value) > 65536:
      return None
    try:
      return response(json.loads(value), depth + 1)
    except ValueError:
      return None
  if isinstance(value, dict):
    if value.get('isError') or value.get('is_error'):
      return None
    if isinstance(value.get('server_runtime'), dict):
      return value
    for field in ('structuredContent', 'content', 'text', 'result'):
      result = response(value.get(field), depth + 1)
      if result is not None:
        return result
  if isinstance(value, list):
    for item in value[:8]:
      result = response(item, depth + 1)
      if result is not None:
        return result
  return None


def observe(payload: dict[str, Any]) -> None:
  """Remember only a status reply from the exact MCP namespace and owning session."""
  name = str(payload.get('tool_name', ''))
  sid = payload.get('session_id')
  args = payload.get('tool_input', {})
  if (payload.get('hook_event_name') != 'PostToolUse' or payload.get('agent_id') or not name.startswith('mcp__')
      or not name.endswith('__delegate_control') or not isinstance(args, dict)
      or args.get('session_id') != sid or args.get('action', 'status') != 'status'):
    return
  value = response(payload.get('tool_response'))
  if not value or value.get('session_id') != sid:
    return
  runtime = value['server_runtime']
  version = runtime.get('version', '')
  caps = runtime.get('capabilities', [])
  if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
    return
  with st.locked():
    state = st.session(sid)
    records = state.get('runtime_handshakes', {})
    allowed_caps = set(CAPABILITIES) | set(OPTIONAL_CAPABILITIES)
    records[name.rsplit('__', 1)[0]] = {'version': version,
      'capabilities': [x for x in caps if isinstance(x, str) and x in allowed_caps] if isinstance(caps, list) else [],
      'observed_at': time.time(), 'identity': runtime.get('identity')}
    state['runtime_handshakes'] = dict(list(records.items())[-16:])
    st.save_session(sid, state)


def rejection(payload: dict[str, Any]) -> str:
  """Gate launches only; monitoring, cancellation and collection remain available."""
  name = str(payload.get('tool_name', ''))
  args = payload.get('tool_input', {})
  if not name.startswith('mcp__') or not isinstance(args, dict):
    return ''
  launch = name.endswith('__delegate_run') or (name.endswith('__delegate_loop') and args.get('action') in
    {'start', 'resume', 'recover', 'advance', 'retry'})
  if not launch:
    return ''
  sid = payload.get('session_id')
  if args.get('session_id') != sid:
    return 'Delegate launch session_id must match the owning session'
  record = st.session(sid).get('runtime_handshakes', {}).get(name.rsplit('__', 1)[0], {})
  version = record.get('version', '')
  caps = record.get('capabilities', [])
  capable = (isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+', version)
    and tuple(map(int, version.split('.'))) >= MINIMUM
    and set(CAPABILITIES) <= set(caps) and live_identity(record.get('identity')))
  if capable:
    import capabilities
    effective_ver = tuple(map(int, version.split('.')))
    spec_items = (
      [args, args['run_spec']]
      if name.endswith('__delegate_loop') and isinstance(args.get('run_spec'), dict)
      else [args]
    )
    needs_sandbox_controls = False
    for item in spec_items:
      raw_perm = item.get('permissions')
      perm = None
      if isinstance(raw_perm, str) and raw_perm.strip():
        try:
          perm = capabilities.normalize_permissions(raw_perm)
        except ValueError:
          perm = None
      if (perm in {'network-shell', 'full-access'}
          or bool(item.get('writable_roots'))
          or item.get('network_access') is not None):
        needs_sandbox_controls = True
        break
    if needs_sandbox_controls and effective_ver < (0, 26, 7):
      return (
        'New delegate launch blocked: this MCP connection predates delegate >=0.26.7 '
        'parent sandbox controls (writable_roots, network_access, network-shell/full-access; observed: '
        + version
        + '). Use delegate-agent call with the SAME session_id, or run /reload-plugins --force / restart.'
      )
    return ''
  return ('New delegate launch blocked: this MCP connection has not demonstrated delegate >=0.26.0 policy capabilities '
    '(observed: ' + (version or 'unknown') + '). Call delegate_control(action="status") once on the current '
    'connection (no session restart needed after /reload-plugins). If the server is old, use delegate-agent call '
    'with the SAME session_id, or run /reload-plugins --force. '
    'Existing jobs may still be inspected, collected, steered or cancelled; do not rerun them to upgrade.')


def reset(payload: dict[str, Any]) -> None:
  """Invalidate previous connection observations when the host starts/reopens this session."""
  if payload.get('hook_event_name') == 'SessionStart' and not payload.get('agent_id'):
    with st.locked():
      state = st.session(payload['session_id'])
      if state.pop('runtime_handshakes', None) is not None:
        st.save_session(payload['session_id'], state)

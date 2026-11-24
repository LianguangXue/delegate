"""Host-aware entry points for the shared delegation engine."""
from typing import Any
import hashlib
import os
import uuid
import storage as st

NATIVE_HOSTS = frozenset({"codex", "dsh", "antigravity"})
HOST_ALIASES = {"agy": "antigravity"}


def caller_host(default: str = "claude") -> str:
  """Return canonical caller host identifier from ``DELEGATE_CALLER_HOST``.

  Args:
    default: Fallback host identifier when ``DELEGATE_CALLER_HOST`` is unset.

  Returns:
    Normalized host name with ``agy`` mapped to ``antigravity``.
  """
  raw = (os.environ.get("DELEGATE_CALLER_HOST") or default).strip()
  return HOST_ALIASES.get(raw, raw)


def connect(native_session_id: str = "", parent_model: str = "", parent_provider: str = "") -> dict[str, Any]:
  """Create/recover one logical parent session; never guess an account or token balance.

  Args:
    native_session_id: Caller conversation or thread identifier.
    parent_model: Optional parent model identifier for advisory planning.
    parent_provider: Optional configured parent provider identifier.

  Returns:
    Logical ``session_id`` binding dictionary for all delegate tool calls.

  Raises:
    ValueError: If host identity, metadata lengths, or provider fields are invalid.
  """
  host = caller_host()
  if host not in NATIVE_HOSTS:
    raise ValueError("Use delegate_host through the Codex/DSH/Antigravity host adapter")
  if os.environ.get("DELEGATE_WORKER"):
    raise ValueError("Delegated workers cannot recursively delegate")
  if any(not isinstance(v, str) for v in (native_session_id, parent_model, parent_provider)):
    raise ValueError("Host metadata must be text")
  env_native = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip() if host == "antigravity" else ""
  native = native_session_id or env_native or str(uuid.uuid4())
  if len(native) > 200 or len(parent_model) > 200:
    raise ValueError("Host identifiers too long")
  if parent_provider and parent_provider not in st.config()["providers"]:
    raise ValueError("Unknown parent provider")
  provider = parent_provider or ("codex" if host == "codex" else "")
  sid = host + "-" + hashlib.sha256(native.encode()).hexdigest()[:32]
  with st.locked():
    state = st.session(sid)
    if state.get("host_kind") not in {None, host}:
      raise ValueError("Parent host mismatch")
    state.update(host_kind=host, parent_provider=provider, parent_model=parent_model)
    st.save_session(sid, state)
  return {
    "session_id": sid,
    "native_session_id": native,
    "host": host,
    "mode": state["mode"],
    "parent_provider": provider or None,
    "parent_usage": "unknown unless independently metered",
    "instructions": (
      "Use this session_id for all delegate tools. Keep it per conversation; do not share with another "
      "parent. Collect results before acceptance. No host-native idle wake is assumed; use result wait "
      "or the background waiter."
    ),
  }


def check(arguments: dict[str, Any]) -> None:
  """Keep host namespaces distinct without changing legacy Claude session semantics."""
  if os.environ.get("DELEGATE_WORKER"):
    raise ValueError("Delegated workers cannot recursively call delegate")
  host = caller_host()
  sid = arguments.get("session_id")
  if sid:
    recorded = st.session(sid).get("host_kind", "claude")
    if recorded != host:
      raise ValueError("Call delegate_host first and use this host conversation's session_id")

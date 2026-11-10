"""Host-aware entry points for the shared delegation engine."""
from typing import Any
import hashlib
import os
import uuid
import storage as st


def connect(native_session_id: str = "", parent_model: str = "", parent_provider: str = "") -> dict[str, Any]:
  """Create/recover one logical parent session; never guess an account or token balance."""
  host = os.environ.get("DELEGATE_CALLER_HOST", "claude")
  if host not in {"codex", "dsh"}:
    raise ValueError("Use delegate_host through the Codex/DSH host adapter")
  if os.environ.get("DELEGATE_WORKER"):
    raise ValueError("Delegated workers cannot recursively delegate")
  if any(not isinstance(v, str) for v in (native_session_id, parent_model, parent_provider)):
    raise ValueError("Host metadata must be text")
  native = native_session_id or str(uuid.uuid4())
  if len(native)>200 or len(parent_model)>200:
    raise ValueError("Host identifiers too long")
  if parent_provider and parent_provider not in st.config()["providers"]:
    raise ValueError("Unknown parent provider")
  provider = parent_provider or ("codex" if host == "codex" else "")
  sid = host + "-" + hashlib.sha256(native.encode()).hexdigest()[:32]
  with st.locked():
    state = st.session(sid)
    if state.get("host_kind") not in {None,host}:
      raise ValueError("Parent host mismatch")
    state.update(host_kind=host, parent_provider=provider, parent_model=parent_model)
    st.save_session(sid,state)
  return {"session_id":sid,"native_session_id":native,"host":host,"mode":state["mode"],
    "parent_provider":provider or None,"parent_usage":"unknown unless independently metered",
    "instructions":"Use this session_id for all delegate tools. Keep it per conversation; do not share with another parent. "
      "Collect results before acceptance. No host-native idle wake is assumed; use result wait or the background waiter."}


def check(arguments: dict[str, Any]) -> None:
  """Keep host namespaces distinct without changing legacy Claude session semantics."""
  if os.environ.get("DELEGATE_WORKER"):
    raise ValueError("Delegated workers cannot recursively call delegate")
  host=os.environ.get("DELEGATE_CALLER_HOST", "claude")
  sid=arguments.get("session_id")
  if sid:
    recorded = st.session(sid).get("host_kind", "claude")
    if recorded != host:
      raise ValueError("Call delegate_host first and use this host conversation's session_id")

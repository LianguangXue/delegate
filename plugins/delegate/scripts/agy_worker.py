"""Antigravity (agy) CLI catalog discovery and bounded non-interactive worker execution."""
from pathlib import Path
from typing import Any, Callable
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import capabilities
import credentials
import diagnostics
from lifecycle import identity, stop_owned
import runtime
import storage as st

DEFAULT_EFFORTS: list[str] = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_MODELS: list[str] = [
  "gemini-4-neon-high",
  "gemini-4-neon-xhigh",
  "gemini-4-neon-medium",
  "gemini-4-neon-low",
  "gemini-4-carbon-high",
  "gemini-4-carbon-medium",
  "gemini-4-carbon-low",
  "gemini-4-argon-high",
  "gemini-4-argon-medium",
  "gemini-4-argon-low",
  "gemini-4-hydrogen-high",
  "gemini-4-hydrogen-xhigh",
  "gemini-4-hydrogen-medium",
  "gemini-4-hydrogen-low",
  "gemini-pro-orchestrator-neon",
  "gemini-pro-orchestrator",
  "gemini-pro-orchestrator-argon",
  "gemini-3.8-flash-high",
  "gemini-3.8-flash-medium",
  "gemini-3.8-flash-low",
]
CATALOG_TTL_SECONDS: int = 300
_MAX_DIRECT_PROMPT_BYTES: int = 120000
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TWO_COL_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?:\t+|\s{2,})(.+)$")
_SLUG_COL_RE = re.compile(r"^([a-z0-9][a-z0-9._-]*[-_.0-9][a-z0-9._-]*)\s+(.+)$")
_SKIP_PREFIXES = ("fetching ", "loading ", "available ", "usage:", "error:", "warning:", "flags:")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_SENSITIVE_KEYS = frozenset({
  "token", "refresh_token", "access_token", "id_token", "auth_token", "oauth_token",
  "bearer_token", "session_token", "api_key", "apikey", "x_api_key", "secret",
  "client_secret", "api_secret", "secret_key", "password", "passwd", "authorization",
  "proxy_authorization", "cookie", "set_cookie", "credential", "credentials", "private_key",
})
_SENSITIVE_SUFFIXES = (
  "_api_key", "_apikey", "_secret", "_password", "_passwd", "_private_key",
  "_auth_token", "_access_token", "_refresh_token", "_id_token", "_bearer_token",
  "_session_token", "_oauth_token", "_credential", "_credentials",
)
_SAFE_COUNTER_SUFFIXES = ("_tokens", "_token_count", "_token_limit", "_token_budget")
_AUTH_HEADER_RE = re.compile(
  r"(?i)(\b(?:proxy[_-]?)?authorization[\"']?\s*(?:=|:)\s*)"
  r"(?:\"([^\"]*)\"|'([^']*)'|"
  r"((?:Bearer|Basic|Digest|Token|Negotiate)\b[^\r\n;}{]*)|"
  r"([^\s\"',;}{]+))"
)
_COOKIE_HEADER_RE = re.compile(
  r"(?i)(\b(?:set[_-]?)?cookie[\"']?\s*(?:=|:)\s*)"
  r"(?:\"([^\"]*)\"|'([^']*)'|([^\r\n\"'}{]+))"
)
_KV_SECRET_RE = re.compile(
  r"(?i)(\b(?!\w*tokens\b|token[_-]?(?:count|limit|budget)\b)"
  r"(?:refresh[_-]?token|access[_-]?token|id[_-]?token|auth[_-]?token|"
  r"oauth[_-]?token|bearer[_-]?token|session[_-]?token|api[_-]?key|x[_-]?api[_-]?key|"
  r"client[_-]?secret|api[_-]?secret|private[_-]?key|secret[_-]?key|secret|"
  r"password|passwd|credentials?|token)\b"
  r"[\"']?\s*(?:=|:)\s*)"
  r"(?:\"([^\"]*)\"|'([^']*)'|"
  r"((?:Bearer|Basic|Digest|Token|Negotiate)\s+[^\s\"',;}{]+)|"
  r"([^\s\"',;}{]+))"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN_RE = re.compile(
  r"\b(?:ya29\.[A-Za-z0-9._-]+|1//[A-Za-z0-9._-]{8,}|sk-[A-Za-z0-9_-]{12,})\b"
)
_MEMORY_CACHE: dict[str, dict[str, Any]] = {}


def clear_catalog_cache() -> None:
  """Clear the in-process catalog cache for deterministic unit testing."""
  _MEMORY_CACHE.clear()


def _strip_ansi(text: str) -> str:
  """Remove ANSI escape sequences from CLI terminal output."""
  return _ANSI_RE.sub("", text)


def _resolve_binary(command: str, env: dict[str, str] | None = None) -> str:
  """Resolve ``agy`` executable path, checking ``~/.local/bin`` when absent from PATH."""
  raw = (command or "agy").strip() or "agy"
  search_path = (env or os.environ).get("PATH", "")
  found = shutil.which(raw, path=search_path)
  if found:
    return found
  if raw == "agy":
    fallback = Path.home() / ".local" / "bin" / "agy"
    if fallback.is_file():
      return str(fallback)
  return raw


def parse_models_output(raw: str, efforts: list[str] | None = None) -> list[dict[str, Any]]:
  """Parse ``agy models`` stdout (including spinner frames or JSON) into model metadata rows.

  Args:
    raw: Raw stdout text returned by ``agy models``.
    efforts: Supported effort levels advertised for each model.

  Returns:
    Ordered, deduplicated list of ``model_details`` dictionaries containing ``id``, ``label``,
    and ``effort.supported_levels``.
  """
  levels = list(efforts or DEFAULT_EFFORTS)
  cleaned = _strip_ansi(raw or "")
  stripped = cleaned.strip()
  if stripped.startswith(("{", "[")):
    try:
      parsed = json.loads(stripped)
      items = parsed.get("models", parsed.get("data", [])) if isinstance(parsed, dict) else parsed
      if isinstance(items, list):
        rows: list[dict[str, Any]] = []
        seen_json: set[str] = set()
        for entry in items:
          mid = entry if isinstance(entry, str) else (
            entry.get("id") or entry.get("model") if isinstance(entry, dict) else None
          )
          if isinstance(mid, str) and mid.strip() and mid.strip() not in seen_json:
            clean_id = mid.strip()
            seen_json.add(clean_id)
            label = entry.get("label") or entry.get("name") or clean_id if isinstance(entry, dict) else clean_id
            rows.append({"id": clean_id, "label": str(label), "effort": {"supported_levels": list(levels)}})
        if rows:
          return rows
    except ValueError:
      pass

  details: list[dict[str, Any]] = []
  seen: set[str] = set()
  for segment in cleaned.splitlines():
    line = segment.strip()
    if not line or line.startswith(("#", "-")):
      continue
    if "\u2800" <= line[0] <= "\u28ff":
      continue
    if line.lower().startswith(_SKIP_PREFIXES):
      continue
    model_id: str | None = None
    label: str = ""
    two_col = _TWO_COL_RE.match(line)
    if two_col:
      model_id, label = two_col.group(1), two_col.group(2).strip()
    elif _BARE_ID_RE.match(line):
      model_id, label = line, line
    else:
      slug_col = _SLUG_COL_RE.match(line)
      if slug_col:
        model_id, label = slug_col.group(1), slug_col.group(2).strip()
    if model_id and model_id not in seen:
      seen.add(model_id)
      details.append({"id": model_id, "label": label or model_id, "effort": {"supported_levels": list(levels)}})
  return details


def _cache_identity(provider: dict[str, Any], efforts: list[str]) -> tuple[str, str]:
  """Compute memory key and disk cache filename scoped to state root and provider config."""
  state_root = str(st.root())
  payload = json.dumps(
    {
      "command": provider.get("command") or "agy",
      "models": provider.get("models") or [],
      "efforts": efforts,
      "default_model": provider.get("default_model") or "",
    },
    sort_keys=True,
  )
  digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
  return state_root + ":" + digest, "catalog-antigravity-" + digest + ".json"


def catalog(provider: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
  """Discover Antigravity models and efforts with in-memory and disk TTL caching.

  Args:
    provider: Provider configuration dictionary for an ``antigravity`` provider.
    refresh: Force fresh ``agy models`` execution instead of reusing cached discovery.

  Returns:
    Catalog dictionary with ``provider_kind``, ``models``, ``efforts``, ``default_model``,
    ``model_details``, and ``source``.
  """
  efforts = list(provider.get("efforts") or DEFAULT_EFFORTS)
  allowlist = [m for m in (provider.get("models") or []) if isinstance(m, str) and m.strip()]
  ttl = int(provider.get("catalog_ttl_seconds") or CATALOG_TTL_SECONDS)
  mem_key, disk_name = _cache_identity(provider, efforts)
  now = time.time()

  if not refresh:
    mem_hit = _MEMORY_CACHE.get(mem_key)
    if mem_hit and 0 <= now - float(mem_hit.get("observed_at", 0)) < ttl:
      return copy.deepcopy(mem_hit)
    disk_hit = st.read(disk_name)
    if isinstance(disk_hit, dict) and 0 <= now - float(disk_hit.get("observed_at", 0)) < ttl:
      _MEMORY_CACHE[mem_key] = copy.deepcopy(disk_hit)
      return copy.deepcopy(disk_hit)

  cmd = _resolve_binary(str(provider.get("command") or "agy"))
  env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "DELEGATE_WORKER": "1"}
  discovered: list[dict[str, Any]] = []
  source = "agy models"
  try:
    proc = subprocess.run(
      [cmd, "models"],
      stdin=subprocess.DEVNULL,
      capture_output=True,
      text=True,
      env=env,
      timeout=15,
      check=False,
    )
    if proc.returncode == 0:
      discovered = parse_models_output(proc.stdout, efforts=efforts)
  except (OSError, subprocess.SubprocessError):
    discovered = []

  if discovered:
    label_map = {row["id"]: row.get("label", row["id"]) for row in discovered}
    discovered_ids = [row["id"] for row in discovered]
    if allowlist:
      filtered = [m for m in discovered_ids if m in allowlist]
      models_list = filtered if filtered else list(allowlist)
      source = "agy models (filtered by allowlist)" if filtered else "configured allowlist; no overlap with agy models"
    else:
      models_list = discovered_ids
  else:
    models_list = list(allowlist) if allowlist else list(DEFAULT_MODELS)
    label_map = {m: m for m in models_list}
    source = (
      "configured allowlist; agy models unavailable"
      if allowlist
      else "fallback defaults; agy models unavailable"
    )

  configured_default = provider.get("default_model")
  default_model: str | None
  if isinstance(configured_default, str) and configured_default.strip():
    default_model = configured_default.strip()
    if default_model not in models_list:
      models_list = [default_model, *models_list]
  else:
    default_model = models_list[0] if models_list else None

  model_details = [
    {"id": mid, "label": label_map.get(mid, mid), "effort": {"supported_levels": list(efforts)}}
    for mid in models_list
  ]
  value: dict[str, Any] = {
    "provider_kind": "antigravity",
    "models": models_list,
    "efforts": efforts,
    "default_model": default_model,
    "model_details": model_details,
    "source": source,
    "observed_at": time.time(),
  }
  try:
    st.write(disk_name, value)
  except OSError:
    pass
  _MEMORY_CACHE[mem_key] = copy.deepcopy(value)
  return copy.deepcopy(value)


def _resolve_job_permissions(job: dict[str, Any], agent: dict[str, Any]) -> str:
  """Resolve canonical subagent permission tier for an Antigravity worker job."""
  execution = job.get("execution_environment") or {}
  raw = execution.get("permissions") if "permissions" in execution else job.get("permissions")
  if raw is None:
    raw = agent.get("permissions", "auto")
  if not isinstance(raw, str):
    raise ValueError("Unsupported worker permissions: " + str(raw))
  if raw != "auto":
    if raw not in capabilities.PERMISSIONS:
      raise ValueError("Unsupported worker permissions: " + str(raw))
    return raw
  workspace = (job.get("role") or agent or {}).get("workspace", "read-only")
  return "read-only" if workspace == "read-only" else "workspace-write"


def _extract_cli_session(job: dict[str, Any]) -> str | None:
  """Extract existing conversation ID for ``--conversation`` resume when present."""
  raw = job.get("cli_session")
  if isinstance(raw, str) and raw.strip():
    return raw.strip()
  if isinstance(raw, dict):
    for key in ("conversation_id", "conversationId", "id"):
      candidate = raw.get(key)
      if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
  thread_id = job.get("thread_id")
  if isinstance(thread_id, str) and thread_id.strip():
    return thread_id.strip()
  return None


def _build_prompt(job: dict[str, Any]) -> str:
  """Extract prepared worker prompt or synthesize bounded task text for standalone calls."""
  explicit = job.get("prompt")
  if isinstance(explicit, str) and explicit.strip():
    return explicit
  parts = [str(job.get("task") or "").strip()]
  if job.get("acceptance"):
    parts.append("ACCEPTANCE\n" + str(job["acceptance"]).strip())
  if job.get("context"):
    parts.append("CONTEXT\n" + str(job["context"]).strip())
  text = "\n\n".join(p for p in parts if p)
  if not text:
    raise ValueError("Antigravity worker requires a non-empty prompt or task")
  return text


def build_command(
  job: dict[str, Any],
  agent: dict[str, Any],
  provider: dict[str, Any],
  prompt: str,
  extra_dirs: list[str] | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
  """Construct the exact ``agy`` CLI argv, isolated worker environment, and resume ID.

  Args:
    job: Job specification dictionary.
    agent: Effective or configured agent role dictionary.
    provider: Antigravity provider dictionary.
    prompt: Non-empty prompt string passed to ``agy -p``.
    extra_dirs: Optional extra workspace directories (e.g. prompt spillover dir).

  Returns:
    Tuple of ``(argv, env, resume_conversation_id)``.

  Raises:
    ValueError: If permissions are unsupported or prompt is empty.
  """
  if not isinstance(prompt, str) or not prompt.strip():
    raise ValueError("Antigravity prompt must be a non-empty string")
  perms = _resolve_job_permissions(job, agent)
  env = dict(os.environ)
  env.pop("DELEGATE_CALLER_HOST", None)
  env["DELEGATE_WORKER"] = "1"
  local_bin = str(Path.home() / ".local" / "bin")
  path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
  if local_bin not in path_parts:
    env["PATH"] = os.pathsep.join([local_bin, *path_parts])
  if perms in {"workspace-shell", "network-shell", "full-access"}:
    try:
      env.update(capabilities.environment(job))
    except Exception:
      pass

  execution = job.get("execution_environment") or {}
  user_roots = list(execution.get("writable_roots") or job.get("writable_roots") or [])
  spill_roots = [str(d) for d in (extra_dirs or []) if str(d).strip()]
  dir_flags: list[str] = []
  allowed_roots = spill_roots if perms == "read-only" else [*user_roots, *spill_roots]
  for root in dict.fromkeys(str(r) for r in allowed_roots if str(r).strip()):
    dir_flags.extend(["--add-dir", root])

  if perms == "read-only":
    perm_flags = ["--mode", "plan", "--sandbox", *dir_flags]
  elif perms == "workspace-write":
    perm_flags = ["--sandbox", "--mode", "accept-edits", *dir_flags]
  elif perms in {"workspace-shell", "network-shell"}:
    perm_flags = ["--sandbox", "--mode", "auto", *dir_flags]
  elif perms == "full-access":
    perm_flags = ["--dangerously-skip-permissions", *dir_flags]
  else:
    raise ValueError("Unsupported worker permissions: " + str(perms))

  binary = _resolve_binary(str(provider.get("command") or "agy"), env=env)
  argv: list[str] = [binary, "-p", prompt, "--output-format", "json", "--disable-slash-commands"]
  model = job.get("model") or agent.get("default_model") or provider.get("default_model")
  if isinstance(model, str) and model.strip() and model.strip() != "auto":
    argv.extend(["--model", model.strip()])
  effort = job.get("effort")
  if isinstance(effort, str) and effort.strip() and effort.strip() != "auto":
    argv.extend(["--effort", effort.strip()])
  resume_id = _extract_cli_session(job)
  if resume_id:
    argv.extend(["--conversation", resume_id])
  argv.extend(perm_flags)
  return argv, env, resume_id


def _is_sensitive_key(key: str) -> bool:
  """Return True if a JSON key names a credential/token field rather than a token counter."""
  normalized = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", key.strip()).lower().replace("-", "_")
  if normalized in {"tokens", "token_count", "token_budget"} or normalized.endswith(_SAFE_COUNTER_SUFFIXES):
    return False
  return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def _redact_text(text: str, env: dict[str, str], secret_values: list[str]) -> str:
  """Scrub env secrets, inline key=value credentials, and auth/cookie headers from text."""
  scrubbed = diagnostics.redact(credentials.redact(text, env), secret_values)

  def _replace_kv(match: re.Match[str]) -> str:
    prefix = match.group(1)
    if match.group(2) is not None:
      return prefix + '"[REDACTED]"'
    if match.group(3) is not None:
      return prefix + "'[REDACTED]'"
    return prefix + "[REDACTED]"

  scrubbed = _AUTH_HEADER_RE.sub(_replace_kv, scrubbed)
  scrubbed = _COOKIE_HEADER_RE.sub(_replace_kv, scrubbed)
  scrubbed = _KV_SECRET_RE.sub(_replace_kv, scrubbed)
  scrubbed = _BEARER_TOKEN_RE.sub(r"\1[REDACTED]", scrubbed)
  return _KNOWN_TOKEN_RE.sub("[REDACTED]", scrubbed)


def _redact_data(data: Any, env: dict[str, str], secret_values: list[str]) -> Any:
  """Recursively redact secret values and credential-keyed fields inside JSON structures."""
  if isinstance(data, str):
    return _redact_text(data, env, secret_values)
  if isinstance(data, list):
    return [_redact_data(item, env, secret_values) for item in data]
  if isinstance(data, dict):
    cleaned: dict[str, Any] = {}
    for raw_k, raw_v in data.items():
      key_str = str(raw_k)
      safe_k = _redact_text(key_str, env, secret_values)
      if _is_sensitive_key(key_str):
        cleaned[safe_k] = raw_v if (raw_v is None or type(raw_v) is bool) else "[REDACTED]"
      else:
        cleaned[safe_k] = _redact_data(raw_v, env, secret_values)
    return cleaned
  return data


def _normalize_usage(raw_usage: Any) -> dict[str, Any]:
  """Ensure usage dict preserves raw counters and exposes ``input_tokens``/``output_tokens``."""
  if not isinstance(raw_usage, dict):
    return {}
  usage = dict(raw_usage)
  if "input_tokens" not in usage and isinstance(usage.get("inputTokens"), int):
    usage["input_tokens"] = usage["inputTokens"]
  if "output_tokens" not in usage and isinstance(usage.get("outputTokens"), int):
    usage["output_tokens"] = usage["outputTokens"]
  if "cache_read_input_tokens" not in usage and isinstance(usage.get("cachedInputTokens"), int):
    usage["cache_read_input_tokens"] = usage["cachedInputTokens"]
  return usage


def _extract_json_dict(raw_stdout: str) -> dict[str, Any] | None:
  """Parse whole stdout or final JSON-object line when spinner text precedes JSON."""
  stripped = raw_stdout.strip()
  if not stripped:
    return None
  candidates = [stripped]
  cleaned = _strip_ansi(raw_stdout)
  for segment in reversed(cleaned.splitlines()):
    cand = segment.strip()
    if cand.startswith("{") and cand.endswith("}"):
      candidates.append(cand)
      break
  for candidate in candidates:
    try:
      parsed = json.loads(candidate)
    except ValueError:
      continue
    if isinstance(parsed, dict):
      return parsed
  return None


def _clean_text_fallback(raw_stdout: str) -> str:
  """Clean terminal spinner carriage-return prefixes from plain-text stdout."""
  cleaned = _strip_ansi(raw_stdout)
  kept: list[str] = []
  for raw_line in cleaned.split("\n"):
    segments = [seg for seg in raw_line.split("\r") if seg != ""]
    if not segments:
      kept.append("")
      continue
    final_seg = segments[-1]
    stripped = final_seg.lstrip()
    if stripped and "\u2800" <= stripped[0] <= "\u28ff":
      continue
    kept.append(final_seg)
  return "\n".join(kept).strip()


def parse_output(
  raw_stdout: str,
  env: dict[str, str],
  fallback_session: str | None = None,
) -> dict[str, Any]:
  """Parse ``agy`` JSON or fallback plain-text stdout into the canonical worker return shape.

  Args:
    raw_stdout: Captured standard output from ``agy -p``.
    env: Subprocess environment used for secret redaction.
    fallback_session: Prior conversation ID retained if stdout omits one.

  Returns:
    Dictionary with ``result``, ``usage``, ``model_usage``, ``cli_session``,
    and ``permission_denials``.

  Raises:
    RuntimeError: If parsed JSON reports ``is_error=True``.
  """
  secret_values = diagnostics.secrets(env)
  parsed = _extract_json_dict(raw_stdout)
  if parsed is not None:
    safe = _redact_data(parsed, env, secret_values)
    if safe.get("is_error") is True:
      err = safe.get("error") or safe.get("error_message") or safe.get("result") or "unknown Antigravity error"
      raise RuntimeError("Antigravity worker error: " + str(err)[:4000])
    text_val: Any = ""
    for field in ("result", "response", "output", "text"):
      if field in safe and safe[field] is not None:
        text_val = safe[field]
        break
    result_str = text_val if isinstance(text_val, str) else json.dumps(text_val, ensure_ascii=False)
    usage = _normalize_usage(safe.get("usage"))
    raw_model_usage = safe.get("model_usage") if isinstance(safe.get("model_usage"), dict) else safe.get("modelUsage")
    model_usage = dict(raw_model_usage) if isinstance(raw_model_usage, dict) else {}
    cli_session: str | None = fallback_session
    for key in ("conversation_id", "conversationId", "session_id", "sessionId", "cli_session"):
      val = safe.get(key)
      if isinstance(val, str) and val.strip():
        cli_session = val.strip()
        break
    raw_denials = safe.get("permission_denials")
    if not isinstance(raw_denials, list):
      raw_denials = safe.get("permissionDenials")
    denials = list(raw_denials) if isinstance(raw_denials, list) else []
    return {
      "result": result_str,
      "usage": usage,
      "model_usage": model_usage,
      "cli_session": cli_session,
      "permission_denials": denials,
    }

  redacted_text = _redact_text(_clean_text_fallback(raw_stdout), env, secret_values)
  return {
    "result": redacted_text,
    "usage": {},
    "model_usage": {},
    "cli_session": fallback_session,
    "permission_denials": [],
  }


def _has_stored_job(job_id: str | None) -> bool:
  """Check whether ``job_id`` corresponds to a persisted job record in ``storage``."""
  if not isinstance(job_id, str) or not job_id:
    return False
  try:
    return st.read("job-" + st.key(job_id) + ".json") is not None
  except ValueError:
    return False


def run_agy(job: dict[str, Any], agent: dict[str, Any], provider: dict[str, Any],
            *, before_spawn: Callable[[], None] | None = None) -> dict[str, Any]:
  """Execute ``agy`` non-interactively with scoped permissions and parse structured output.

  Args:
    job: Job record or execution dictionary (supports ``cli_session``, ``permissions``,
      ``writable_roots``, ``model``, ``effort``, ``cwd``, and ``prompt``/``task``).
    agent: Effective or configured agent role dictionary.
    provider: Antigravity provider dictionary.
    before_spawn: Revalidate a tracked job immediately before starting the CLI.

  Returns:
    Dictionary containing ``result``, ``usage``, ``model_usage``, ``cli_session``,
    and ``permission_denials``.

  Raises:
    ValueError: If permissions or prompt inputs are invalid.
    InterruptedError: If job cancellation is claimed during execution.
    TimeoutError: If job deadline expires during execution.
    RuntimeError: If ``agy`` exits non-zero or returns an error payload.
  """
  full_prompt = _build_prompt(job)
  cwd = str(Path(job.get("cwd") or job.get("source_cwd") or ".").resolve())
  job_id = job.get("id") if isinstance(job.get("id"), str) else None
  tracked = _has_stored_job(job_id)
  spill_dir: Path | None = None
  try:
    extra_dirs: list[str] = []
    prompt_arg = full_prompt
    if len(full_prompt.encode("utf-8")) > _MAX_DIRECT_PROMPT_BYTES:
      spill_base = st.root() / "agy-prompts"
      spill_base.mkdir(parents=True, mode=0o700, exist_ok=True)
      spill_dir = Path(tempfile.mkdtemp(prefix="job-", dir=spill_base))
      os.chmod(spill_dir, 0o700)
      spill_file = spill_dir / "prompt.md"
      spill_file.write_text(full_prompt, encoding="utf-8")
      os.chmod(spill_file, 0o600)
      extra_dirs.append(str(spill_dir))
      prompt_arg = (
        "Read the complete task packet from "
        + str(spill_file)
        + " first, then execute its instructions and acceptance criteria."
      )

    command, env, resume_id = build_command(job, agent, provider, prompt_arg, extra_dirs=extra_dirs)
    secret_values = diagnostics.secrets(env)
    output_path = st.root() / ("output-" + st.key(job_id) + ".json") if tracked and job_id else None
    error_path = st.root() / ("stderr-" + st.key(job_id) + ".txt") if tracked and job_id else None

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output, tempfile.TemporaryFile(
      mode="w+", encoding="utf-8"
    ) as error:
      if before_spawn:
        before_spawn()
      if tracked and job_id:
        st.update_job(job_id, backend_starting=True, inference_state="attempted", inference_attempted_at=time.time())
      try:
        proc = subprocess.Popen(
          command,
          cwd=cwd,
          env=env,
          stdin=subprocess.DEVNULL,
          stdout=output,
          stderr=error,
          text=True,
          start_new_session=True,
        )
      except OSError:
        if tracked and job_id:
          st.update_job(job_id, backend_starting=False, inference_state="not_started")
        raise

      proc_id = identity(proc.pid, include_zombies=True)
      try:
        started = time.monotonic()
        if tracked and job_id:
          st.update_job(
            job_id,
            inference_identity=proc_id,
            backend_identity=proc_id,
            backend_starting=False,
            execution_started_at=time.time(),
            progress_observable=False,
            phase="executing",
          )
          runtime.settings(job_id)
        timeout_sec = job.get("timeout_seconds")
        local_deadline = (
          started + float(timeout_sec)
          if isinstance(timeout_sec, (int, float)) and timeout_sec > 0
          else float("inf")
        )
        while proc.poll() is None:
          if tracked and job_id:
            current = runtime.settings(job_id)
            deadline = runtime.deadline(current, started)
            if current.get("cancel_requested") or time.monotonic() >= deadline:
              if reason := runtime.claim_stop(job_id, started):
                if reason == "cancelled":
                  raise InterruptedError("Antigravity worker cancelled")
                raise TimeoutError("Antigravity worker timed out")
            st.update_job(job_id, heartbeat=time.time())
          elif time.monotonic() >= local_deadline:
            raise TimeoutError("Antigravity worker timed out")
          time.sleep(0.2 if not tracked else 1.0)

        if tracked and job_id:
          st.update_job(job_id, execution_finished_at=time.time(), phase="collecting-result")
        output.flush()
        error.flush()
        output.seek(0)
        error.seek(0)
        raw_out = output.read()
        raw_err = error.read()
        if proc.returncode != 0:
          parsed_err = _extract_json_dict(raw_out)
          if parsed_err is not None:
            safe_err = _redact_data(parsed_err, env, secret_values)
            detail = safe_err.get("error") or safe_err.get("error_message") or safe_err.get("result") or ""
            if detail:
              raise RuntimeError("Antigravity worker failed: " + str(detail)[-4000:])
          combined = _redact_text((raw_err + "\n" + raw_out).strip(), env, secret_values)
          raise RuntimeError("Antigravity worker failed (exit " + str(proc.returncode) + "): " + combined[-4000:])
        return parse_output(raw_out, env, fallback_session=resume_id)
      finally:
        try:
          stop_owned(proc, proc_id)
        finally:
          if output_path is not None and error_path is not None:
            for stream, path in ((output, output_path), (error, error_path)):
              stream.seek(0)
              raw = stream.read()
              parsed_log = _extract_json_dict(raw)
              if parsed_log is not None:
                safe_log = json.dumps(_redact_data(parsed_log, env, secret_values), ensure_ascii=False)
              else:
                safe_log = _redact_text(raw, env, secret_values)
              path.write_text(safe_log, encoding="utf-8")
  finally:
    if spill_dir is not None:
      shutil.rmtree(spill_dir, ignore_errors=True)

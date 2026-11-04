"""Read explicitly selected Switch Bridge API snapshots without copying credentials."""
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError
import hashlib
import hmac
import json
import os
import stat


def private_json(path: Path) -> dict[str, Any]:
  """Read a bounded private regular file; errors never include credential contents."""
  try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
      info = os.fstat(stream.fileno())
      if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 131072:
        raise ValueError()
      value = json.load(stream)
      if not isinstance(value, dict):
        raise ValueError()
      return value
  except (OSError, ValueError):
    raise ValueError("Credential source must be a valid private owned JSON file") from None


def bridge(provider: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
  """Validate the Bridge fingerprint and pinned endpoint before accessing a key."""
  try:
    root = Path(provider["bridge_state_dir"]).resolve(strict=True)
    alias = provider["bridge_account"]
    if not isinstance(alias, str) or not alias or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in alias):
      raise ValueError()
    state = private_json(root / "state.json")
    profile = state["profiles"][alias]
    if profile["auth_mode"] != "api" or profile["api_preset"] not in {"deepseek", "deepseek-official"}:
      raise ValueError()
    directory = Path(profile["config_dir"]).resolve(strict=True)
    expected_parent = (root / "api-accounts" / alias).resolve(strict=True)
    if directory.parent != expected_parent or directory == expected_parent:
      raise ValueError()
    settings = private_json(directory / "settings.json")
    fingerprint = 'api:' + hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    if not hmac.compare_digest(fingerprint, profile["principal"]):
      raise ValueError()
    env = settings["env"]
    if env["ANTHROPIC_BASE_URL"] != profile["base_url"] or provider["base_url"] != profile["base_url"]:
      raise ValueError()
    if profile["api_preset"] == "deepseek-official" and profile["base_url"] != "https://api.deepseek.com/anthropic":
      raise ValueError()
    key_name = "ANTHROPIC_API_KEY" if profile["api_preset"] == "deepseek-official" else "ANTHROPIC_AUTH_TOKEN"
    if not isinstance(env[key_name], str) or not env[key_name] or any(c.isspace() for c in env[key_name]):
      raise ValueError()
    fields = {key_name, "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_API_TYPE",
              "CLAUDE_CODE_HEADERS", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "CLAUDE_CODE_ALLOW_UNKNOWN_MODELS"}
    selected = {k: v for k, v in env.items() if k in fields}
    return profile, selected
  except (KeyError, TypeError, ValueError, OSError):
    raise ValueError("Switch Bridge account, private snapshot, fingerprint or endpoint binding is invalid") from None


def environment(provider: dict[str, Any]) -> dict[str, str]:
  """Return credentials only in memory for a provider-scoped child or request."""
  if provider.get("bridge_account"):
    return bridge(provider)[1]
  token = os.environ.get(provider.get("api_key_env", ""))
  if not token:
    raise ValueError("Provider credential environment variable is not set")
  return {"ANTHROPIC_BASE_URL": provider["base_url"], "ANTHROPIC_AUTH_TOKEN": token}


def present(provider: dict[str, Any]) -> bool:
  """Report availability without exposing the stored value."""
  try:
    environment(provider)
    return True
  except ValueError:
    return False


def redact(text: str, env: dict[str, str]) -> str:
  """Remove credential values if a backend happens to echo them in output/errors."""
  for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    if env.get(name):
      text = text.replace(env[name], "[REDACTED]")
  return text


def redact_data(value: Any, env: dict[str, str]) -> Any:
  """Sanitize structured metadata before it reaches the state store or MCP output."""
  if isinstance(value, str):
    return redact(value, env)
  if isinstance(value, list):
    return [redact_data(item, env) for item in value]
  if isinstance(value, dict):
    return {redact(str(key), env): redact_data(item, env) for key, item in value.items()}
  return value


class NoRedirect(HTTPRedirectHandler):
  """Do not forward account credentials through redirects."""

  def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
    return None


def get_json(provider: dict[str, Any], url: str) -> dict[str, Any]:
  """Read same-origin metadata with the selected account and its routing header."""
  base, target = urlsplit(provider["base_url"]), urlsplit(url)
  if ((base.scheme, base.netloc) != (target.scheme, target.netloc) or target.username or target.password
      or target.query or target.fragment):
    raise ValueError("Provider metadata URL must match the credential's pinned origin")
  env = environment(provider)
  token = env.get("ANTHROPIC_API_KEY") or env["ANTHROPIC_AUTH_TOKEN"]
  headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
  for line in env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
    key, sep, value = line.partition(":")
    if sep and key.lower() == "x-provider":
      headers[key] = value.strip()
  try:
    with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=15) as response:
      raw = response.read(262145)
      if len(raw) > 262144:
        raise ValueError("Provider metadata response is too large")
      value = json.loads(raw)
      if not isinstance(value, dict):
        raise ValueError("Invalid provider metadata response")
      return redact_data(value, env)
  except HTTPError as exc:
    raise ValueError("Provider metadata request failed (HTTP " + str(exc.code) + ")") from None
  except (URLError, TimeoutError, OSError):
    raise ValueError("Provider metadata request could not complete") from None

"""Bounded, credential-redacted stderr capture for owned Codex processes."""
from pathlib import Path
from typing import Any, BinaryIO
import json
import os
import re
import stat
import tempfile
import threading
import shlex


def secrets(env: dict[str, str]) -> list[str]:
  """Collect known secret values in memory; never expose auth data in diagnostics."""
  values = [v for k, v in env.items() if v and re.search(r"TOKEN|KEY|SECRET|PASSWORD|HEADERS", k, re.I)]
  path = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
  try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
      info = os.fstat(source.fileno())
      if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 131072:
        raise ValueError("Unsupported diagnostic credential source")
      raw = source.read(131073)
      if len(raw) > 131072:
        raise ValueError("Diagnostic credential source exceeded bound")
      def walk(value: Any) -> None:
        if isinstance(value, dict):
          for k, v in value.items():
            if isinstance(v, str) and v and re.search(r"token|key|secret|password", k, re.I):
              values.append(v)
            elif isinstance(v, (dict, list)):
              walk(v)
        elif isinstance(value, list):
          for item in value:
            walk(item)
      walk(json.loads(raw))
  except (OSError, ValueError):
    pass
  return sorted(set(values), key=len, reverse=True)


def redact(text: str, values: list[str]) -> str:
  """Redact literal/escaped credentials and recognizable authorization patterns."""
  for value in values:
    for encoded in (value, json.dumps(value)[1:-1]):
      text = text.replace(encoded, "[REDACTED]")
  text = re.sub(r"(?i)Bearer\s+[^\s\"',;]+", "Bearer [REDACTED]", text)
  text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s]+@", r"\1[REDACTED]@", text)
  return re.sub(r"\bsk-[a-zA-Z0-9_-]+", "[REDACTED]", text)


def redact_fields(value: Any) -> Any:
  """Remove labeled secrets even if they were refreshed after process startup."""
  if isinstance(value, dict):
    return {k: "[REDACTED]" if re.search(r"token|api.?key|secret|password|authorization|cookie", k, re.I)
            else redact_fields(v) for k, v in value.items()}
  if isinstance(value, list):
    return [redact_fields(v) for v in value]
  return value


def command_line(arguments: list[str], values: list[str], depth: int = 0) -> dict[str, Any]:
  """Bound and redact argv without reading child environments or executing a shell."""
  arguments = list(arguments)
  if arguments and Path(arguments[0]).name in {"bash", "sh", "zsh", "dash", "ksh"}:
    for index, argument in enumerate(arguments[:-1]):
      if argument not in {"-c", "-lc", "-cl"}:
        continue
      body = arguments[index+1]
      try:
        if depth >= 2 or any(marker in body for marker in ("$", "`", "<<", "(", ")", "{", "}", "\n", "\r")):
          raise ValueError("Complex shell body")
        lexer = shlex.shlex(body, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
        if len(tokens) > 256 or "eval" in tokens or (tokens and tokens[0] in {"if", "for", "while", "case", "{"}):
          raise ValueError("Complex shell body")
        rendered, group = [], []
        for token in tokens + [";"]:
          if token and set(token) <= set(";&|"):
            if group:
              rendered.append(command_line(group, values, depth+1)["command_line"])
              group = []
            rendered.append(token)
          else:
            group.append(token)
        arguments[index+1] = " ".join(rendered[:-1])
      except ValueError:
        arguments[index+1] = "[complex shell body omitted]"
      break
  result = []
  hide_next = False
  index = 0
  while index < len(arguments):
    argument = arguments[index]
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argument) or Path(argument).name in {"env", "exec", "command", "nohup"}:
      index += 1
      continue
    break
  executable = Path(arguments[index]).name if index < len(arguments) else ""
  for offset, argument in enumerate(arguments):
    if hide_next:
      result.append("[REDACTED]")
      hide_next = False
      continue
    flag, equals, _ = argument.partition("=")
    sensitive = bool(re.search(r"api[-_]?key|token|password|secret|authorization|cookie|headers?", flag, re.I))
    colon_auth = (argument in {"-u", "-U", "--user", "--proxy-user"} and offset + 1 < len(arguments)
                  and ":" in arguments[offset+1])
    if executable in {"mysql", "mariadb", "sshpass"} and argument.startswith("-p") and len(argument) > 2:
      result.append("-p[REDACTED]")
      continue
    if (executable == "curl" or ":" in argument) and argument.startswith(("-u", "-U")) and len(argument) > 2:
      result.append(argument[:2] + "[REDACTED]")
      continue
    basic_auth = executable in {"curl", "wget"} and flag in {"-u", "--user", "-U", "--proxy-user", "--http-user", "--http-password"}
    password_arg = executable == "sshpass" and flag == "-p"
    if argument == "-H" or basic_auth or colon_auth or password_arg or (argument.startswith("-") and sensitive):
      result.append(flag + "=[REDACTED]" if equals else argument)
      hide_next = not bool(equals)
      continue
    safe = redact(argument, values)
    safe = re.sub(r"(?is)((?:api[-_]?key|(?:(?:access|refresh|auth|id)[-_]?)?token|password|secret|authorization|cookie|headers?)[\"'\s:=]+).+",
                  r"\1[REDACTED]", safe)
    result.append(safe)
  bounded = []
  budget = 2048
  for argument in result:
    if budget <= 0:
      break
    bounded.append(argument[:budget])
    budget -= len(argument) + 1
  rendered = shlex.join(bounded)
  return {"argv": bounded, "command_line": rendered[:2048],
          "arguments_truncated": len(bounded) < len(result) or len(rendered) > 2048 or budget < 0}


class StderrCapture:
  """Drain stderr continuously into a bounded ring; only sanitized text reaches disk."""

  def __init__(self, stream: BinaryIO, values: list[str], path: Path | None) -> None:
    self.stream, self.path = stream, path
    self.values = values
    self.buffer = bytearray()
    self.truncated = False
    self.lock = threading.Lock()
    self.reader = threading.Thread(target=self._read, daemon=True)
    self.reader.start()

  def _read(self) -> None:
    try:
      while chunk := os.read(self.stream.fileno(), 4096):
        with self.lock:
          self.buffer.extend(chunk)
          self.truncated = self.truncated or len(self.buffer) > 65536
          del self.buffer[:-65536]
    except OSError:
      pass

  def text(self) -> str:
    """Return at most 64 KiB of sanitized recent stderr."""
    with self.lock:
      raw = bytes(self.buffer).decode("utf-8", errors="replace")
      truncated = self.truncated
    if truncated:
      # The first partial line can contain an unmatched fragment of a credential.
      raw = "[older stderr omitted]\n" + raw.partition("\n")[2]
    lines = []
    for line in raw.splitlines():
      try:
        line = json.dumps(redact_fields(json.loads(line)), ensure_ascii=False)
      except ValueError:
        pass
      lines.append(redact(line, self.values))
    return "\n".join(lines).encode("utf-8")[-65536:].decode("utf-8", errors="ignore")

  def save(self) -> None:
    """Persist only redacted output using an owner-only file descriptor."""
    if self.path:
      fd, temp = tempfile.mkstemp(prefix=".stderr-", dir=self.path.parent)
      try:
        with os.fdopen(fd, "w") as output:
          output.write(self.text())
        os.replace(temp, self.path)
      finally:
        if os.path.exists(temp):
          os.unlink(temp)

  def close(self) -> None:
    """Reap the stderr reader without hanging on escaped descendants."""
    self.reader.join(timeout=1)
    self.save()
    if not self.reader.is_alive():
      self.stream.close()

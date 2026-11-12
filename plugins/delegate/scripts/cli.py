"""Discoverable parent CLI; explicit protocols remain separate from the MCP stdio stream."""
from pathlib import Path
from typing import Any, Callable
import argparse
import json
import os
import stat
import sys

import completion
import hosts
import persistent_tasks
import result_delivery
import runtime_image
import storage as st

HELP = '''Current CLI fallback for old MCP connections. Keep the ORIGINAL session_id.

JSON protocol (normal delegate tool names and arguments):
  delegate-agent call <<'JSON'
  {"name":"delegate_job","arguments":{"session_id":"SID","action":"result","job_id":"JOB"}}
  JSON
  delegate-agent call --input request.json
  delegate-agent call codex < request.json  # native parent; dsh also supported

Full report, no report-body echo or private job-JSON parsing:
  delegate-agent result JOB --session-id SID --output /tmp/review.md
  delegate-agent verify JOB --session-id SID --verdict accepted --evidence "Checked the complete report"

Dirty source builder: use source_inputs=["README.md","doc/PLAN.md"] inside
delegate_run arguments, plus operation="write". These are exact repo-root-relative
drafts; the source HEAD/index/files stay untouched. Read-only analysis can return text.

Installing this CLI does NOT replace an already-connected MCP server. Use this
current CLI for ALL calls in the old session, or reload/start a current session.
'''


def read_text(name: str | None, maximum: int = 1048576, label: str = "JSON") -> str:
  """Read bounded stdin or a regular file; empty input never produces a JSON traceback."""
  if name is None or name == "-":
    if name is None and sys.stdin.isatty():
      raise ValueError("No JSON input. Use a quoted heredoc or --input request.json; see call --help")
    text = sys.stdin.read(maximum + 1)
  else:
    fd = os.open(Path(name).expanduser(), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
      if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise ValueError("Input must be a regular text file")
      text = stream.read(maximum + 1)
  if len(text) > maximum:
    raise ValueError("CLI input exceeds its bounded text limit")
  if not text.strip():
    if label != "JSON":
      raise ValueError(label + " input is empty")
    raise ValueError('No JSON input. Expected {"name":"delegate_job","arguments":{"session_id":"SID","action":"result","job_id":"JOB"}}; see call --help')
  return text


def payload(name: str | None) -> dict[str, Any]:
  """Validate the documented envelope before any dispatch side effects."""
  try:
    value = json.loads(read_text(name))
  except json.JSONDecodeError as exc:
    raise ValueError(f"Invalid JSON input at line {exc.lineno}, column {exc.colno}; see call --help") from None
  if not isinstance(value, dict):
    raise ValueError("CLI input must be a JSON object")
  return value


def set_host(host: str | None) -> None:
  """Preserve host namespace checking for both legacy and convenience commands."""
  if host:
    os.environ["DELEGATE_CALLER_HOST"] = host


def guide_path() -> str:
  """Locate only the current installed package's bundled guide; never scan home directories."""
  candidates = [Path(__file__).resolve().parent.parent / "skills/delegate/references/cli.md"]
  if candidates[0].is_file():
    return str(candidates[0])
  try:
    registry = Path.home() / ".claude/plugins/installed_plugins.json"
    fd = os.open(registry, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
      info = os.fstat(stream.fileno())
      if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
        return "Built-in: delegate-agent call --help"
      raw = stream.read(1048577)
    if len(raw) <= 1048576:
      value = json.loads(raw)
      plugins = value.get("plugins", {}) if isinstance(value, dict) else {}
      if isinstance(plugins, dict):
        for index, (name, entries) in enumerate(plugins.items()):
          if index >= 64 or len(candidates) >= 8:
            break
          if name.split("@")[0] == "delegate" and isinstance(entries, list):
            for entry in entries[:16]:
              if (isinstance(entry, dict) and entry.get("version") == runtime_image.VERSION
                  and isinstance(entry.get("installPath"), str) and 0 < len(entry["installPath"]) <= 4096):
                candidates.append(Path(entry["installPath"]) / "skills/delegate/references/cli.md")
                if len(candidates) >= 8:
                  break
  except (OSError, ValueError, TypeError, KeyError):
    pass
  for path in candidates:
    try:
      if path.is_file():
        return str(path)
    except (OSError, ValueError):
      pass
  return "Built-in: delegate-agent call --help"


def parser() -> argparse.ArgumentParser:
  """Build the small user CLI without exposing internal worker/controller modes."""
  root = argparse.ArgumentParser(prog="delegate-agent", description="Quota-aware delegation CLI", epilog=HELP,
    formatter_class=argparse.RawDescriptionHelpFormatter)
  root.add_argument("--version", action="version", version="delegate-agent " + runtime_image.VERSION)
  commands = root.add_subparsers(dest="command")
  for name in ("call", "wait", "loop-wait"):
    sub = commands.add_parser(name, help="JSON request on stdin or --input", epilog=HELP,
      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub.add_argument("host", nargs="?", choices=["codex", "dsh"])
    sub.add_argument("--input", metavar="FILE", help="JSON request file; - reads stdin")
  for name in ("result", "verify", "status"):
    sub = commands.add_parser(name, help={"result":"Read/export a report", "verify":"Record verdict without report echo", "status":"Inspect metadata without report text"}[name])
    sub.add_argument("job_id", nargs="?" if name == "status" else None)
    sub.add_argument("--session-id", required=True)
    sub.add_argument("--host", choices=["claude", "codex", "dsh"])
    if name == "result":
      sub.add_argument("--view", choices=["auto", "page", "summary", "file"], default="auto", help="auto suppresses repeats; file exports a full private report")
      sub.add_argument("--offset", type=int, default=0)
      sub.add_argument("--limit", type=int, default=result_delivery.DEFAULT_LIMIT)
      sub.add_argument("--sha256", default="")
      group = sub.add_mutually_exclusive_group()
      group.add_argument("--output", metavar="FILE", help="Export the FULL UTF-8 report; print only an acknowledgement")
      group.add_argument("--all", action="store_true", help="Explicit full text on stdout; host display limits may still apply")
      sub.add_argument("--force", action="store_true", help="Explicitly replace an existing export file")
    if name == "verify":
      sub.add_argument("--verdict", choices=["accepted", "needs-work", "blocked"], required=True)
      group = sub.add_mutually_exclusive_group(required=True)
      group.add_argument("--evidence")
      group.add_argument("--evidence-file")
  sub = commands.add_parser("doctor", help="Inspect this CLI's version and capabilities; no provider probes")
  sub.add_argument("--session-id")
  sub.add_argument("--host", choices=["claude", "codex", "dsh"])
  sub = commands.add_parser("schema", help="Show the exact current tool input schema")
  sub.add_argument("tool", nargs="?")
  return root


def main(argv: list[str], dispatch: Callable[[str, dict[str, Any]], Any],
         tool_list: Callable[[], list[dict[str, Any]]]) -> int:
  """Run user commands with clear errors; never turn help/empty input into an MCP stream."""
  root = parser()
  if not argv or argv == ["help"]:
    root.print_help()
    return 0
  args = root.parse_args(argv)
  try:
    set_host(getattr(args, "host", None))
    if args.command in {"call", "wait", "loop-wait"}:
      value = payload(args.input)
      if args.command == "call":
        if not isinstance(value.get("name"), str) or not isinstance(value.get("arguments", {}), dict) or set(value) - {"name", "arguments"}:
          raise ValueError('call expects {"name":"TOOL","arguments":{...}}; see call --help')
        result = dispatch(value["name"], value.get("arguments", {}))
      else:
        hosts.check(value)
        result = completion.wait(value) if args.command == "wait" else persistent_tasks.wait(value)
    elif args.command == "schema":
      rows = tool_list()
      result = next((t for t in rows if t["name"] == args.tool), None) if args.tool else rows
      if result is None:
        raise ValueError("Unknown tool; run schema to list current names")
    elif args.command == "doctor":
      if args.session_id:
        hosts.check({"session_id": args.session_id})
      result = {"cli_version": runtime_image.VERSION, "scripts": str(Path(__file__).resolve().parent),
        "connected_mcp_version": "unknown: check the CONNECTED MCP delegate_control status server_runtime.version",
        "capabilities": ["source_inputs", "required_files", "task_profile", "paged_results", "compact_verify", "full_report_export", "persistent_tasks"],
        "input_format": {"name": "delegate_job", "arguments": {"session_id": args.session_id or "SID", "action": "result", "job_id": "JOB"}},
        "help": "delegate-agent call --help; delegate-agent result --help; delegate-agent schema delegate_run",
        "documentation": guide_path()}
    elif args.command in {"status", "verify", "result"}:
      arguments = {"session_id": args.session_id, "job_id": args.job_id, "action": args.command}
      if args.command == "status" and not args.job_id:
        result = dispatch("delegate_control", {"session_id": args.session_id, "action": "status"})
      elif args.command == "verify":
        evidence = read_text(args.evidence_file, 65536, "Evidence") if args.evidence_file is not None else args.evidence
        result = dispatch("delegate_job", {**arguments, "verdict": args.verdict, "evidence": evidence})
      elif args.command == "result" and (args.output is not None or args.all):
        if args.output is not None and not args.output.strip():
          raise ValueError("--output requires a nonempty report destination; no report was collected")
        hosts.check({"session_id": args.session_id})
        if args.offset or args.sha256 or args.force and not args.output:
          raise ValueError("Full export uses the entire current result; no paging args, and --force requires --output")
        if args.all:
          from lifecycle import ACTIVE, reconcile
          job = reconcile(args.job_id)
          if job["session_id"] != args.session_id:
            raise ValueError("Job belongs to another session")
          if job["status"] in ACTIVE - {"orphaned"}:
            raise ValueError("Job is still active; use wait before requesting its full terminal report")
          text = job.get("result") or ""
          sys.stdout.write(text)
          sys.stdout.flush()
          if not result_delivery.acknowledge(job, result_delivery.fingerprint(text), 0, len(text), len(text), source="CLI explicit full stdout delivery"):
            raise ValueError("A full snapshot was printed, but the current result changed; fetch its new version")
          return 0
        result = result_delivery.export(args.session_id, args.job_id, args.output, args.force)
      else:
        if args.command == "result":
          if args.force:
            raise ValueError("--force requires --output")
          arguments.update(result_view=args.view, result_offset=args.offset, result_limit=args.limit, result_sha256=args.sha256)
        result = dispatch("delegate_job", arguments)
    else:
      root.print_help()
      return 0
    print(result_delivery.serialize(result))
    return 0
  except Exception as exc:
    print(json.dumps({"error": str(exc), "type": type(exc).__name__, "cli_version": runtime_image.VERSION,
      "hint": "delegate-agent --help or delegate-agent call --help; keep the original session_id"}, ensure_ascii=False), file=sys.stderr)
    return 2

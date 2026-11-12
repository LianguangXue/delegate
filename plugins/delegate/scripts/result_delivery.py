"""Valid bounded result pages with complete-delivery bookkeeping, separate from acceptance."""
from typing import Any
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

import storage as st
import runtime_image
from lifecycle import ACTIVE

DEFAULT_LIMIT = 24000
MAX_LIMIT = 32000
WIRE_LIMIT = 32000  # UTF-8 JSON bytes, including metadata/escaping rather than only text characters.


def serialize(value: Any) -> str:
  """Require strict JSON before any result is recorded as deliverable."""
  return json.dumps(value, ensure_ascii=False, allow_nan=False)


def fingerprint(text: str) -> str:
  """Identify the exact text version, including Unicode, across result pages."""
  return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate(offset: int, limit: int, expected: str) -> None:
  """Reject invalid paging arguments before a result call can wait or mark collection."""
  if type(offset) is not int or offset < 0:
    raise ValueError("result_offset must be a non-negative character offset")
  if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
    raise ValueError(f"result_limit must be 1..{MAX_LIMIT} characters (default {DEFAULT_LIMIT})")
  if not isinstance(expected, str) or (expected and (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected))):
    raise ValueError("result_sha256 must be the fingerprint returned by a previous result page")
  if offset and not expected:
    raise ValueError("Pass result_sha256 from the previous page when result_offset is nonzero")


def next_call(job: dict[str, Any], offset: int = 0, expected: str = "", limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
  """Return a ready-to-use tool request rather than requiring private-record reads."""
  arguments = {"session_id": job["session_id"], "action": "result", "job_id": job["id"],
               "result_offset": offset, "result_limit": limit, "result_view": "page"}
  if expected:
    arguments["result_sha256"] = expected
  return {"name": "delegate_job", "arguments": arguments}


def proof(job: dict[str, Any]) -> tuple[str, list[list[int]], bool]:
  """Use coverage only for the current text, never a stale legacy collection flag."""
  text = job.get("result") or ""
  digest = fingerprint(text)
  delivery = job.get("result_delivery") or {}
  ranges = delivery.get("ranges", []) if delivery.get("sha256") == digest else []
  complete = delivery.get("complete") is True and ranges == [[0, len(text)]]
  return digest, ranges, complete


def receipt(job: dict[str, Any], unchanged: bool = False) -> dict[str, Any]:
  """Return a small acknowledgement or continuation handle, never repeat the report body."""
  text = job.get("result") or ""
  digest, ranges, complete = proof(job)
  offset = 0
  for lo, hi in sorted(ranges):
    if lo > offset:
      break
    offset = max(offset, hi)
  result = {k: job[k] for k in ("id", "session_id", "status", "agent", "provider_name", "model", "effort", "review",
    "codex_session", "cli_session") if k in job}
  result.update(result_view="receipt", result_text_included=False, result_unchanged=unchanged,
    result_total_chars=len(text), result_sha256=digest, result_delivery_complete=complete,
    next_result_call=next_call(job, offset if offset < len(text) else 0, digest) if not complete else None,
    full_result_call={"name": "delegate_job", "arguments": {"session_id": job["session_id"], "job_id": job["id"], "action": "result", "result_view": "file"}},
    reread_result_call=next_call(job, 0, digest), job_runtime_version=runtime_image.job_version(job),
    server_runtime_version=runtime_image.VERSION, record_path=str(st.root() / ("job-" + job["id"] + ".json")))
  if job.get("review_evidence"):
    evidence = str(job["review_evidence"])
    result.update(review_evidence=evidence[:1024], review_evidence_truncated=len(evidence) > 1024)
  if job.get("error"):
    result["error"] = str(job["error"])[:2000]
  host = st.session(job["session_id"]).get("host_kind", "claude")
  result["export_command"] = ("delegate-agent result " + job["id"] + " --session-id " + job["session_id"] +
    (" --host " + host if host in {"codex", "dsh"} else "") + " --output /tmp/delegate-" + job["id"] + "-" + digest[:12] + ".md")
  if len(serialize(result).encode("utf-8")) > 12000:
    result = {**compact(result), "result_view": "receipt", "result_text_included": False,
      "result_sha256": digest, "result_total_chars": len(text), "result_delivery_complete": complete,
      "next_result_call": result["next_result_call"], "reread_result_call": result["reread_result_call"],
      "full_result_call": result["full_result_call"],
      "export_command": result["export_command"]}
  return result


def compact(value: dict[str, Any]) -> dict[str, Any]:
  """Keep identity/recovery metadata when a detailed record leaves no room for text."""
  fields = {"id", "session_id", "status", "agent", "provider_name", "model", "effort", "review",
            "record_path", "background", "await_completion", "completion_mode", "job_runtime_version", "server_runtime_version"}
  result = {}
  shortened = []
  for key in fields:
    item = value.get(key)
    if key not in value:
      continue
    if isinstance(item, str):
      encoded = item.encode("utf-8")
      result[key] = encoded[:512].decode("utf-8", errors="ignore")
      if len(encoded) > 512:
        shortened.append(key)
    elif item is None or type(item) in {bool, int, float}:
      result[key] = item
    else:
      shortened.append(key)
  omitted = sorted(set(value) - fields - {"result"})
  result["metadata_omitted"] = [k[:64] for k in omitted[:64]]
  result["metadata_omitted_count"] = len(omitted)
  if shortened:
    result["metadata_shortened"] = shortened
  result["metadata_note"] = "Large metadata omitted to keep result text pageable; inspect the job for diagnostics"
  if value.get("error"):
    result["error"] = str(value["error"]).encode("utf-8")[:2000].decode("utf-8", errors="ignore")
  return result


def acknowledge(job: dict[str, Any], digest: str, start: int, end: int, total: int,
                source: str = "complete result tool delivery") -> bool:
  """Collect only a terminal result whose entire current text has been returned by tool calls."""
  if job["status"] in ACTIVE - {"orphaned"}:
    return False
  with st.locked():
    current = st.job(job["id"])
    if current["status"] in ACTIVE - {"orphaned"} or fingerprint(current.get("result") or "") != digest:
      return False
    previous = current.get("result_delivery") or {}
    ranges = previous.get("ranges", []) if previous.get("sha256") == digest else []
    ranges = sorted([*ranges, [start, end]])
    merged = []
    for lo, hi in ranges:
      if merged and lo <= merged[-1][1]:
        merged[-1][1] = max(merged[-1][1], hi)
      else:
        merged.append([lo, hi])
    if len(merged) > 256:
      raise ValueError("Too many disjoint result pages; collect sequentially from result_offset=0")
    complete = bool(merged and merged[0] == [0, total])
    current["result_delivery"] = {"sha256": digest, "total_chars": total, "ranges": merged,
                                  "complete": complete, "last_page_at": time.time()}
    if complete:
      current.update(collected_at=time.time(), collection_source=source)
    elif previous.get("sha256") != digest:
      current.pop("collected_at", None)
    st.write("job-" + current["id"] + ".json", current)
  return complete


def page(job: dict[str, Any], value: dict[str, Any], offset: int, limit: int, expected: str) -> dict[str, Any]:
  """Fit valid JSON on the wire, preserving every remaining character through explicit continuation."""
  validate(offset, limit, expected)
  text = job.get("result") or ""
  digest = fingerprint(text)
  if expected and expected != digest:
    raise ValueError("Result text changed; restart at result_offset=0 and use its new result_sha256")
  if offset > len(text):
    raise ValueError(f"result_offset exceeds this result's length ({len(text)} characters)")
  metadata = {k: v for k, v in value.items() if k != "result"}
  if len(serialize(metadata).encode("utf-8")) > 12000:
    metadata = compact(metadata)
  full_call = {"name": "delegate_job", "arguments": {"session_id": job["session_id"], "job_id": job["id"],
    "action": "result", "result_view": "file"}}
  def render(end: int) -> dict[str, Any]:
    more = end < len(text)
    return {**metadata, "result": text[offset:end], "result_offset": offset, "result_chars": end - offset,
      "result_view": "page", "result_text_included": True,
      "result_total_chars": len(text), "result_sha256": digest, "result_truncated": more,
      "next_result_offset": end if more else None, "result_delivery_complete": False,
      "next_result_call": next_call(job, end, digest, limit) if more else None, "full_result_call": full_call}
  # JSON escaping can expand quotes/control characters; budget the serialized page.
  low, high = offset, min(len(text), offset + limit)
  while low < high:
    middle = (low + high + 1) // 2
    if len(serialize(render(middle)).encode("utf-8")) <= WIRE_LIMIT - 1024:
      low = middle
    else:
      high = middle - 1
  if low == offset and offset < len(text):
    raise ValueError("Result metadata exceeds the transport budget; use compact result delivery")
  result = render(low)
  if len(serialize(result).encode("utf-8")) > WIRE_LIMIT - 1024:
    raise ValueError("Result metadata exceeds the transport budget; no collection was recorded")
  result["result_delivery_complete"] = acknowledge(job, digest, offset, low, len(text))
  if not result["result_delivery_complete"] and low == len(text) and job["status"] not in ACTIVE - {"orphaned"}:
    result["next_result_call"] = next_call(job, 0, digest, limit)
    result["collection_note"] = "Earlier text has not been fully delivered; collect missing pages before acceptance"
  return result


def export(session_id: str, job_id: str, destination: str, force: bool = False) -> dict[str, Any]:
  """Publish the exact full report as a parent-requested artifact before marking delivery."""
  from lifecycle import reconcile
  job = reconcile(job_id)
  if job["session_id"] != session_id:
    raise ValueError("Job belongs to another session")
  if job["status"] in ACTIVE - {"orphaned"}:
    raise ValueError("Job is still active; use the managed waiter before exporting its terminal report")
  text = job.get("result") or ""
  digest = fingerprint(text)
  target = Path(destination).expanduser().absolute()
  if target.is_symlink():
    raise ValueError("Refusing to overwrite a symlink report destination")
  reused = False
  if target.exists():
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as existing:
      info = os.fstat(existing.fileno())
      if not stat.S_ISREG(info.st_mode):
        raise ValueError("Report destination must be a regular file")
      if info.st_uid == os.getuid() and info.st_size == len(text.encode("utf-8")):
        value = hashlib.sha256()
        while chunk := existing.read(65536):
          value.update(chunk)
        reused = value.hexdigest() == digest
    if not reused and not force:
      raise FileExistsError("Report destination exists; choose another path or explicitly pass --force")
  target.parent.mkdir(parents=True, exist_ok=True)
  temp = None
  try:
    if not reused:
      fd, temp = tempfile.mkstemp(prefix=".delegate-report-", dir=target.parent)
      with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
      # No completion is recorded if publication fails (including an existing target).
      if force:
        os.replace(temp, target)
      else:
        os.link(temp, target)
    complete = acknowledge(job, digest, 0, len(text), len(text), source="full report artifact export")
  finally:
    if temp and os.path.exists(temp):
      os.unlink(temp)
  return {"id": job_id, "session_id": session_id, "status": job["status"], "review": job.get("review"),
    "result_file": str(target), "result_chars": len(text), "result_sha256": digest,
    "result_delivery_complete": complete, "result_text_included": False, "result_file_reused": reused,
    "next_result_call": None if complete else next_call(st.job(job_id)),
    "note": "Full report exported, not accepted; read the artifact and verify evidence before acceptance"}


def artifact(session_id: str, job_id: str) -> dict[str, Any]:
  """Export to a private immutable report artifact without granting arbitrary MCP writes."""
  job = st.job(job_id)
  if job["session_id"] != session_id:
    raise ValueError("Job belongs to another session")
  directory = st.root() / "reports"
  if directory.is_symlink():
    raise ValueError("Report artifact directory must not be a symlink")
  path = directory / (st.key(job_id) + "-" + fingerprint(job.get("result") or "") + ".md")
  return export(session_id, job_id, str(path))


def wire(value: Any) -> str:
  """Never emit sliced, invalid JSON when another tool returns oversized diagnostic metadata."""
  rendered = serialize(value)
  if len(rendered.encode("utf-8")) <= WIRE_LIMIT:
    return rendered
  fallback = compact(value) if isinstance(value, dict) else {"items": len(value) if isinstance(value, list) else None}
  fallback.update(response_truncated=True,
    message="Diagnostic response exceeds transport budget; request a narrower scope. Job result text uses paginated action=result.")
  rendered = serialize(fallback)
  if len(rendered.encode("utf-8")) > WIRE_LIMIT:
    return serialize({"response_truncated": True, "message": "Response metadata exceeds transport budget; request a specific job result page"})
  return rendered

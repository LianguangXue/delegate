"""CLI migration must be discoverable and report delivery must not waste or hide text."""
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import io
import os
import subprocess
import sys
import unittest

from test_result_notifications import DeliveryFixture, SCRIPTS
import delegate
import result_delivery
import rewake
import service
import storage as st
import cli
import runtime_image


class CliDeliveryTests(DeliveryFixture):
  def cli(self, *args: str, text: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "delegate.py"), "cli", *args], input=text,
      capture_output=True, text=True, timeout=15)

  def test_bare_help_version_and_empty_call_never_parse_missing_json_as_a_traceback(self) -> None:
    for args in ((), ("--help",), ("call", "--help"), ("result", "--help"), ("--version",)):
      value = self.cli(*args)
      self.assertEqual(value.returncode, 0, value.stderr)
      self.assertNotIn("Traceback", value.stderr)
      self.assertTrue(value.stdout)
    value = self.cli("call")
    self.assertEqual(value.returncode, 2)
    self.assertIn("No JSON input", json.loads(value.stderr)["error"])
    self.assertNotIn("Traceback", value.stderr)
    value = self.cli("call", text="not-json")
    self.assertEqual(value.returncode, 2)
    self.assertIn("Invalid JSON", json.loads(value.stderr)["error"])

  def test_envelope_file_and_doctor_do_not_need_provider_requests(self) -> None:
    request = Path(self.tmp.name) / "request.json"
    request.write_text(json.dumps({"name": "delegate_job", "arguments": {
      "session_id": "s", "action": "status", "job_id": "jfixture"}}))
    result = self.cli("call", "--input", str(request))
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(json.loads(result.stdout)["id"], "jfixture")
    doctor = self.cli("doctor", "--session-id", "s")
    self.assertEqual(doctor.returncode, 0, doctor.stderr)
    self.assertIn("unknown", json.loads(doctor.stdout)["connected_mcp_version"])
    schema = self.cli("schema", "delegate_run")
    self.assertIn("source_inputs", json.loads(schema.stdout)["inputSchema"]["properties"])

  def test_explicit_mcp_and_legacy_bare_script_keep_json_rpc_streams(self) -> None:
    request = json.dumps({"id": 1, "method": "tools/list"}) + "\n"
    for args in ([], ["mcp"], ["cli", "host-mcp", "codex"]):
      proc = subprocess.run([sys.executable, str(SCRIPTS / "delegate.py"), *args], input=request,
        capture_output=True, text=True, timeout=15, check=True)
      self.assertEqual(json.loads(proc.stdout)["id"], 1)

  def test_mcp_initialize_matches_the_canonical_runtime_version(self) -> None:
    request = json.dumps({"id": 1, "method": "initialize", "params": {}}) + "\n"
    for args in ([], ["mcp"], ["cli", "host-mcp", "codex"]):
      proc = subprocess.run([sys.executable, str(SCRIPTS / "delegate.py"), *args], input=request,
        capture_output=True, text=True, timeout=15, check=True)
      self.assertEqual(json.loads(proc.stdout)["result"]["serverInfo"]["version"], runtime_image.VERSION)

  def test_optional_guide_lookup_handles_fifo_and_malformed_registry(self) -> None:
    home = Path(self.tmp.name) / "home"
    registry = home / ".claude/plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True)
    with patch("cli.Path.home", return_value=home), patch.object(cli, "__file__", str(home / "frozen/cli.py")):
      for value in (None, [], {"plugins": None}, {"plugins": {"delegate@x": [None, "bad", {}]}}):
        registry.write_text(json.dumps(value))
        self.assertIn("Built-in", cli.guide_path())
      registry.unlink()
      os.mkfifo(registry)
      self.assertIn("Built-in", cli.guide_path())

  def test_verify_receipt_never_echoes_the_report_or_huge_evidence(self) -> None:
    text = "Finding evidence\n" * 3000
    st.update_job("jfixture", result=text)
    value = service.jobs("s", "verify", "jfixture", verdict="accepted", evidence="Checked\n" * 3000)
    self.assertNotIn("result", value)
    self.assertFalse(value["result_text_included"])
    self.assertLess(len(json.dumps(value)), 6000)
    self.assertEqual(st.job("jfixture")["result"], text)
    self.assertTrue(value["review_evidence_truncated"])

  def test_repeat_auto_returns_continuation_or_reread_handle_without_text(self) -> None:
    text = "item\n" * 10000
    st.update_job("jfixture", result=text)
    first = service.jobs("s", "result", "jfixture", result_limit=1000)
    self.assertEqual(first["result"], text[:1000])
    repeated = service.jobs("s", "result", "jfixture")
    self.assertNotIn("result", repeated)
    self.assertFalse(repeated["result_delivery_complete"])
    self.assertEqual(repeated["next_result_call"]["arguments"]["result_offset"], 1000)
    reread = service.jobs(**repeated["reread_result_call"]["arguments"])
    self.assertEqual(reread["result_offset"], 0)
    self.assertIn("result", reread)
    result_delivery.acknowledge(st.job("jfixture"), result_delivery.fingerprint(text), 0, len(text), len(text))
    complete = service.jobs("s", "result", "jfixture")
    self.assertNotIn("result", complete)
    self.assertTrue(complete["result_unchanged"])
    self.assertTrue(complete["result_delivery_complete"])

  def test_complete_unicode_export_has_no_body_echo_and_is_idempotent(self) -> None:
    text = "\n".join(f"Finding {i}: " + "完整证据\n" * 1000 for i in range(1, 13))
    st.update_job("jfixture", result=text)
    destination = Path(self.tmp.name) / "review.md"
    result = self.cli("result", "jfixture", "--session-id", "s", "--output", str(destination))
    self.assertEqual(result.returncode, 0, result.stderr)
    value = json.loads(result.stdout)
    self.assertEqual(destination.read_text(), text)
    self.assertEqual(value["result_sha256"], hashlib.sha256(text.encode()).hexdigest())
    self.assertTrue(value["result_delivery_complete"])
    self.assertNotIn("Finding 12", result.stdout)
    self.assertEqual(st.job("jfixture")["review"], "pending")
    again = self.cli("result", "jfixture", "--session-id", "s", "--output", str(destination))
    self.assertTrue(json.loads(again.stdout)["result_file_reused"])
    ack = self.cli("verify", "jfixture", "--session-id", "s", "--verdict", "accepted", "--evidence", "Full artifact inspected")
    self.assertEqual(ack.returncode, 0, ack.stderr)
    self.assertNotIn("result", json.loads(ack.stdout))

  def test_failed_export_does_not_collect_or_clobber_existing_file(self) -> None:
    empty = self.cli("result", "jfixture", "--session-id", "s", "--output", "")
    self.assertEqual(empty.returncode, 2)
    self.assertEqual(empty.stdout, "")
    self.assertIsNone(st.job("jfixture").get("collected_at"))
    with self.assertRaisesRegex(ValueError, "another session"):
      result_delivery.export("other", "jfixture", str(Path(self.tmp.name) / "foreign.md"))
    self.assertFalse((Path(self.tmp.name) / "foreign.md").exists())

    destination = Path(self.tmp.name) / "existing.md"
    destination.write_text("preserve original")
    result = self.cli("result", "jfixture", "--session-id", "s", "--output", str(destination))
    self.assertEqual(result.returncode, 2)
    self.assertEqual(destination.read_text(), "preserve original")
    self.assertIsNone(st.job("jfixture").get("collected_at"))
    with patch("result_delivery.os.link", side_effect=OSError("publication failed")):
      with self.assertRaises(OSError):
        result_delivery.export("s", "jfixture", str(Path(self.tmp.name) / "new.md"))
    self.assertIsNone(st.job("jfixture").get("collected_at"))

  def test_stdout_flush_failure_does_not_mark_a_report_collected(self) -> None:
    from unittest.mock import Mock
    output = Mock()
    output.flush.side_effect = BrokenPipeError("consumer disconnected")
    with patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
      code = cli.main(["result", "jfixture", "--session-id", "s", "--all"], delegate.dispatch, delegate.tool_list)
    self.assertEqual(code, 2)
    self.assertIsNone(st.job("jfixture").get("collected_at"))

  def test_mcp_file_view_returns_whole_report_without_private_job_json(self) -> None:
    text = "tail evidence\n" * 10000
    st.update_job("jfixture", result=text)
    value = service.jobs("s", "result", "jfixture", result_view="file")
    self.assertEqual(Path(value["result_file"]).read_text(), text)
    self.assertTrue(value["result_delivery_complete"])
    self.assertFalse(value["result_text_included"])
    self.assertLess(len(json.dumps(value)), 2000)

  def test_current_first_page_is_never_hidden_by_legacy_compatibility_hook(self) -> None:
    response = service.jobs("s", "result", "jfixture")
    self.assertTrue(response["result_delivery_complete"])
    payload = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "mcp__x__delegate_job",
      "tool_input": {"session_id": "s", "action": "result", "job_id": "jfixture"},
      "tool_response": {"content": [{"type": "text", "text": json.dumps(response)}]}}
    self.assertIsNone(rewake.compact_echo(payload))
    output = delegate.hook(payload)
    self.assertNotIn("updatedMCPToolOutput", output.get("hookSpecificOutput", {}))

  def test_legacy_verify_and_repeat_can_be_compacted_without_changing_the_job(self) -> None:
    text = "evidence\n" * 5000
    st.update_job("jfixture", result=text, review="accepted", review_evidence="Checked")
    result_delivery.acknowledge(st.job("jfixture"), result_delivery.fingerprint(text), 0, len(text), len(text))
    response = {**st.job("jfixture")}
    payload = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "mcp__x__delegate_job",
      "tool_input": {"session_id": "s", "action": "verify", "job_id": "jfixture", "verdict": "accepted"},
      "tool_response": {"content": [{"type": "text", "text": json.dumps(response)}], "structuredContent": response}}
    rewritten = rewake.compact_echo(payload)
    receipt = json.loads(rewritten["content"][0]["text"])
    self.assertNotIn("result", receipt)
    self.assertIn("unknown", receipt["server_runtime_version"])
    self.assertTrue(receipt["formatter_runtime_version"])
    self.assertNotIn("result", rewritten["structuredContent"])
    self.assertEqual(st.job("jfixture")["result"], text)
    result_delivery.acknowledge(st.job("jfixture"), result_delivery.fingerprint(text), 0, len(text), len(text))
    payload["tool_input"]["action"] = "result"
    self.assertIsNotNone(rewake.compact_echo(payload))
    payload["tool_input"]["detail"] = True
    self.assertIsNone(rewake.compact_echo(payload))


if __name__ == "__main__":
  unittest.main()

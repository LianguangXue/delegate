"""Complete review delivery and model-free waiting must agree with Stop and collection."""
from pathlib import Path
from unittest.mock import patch
import json
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import accounting as ac
import completion
import delegate
import lifecycle
import result_delivery
import rewake
import service
import storage as st


class DeliveryFixture(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory(prefix="delegate-results-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.tmp.name, "DELEGATE_CALLER_HOST": "claude"})
    self.env.start()
    self.task = ac.begin("s")
    self.job = {"id": "jfixture", "session_id": "s", "task_id": self.task["id"], "status": "completed",
      "created_at": time.time(), "finished_at": time.time(), "timeout_seconds": 0, "agent": "codex-reviewer",
      "provider": {"kind": "codex"}, "provider_name": "codex", "model": "fixture", "effort": "medium",
      "review": "pending", "await_completion": True, "result": "complete review",
      "worker_identity": lifecycle.identity(os.getpid())}
    st.write("job-jfixture.json", self.job)
    self.task["jobs"] = ["jfixture"]
    ac.save(self.task)
    state = st.session("s")
    state["jobs"] = ["jfixture"]
    st.save_session("s", state)

  def tearDown(self) -> None:
    self.env.stop()
    self.tmp.cleanup()

  def stop(self) -> str:
    return delegate.stop_feedback("s", False)


class ResultPageTests(DeliveryFixture):
  def test_normal_twelve_finding_review_is_complete_without_old_eight_k_cut(self) -> None:
    text = "\n".join(f"Finding {i}: " + "evidence " * 100 for i in range(1, 13))
    st.update_job("jfixture", result=text)
    value = service.jobs("s", "result", "jfixture")
    self.assertEqual(value["result"], text)
    self.assertIn("Finding 12:", value["result"])
    self.assertFalse(value["result_truncated"])
    self.assertTrue(value["result_delivery_complete"])
    self.assertTrue(st.job("jfixture")["collected_at"])

  def test_all_long_pages_are_recoverable_and_partial_does_not_collect(self) -> None:
    text = "\n".join(f"Finding {i}: " + "证据和边界 " * 800 for i in range(1, 13))
    st.update_job("jfixture", result=text)
    args = dict(session_id="s", action="result", job_id="jfixture", result_limit=7000)
    pages = []
    while True:
      value = service.jobs(**args)
      pages.append(value["result"])
      if value["next_result_call"] is None:
        break
      self.assertFalse(value["result_delivery_complete"])
      self.assertIsNone(st.job("jfixture").get("collected_at"))
      if len(pages) == 1:
        self.assertIn("jfixture", self.stop())
      args = value["next_result_call"]["arguments"]
    self.assertEqual("".join(pages), text)
    self.assertGreater(len(pages), 3)
    self.assertTrue(value["result_delivery_complete"])
    self.assertEqual(self.stop(), "")
    self.assertEqual(st.job("jfixture")["review"], "pending")

  def test_detail_result_and_mcp_transport_remain_valid_for_escaped_text_and_huge_metadata(self) -> None:
    text = ('"\\\x00中文\n' * 12000)
    st.update_job("jfixture", result=text, rationale="x" * 70000, messages=[{"text": "y" * 70000}])
    request = {"id": 1, "method": "tools/call", "params": {"name": "delegate_job", "arguments": {
      "session_id": "s", "action": "result", "job_id": "jfixture", "detail": True}}}
    proc = subprocess.run([sys.executable, str(SCRIPTS / "delegate.py"), "mcp"],
      input=json.dumps(request) + "\n", capture_output=True, text=True, timeout=15, check=True)
    response = json.loads(proc.stdout)["result"]
    self.assertFalse(response.get("isError"))
    rendered = response["content"][0]["text"]
    self.assertLessEqual(len(rendered), result_delivery.WIRE_LIMIT)
    self.assertLessEqual(len(rendered.encode("utf-8")), result_delivery.WIRE_LIMIT)
    page = json.loads(rendered)
    self.assertTrue(page["result_truncated"])
    self.assertTrue(page["next_result_call"])
    self.assertIn("rationale", page["metadata_omitted"])
    self.assertIsNone(st.job("jfixture").get("collected_at"))

  def test_gap_and_stale_hash_cannot_be_mistaken_for_full_delivery(self) -> None:
    text = "abcdefghij" * 10
    st.update_job("jfixture", result=text)
    digest = result_delivery.fingerprint(text)
    value = service.jobs("s", "result", "jfixture", result_offset=50, result_sha256=digest)
    self.assertFalse(value["result_delivery_complete"])
    self.assertEqual(value["next_result_call"]["arguments"]["result_offset"], 0)
    value = service.jobs("s", "result", "jfixture", result_limit=50, result_view="page")
    self.assertTrue(value["result_delivery_complete"])
    st.update_job("jfixture", result=text + "new")
    with self.assertRaisesRegex(ValueError, "changed"):
      service.jobs("s", "result", "jfixture", result_offset=50, result_sha256=digest)
    value = service.jobs("s", "result", "jfixture", result_limit=10)
    self.assertFalse(value["result_delivery_complete"])
    self.assertIsNone(st.job("jfixture").get("collected_at"))

  def test_oversized_identity_metadata_cannot_break_wire_or_ack_an_oversized_page(self) -> None:
    rendered = result_delivery.wire({"id": "jfixture", "model": "中文" * 100000})
    self.assertLessEqual(len(rendered.encode("utf-8")), result_delivery.WIRE_LIMIT)
    self.assertIn("model", json.loads(rendered)["metadata_shortened"])
    st.update_job("jfixture", result="", model="中文" * 100000)
    value = service.jobs("s", "result", "jfixture", detail=True)
    self.assertLessEqual(len(json.dumps(value, ensure_ascii=False).encode("utf-8")), result_delivery.WIRE_LIMIT)
    self.assertTrue(value["result_delivery_complete"])

  def test_invalid_paging_does_not_wait_and_legacy_truncated_hook_does_not_ack(self) -> None:
    with patch("service.time.sleep") as sleep:
      with self.assertRaisesRegex(ValueError, "result_sha256"):
        service.jobs("s", "result", "jfixture", result_offset=1, wait_seconds=50)
      sleep.assert_not_called()
    response = {"id": "jfixture", "session_id": "s", "status": "completed", "result": "partial", "result_truncated": True}
    rewake.observe_collection({"session_id": "s", "hook_event_name": "PostToolUse",
      "tool_name": "mcp__x__delegate_job", "tool_input": {"session_id": "s", "action": "result", "job_id": "jfixture"},
      "tool_response": response})
    self.assertIsNone(st.job("jfixture").get("collected_at"))
    self.assertTrue(json.loads(result_delivery.wire({"diagnostics": "x" * 100000}))["response_truncated"])

  def test_nonfinite_metadata_never_records_an_undeliverable_empty_result_as_collected(self) -> None:
    st.update_job("jfixture", result="", usage={"untrusted": float("nan")})
    with self.assertRaises(ValueError):
      service.jobs("s", "result", "jfixture", detail=True)
    self.assertIsNone(st.job("jfixture").get("collected_at"))
    with self.assertRaises(ValueError):
      result_delivery.wire({"bad": float("inf")})

  def test_mixed_old_server_partial_marker_is_revoked_but_full_coverage_is_preserved(self) -> None:
    response = {"id": "jfixture", "session_id": "s", "status": "completed", "result": "partial", "result_truncated": True}
    payload = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "mcp__x__delegate_job",
      "tool_input": {"session_id": "s", "action": "result", "job_id": "jfixture"}, "tool_response": response}
    st.update_job("jfixture", collected_at=time.time())
    rewake.observe_collection(payload)
    self.assertIsNone(st.job("jfixture").get("collected_at"))
    service.jobs("s", "result", "jfixture")
    rewake.observe_collection(payload)
    self.assertTrue(st.job("jfixture").get("collected_at"))

  def test_legacy_full_delivery_upgrades_existing_marker_to_fingerprinted_coverage(self) -> None:
    st.update_job("jfixture", collected_at=time.time())
    rewake.observe_collection({"session_id": "s", "hook_event_name": "PostToolUse",
      "tool_name": "mcp__x__delegate_job", "tool_input": {"session_id": "s", "action": "result", "job_id": "jfixture"},
      "tool_response": {"id": "jfixture", "session_id": "s", "status": "completed", "result": self.job["result"]}})
    self.assertTrue(st.job("jfixture")["result_delivery"]["complete"])


class NotificationTests(DeliveryFixture):
  def setUp(self) -> None:
    super().setUp()
    st.update_job("jfixture", status="running", completion_mode="wait")

  def test_notify_run_returns_immediately_but_keeps_required_result(self) -> None:
    with patch("service._dispatch", return_value={"id": "jfixture"}) as dispatch, patch("service.jobs") as jobs:
      service.run("s", "codex-reviewer", "fixture", "medium", "task", "accept", "reason", self.tmp.name,
                  notify=True, wait_seconds=50)
    self.assertTrue(dispatch.call_args.kwargs["await_completion"])
    self.assertTrue(dispatch.call_args.kwargs["notify"])
    jobs.assert_not_called()

  def test_parent_mode_recognizes_one_shot_and_persistent_streaming_without_recording_argv(self) -> None:
    cases = [(["/bin/claude", "-p", "prompt"], "one-shot"),
      (["/bin/claude", "--print=true"], "one-shot"),
      (["node", "/pkg/@anthropic-ai/claude-code/cli.js", "-p", "--input-format", "stream-json"], "streaming"),
      (["/bin/claude"], "interactive"), (["/bin/python3", "script.py"], "unknown")]
    for argv, expected in cases:
      with patch("pathlib.Path.open", side_effect=lambda *a, **kw: io.BytesIO("\0".join(argv).encode())), \
          patch("pathlib.Path.read_text", return_value="123 (fixture) S 1"):
        self.assertEqual(completion.parent_mode(), expected)

  def test_stop_does_not_close_task_after_guard_cap_with_undelivered_terminal_result(self) -> None:
    st.update_job("jfixture", status="completed", result="x" * 50000)
    service.jobs("s", "result", "jfixture", result_limit=1000)
    for _ in range(5):
      delegate.hook({"session_id": "s", "hook_event_name": "Stop", "stop_hook_active": False})
    self.assertEqual(ac.load(self.task["id"])["status"], "open")
    self.assertEqual(st.session("s")["active_task"], self.task["id"])

  def test_only_explicit_notify_and_live_waiter_defer_stop_terminal_still_requires_collection(self) -> None:
    service.jobs("s", "watch", "jfixture")
    self.assertTrue(self.stop())
    with patch("completion.parent_mode", return_value="interactive"):
      token = completion.register("jfixture", "claude-asyncRewake")
    self.assertEqual(self.stop(), "")
    self.assertIsNone(st.job("jfixture").get("collected_at"))
    self.assertEqual(ac.load(self.task["id"])["status"], "open")
    st.update_job("jfixture", status="completed")
    self.assertTrue(self.stop())
    completion.close("jfixture", token)
    service.jobs("s", "result", "jfixture")
    self.assertEqual(self.stop(), "")

  def test_stale_dead_recycled_and_one_shot_waiters_do_not_bypass_stop(self) -> None:
    service.jobs("s", "watch", "jfixture")
    for mode, kind, mutation in (("interactive", "claude-asyncRewake", "stale"),
        ("interactive", "host-background", "recycled"), ("one-shot", "claude-asyncRewake", None),
        ("unknown", "claude-asyncRewake", None),
        ("interactive", "foreground", None)):
      with patch("completion.parent_mode", return_value=mode):
        token = completion.register("jfixture", kind)
      job = st.job("jfixture")
      row = job["completion_waiters"][token]
      if mutation == "stale":
        row["heartbeat_at"] -= completion.LEASE_SECONDS + 1
      if mutation == "recycled":
        row["identity"]["start"] = "wrong-start"
      st.write("job-jfixture.json", job)
      self.assertFalse(completion.defers_stop(st.job("jfixture")))
      completion.close("jfixture", token)

  def test_async_hook_registers_waiter_and_notifies_once_without_collecting(self) -> None:
    service.jobs("s", "watch", "jfixture")
    payload = {"session_id": "s", "tool_name": "mcp__x__delegate_job", "tool_input": {"action": "watch"},
      "tool_response": {"id": "jfixture", "status": "running"}}
    def finish(seconds: float) -> None:
      self.assertEqual(self.stop(), "")
      st.update_job("jfixture", status="completed")
    with patch("completion.parent_mode", return_value="interactive"), patch("rewake.time.sleep", side_effect=finish):
      self.assertTrue(rewake.watch(payload))
    self.assertFalse(rewake.watch(payload))
    self.assertFalse(completion.live(st.job("jfixture")))
    self.assertIsNone(st.job("jfixture").get("collected_at"))

  def test_managed_background_wait_is_not_a_result_collector(self) -> None:
    def finish(seconds: float) -> None:
      self.assertTrue(completion.defers_stop(st.job("jfixture")))
      self.assertEqual(self.stop(), "")
      st.update_job("jfixture", status="completed")
    with patch("completion.parent_mode", return_value="interactive"), patch("completion.time.sleep", side_effect=finish):
      notice = completion.wait({"session_id": "s", "job_id": "jfixture", "background": True})
    self.assertTrue(notice["needs_result"])
    self.assertNotIn("result", notice)
    self.assertIsNone(st.job("jfixture").get("collected_at"))
    self.assertTrue(self.stop())
    self.assertEqual(notice["next_result_call"]["arguments"]["job_id"], "jfixture")


if __name__ == "__main__":
  unittest.main()

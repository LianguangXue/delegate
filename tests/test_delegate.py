"""Behavior tests with disposable state and no provider inference."""
from pathlib import Path
from unittest.mock import patch, Mock
import json
import os
import subprocess
import sys
import tempfile
import unittest
import time

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import delegate
import service
import storage as st
import worker
import lifecycle
import rewake
import codex_rpc
import runtime_image

CATALOG = {"models": [{"id": "test-model", "model": "test-model", "supportedReasoningEfforts": [
  {"reasoningEffort": "low"}, {"reasoningEffort": "high"}]}]}


class DelegationTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-test-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.temp.name})
    self.env.start()
    self.args = dict(session_id="s1", agent="codex-reviewer", model="test-model", effort="low",
                     task="Inspect fixture", acceptance="Return measured result", rationale="Small bounded lookup",
                     cwd=self.temp.name)
    self.catalog = patch("service.catalog", return_value=CATALOG)
    self.quota = patch("service.quota", return_value={"status": "unknown"})
    self.spawn = patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid()))
    self.catalog.start()
    self.quota.start()
    self.spawn.start()

  def tearDown(self) -> None:
    self.spawn.stop()
    self.quota.stop()
    self.catalog.stop()
    self.env.stop()
    self.temp.cleanup()

  def test_default_off_rejects_automatic_before_provider_request(self) -> None:
    with self.assertRaisesRegex(ValueError, "off/manual"):
      service.run(**self.args)
    service.catalog.assert_not_called()
    self.assertEqual(st.session("s1")["mode"], "off")

  def test_configured_auto_default_enables_dispatch_without_on(self) -> None:
    service.configure("defaults", value={"default_mode": "auto"})
    self.assertEqual(st.session("s1")["mode"], "auto")
    receipt = service.run(**self.args)
    self.assertEqual(receipt["status"], "queued")
    self.assertEqual(st.session("s2")["mode"], "auto")

  def test_default_change_preserves_explicit_off_and_manual(self) -> None:
    service.control("s1", "off")
    service.control("manual", "manual")
    service.configure("defaults", value={"default_mode": "auto"})
    self.assertEqual(st.session("s1")["mode"], "off")
    self.assertEqual(st.session("manual")["mode"], "manual")
    self.assertEqual(st.session("new")["mode"], "auto")
    service.configure("defaults", value={"default_mode": "off"})
    self.assertEqual(st.session("another")["mode"], "off")

  def test_default_auto_does_not_enable_manual_only_roles_or_commits(self) -> None:
    service.configure("defaults", value={"default_mode": "auto"})
    service.configure("agents", "codex-reviewer", {"automatic": False})
    with self.assertRaisesRegex(ValueError, "manual-only"):
      service.run(**self.args)
    receipt=service.run(**self.args,one_shot=True)
    self.assertFalse(st.job(receipt["id"])["allow_commit"])

  def test_invalid_global_default_cannot_be_saved(self) -> None:
    for value in ({"default_mode":"always"},{"default_mode":["auto"]},{"unknown":True}):
      with self.assertRaises(ValueError):service.configure("defaults",value=value)
    self.assertEqual(st.config().get("default_mode","off"),"off")

  def test_default_auto_hook_is_directly_available_without_starting_worker(self) -> None:
    service.configure("defaults",value={"default_mode":"auto"})
    with patch("budget.schedule_refresh"),patch("budget.notice",return_value=("fixture","quota unknown")):
      result=delegate.hook({"session_id":"new","hook_event_name":"SessionStart"})
    self.assertIn("mode=auto",json.dumps(result))
    self.assertIn("without requesting activation",json.dumps(result))
    service.subprocess.Popen.assert_not_called()

  def test_opt_in_is_session_scoped_and_off_keeps_running_job(self) -> None:
    service.control("s1", "on")
    receipt = service.run(**self.args)
    self.assertEqual(st.session("s2")["mode"], "off")
    service.control("s1", "off")
    self.assertFalse(st.job(receipt["id"])["cancel_requested"])
    with self.assertRaises(ValueError):
      service.run(**self.args)

  def test_one_shot_preserves_off_mode(self) -> None:
    service.run(**self.args, one_shot=True)
    self.assertEqual(st.session("s1")["mode"], "off")

  def test_pool_and_role_allowlists_enforced(self) -> None:
    service.control("s1", "on", agents=["claude-explorer"])
    with self.assertRaisesRegex(ValueError, "pool"):
      service.run(**self.args, one_shot=True)
    service.control("s1", "on", agents=["codex-reviewer"])
    service.configure("agents", "codex-reviewer", {"efforts": ["high"]})
    with self.assertRaisesRegex(ValueError, "allowlist"):
      service.run(**self.args)

  def test_invalid_effort_never_starts_worker(self) -> None:
    with self.assertRaisesRegex(ValueError, "Unsupported effort"):
      service.run(**dict(self.args, effort="ultra"), one_shot=True)
    service.subprocess.Popen.assert_not_called()

  def test_global_concurrency_across_sessions(self) -> None:
    service.run(**self.args, one_shot=True)
    service.run(**dict(self.args, session_id="s2"), one_shot=True)
    with self.assertRaisesRegex(ValueError, "concurrency"):
      service.run(**dict(self.args, session_id="s3"), one_shot=True)

  def test_known_exhaustion_blocks_unknown_does_not_fabricate(self) -> None:
    value = {"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 96}}}}
    with self.assertRaisesRegex(ValueError, "reserve"):
      service.check_quota(value, 5)
    service.check_quota({"status": "unknown"}, 5)

  def test_resume_changes_model_effort_without_new_workspace(self) -> None:
    old = service.run(**self.args, one_shot=True)
    st.update_job(old["id"], status="completed", thread_id="thread1", result="Needs more evidence", finished_at=time.time())
    new = service.run(**dict(self.args, effort="high"), one_shot=True, resume_job=old["id"])
    record = st.job(new["id"])
    self.assertEqual(record["thread_id"], "thread1")
    self.assertEqual(record["effort"], "high")
    with self.assertRaisesRegex(ValueError, "active job"):
      service.run(**self.args, one_shot=True, resume_job=old["id"])

  def test_resume_wait_releases_thread_guard_for_worker_archive(self) -> None:
    old = service.run(**self.args, one_shot=True)
    st.update_job(old["id"], status="completed", thread_id="thread1", finished_at=time.time())

    def inspect_wait(*args, **kwargs):
      with service.thread_cleanup.guard("thread1"):
        return {"archive_lock_available": True}

    with patch("service.jobs", side_effect=inspect_wait):
      result = service.run(**self.args, one_shot=True, resume_job=old["id"], wait_seconds=1)
    self.assertTrue(result["archive_lock_available"])

  def test_main_plan_id_is_not_waited_as_job(self) -> None:
    decision = {"id": "p123", "action": "main"}
    with patch("service._dispatch", return_value=decision), patch("service.jobs") as wait:
      result = service.run(**self.args, wait_seconds=1)
    self.assertEqual(result, decision)
    wait.assert_not_called()

  def test_cross_session_result_and_resume_rejected(self) -> None:
    old = service.run(**self.args, one_shot=True)
    st.update_job(old["id"], status="completed", thread_id="thread1", finished_at=time.time())
    with self.assertRaisesRegex(ValueError, "another session"):
      service.jobs("s2", "result", old["id"])
    with self.assertRaisesRegex(ValueError, "same session"):
      service.run(**dict(self.args, session_id="s2"), one_shot=True, resume_job=old["id"])

  def test_cancel_steer_and_verification_are_distinct(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    service.jobs("s1", "tell", jid, message="Focus on bounds")
    self.assertEqual(st.job(jid)["messages"][0]["status"], "queued")
    with self.assertRaises(ValueError):
      service.jobs("s1", "verify", jid, verdict="accepted", evidence="not actually finished")
    service.jobs("s1", "cancel", jid)
    self.assertTrue(st.job(jid)["cancel_requested"])
    st.update_job(jid, status="completed")
    self.assertEqual(st.job(jid)["review"], "pending")
    service.jobs("s1", "verify", jid, verdict="needs-work", evidence="fixture test failed")
    self.assertEqual(st.job(jid)["review"], "needs-work")

  def test_stop_waits_for_required_job_and_for_terminal_result(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    payload = {"session_id": "s1", "hook_event_name": "Stop", "stop_hook_active": False}
    self.assertIn(jid, delegate.hook(payload)["reason"])
    self.assertTrue(st.session("s1").get("active_task"))
    self.assertEqual(delegate.hook({**payload, "stop_hook_active": True}), {})
    st.update_job(jid, status="completed", result="fixture result", finished_at=time.time())
    self.assertIn(jid, delegate.hook({**payload, "stop_hook_active": True})["reason"])
    result = service.jobs("s1", "result", jid)
    self.assertEqual(result["result"], "fixture result")
    self.assertEqual(delegate.hook({**payload, "stop_hook_active": True}), {})
    self.assertIsNone(st.session("s1").get("active_task"))

  def test_background_opt_out_and_stop_failure_do_not_block(self) -> None:
    receipt = service.run(**self.args, one_shot=True, await_completion=False)
    payload = {"session_id": "s1", "hook_event_name": "Stop"}
    self.assertEqual(delegate.hook(payload), {})
    self.assertEqual(delegate.hook({**payload, "hook_event_name": "StopFailure"}), {})
    st.update_job(receipt["id"], status="completed", finished_at=time.time())
    receipt = service.run(**self.args, one_shot=True)
    self.assertIn(receipt["id"], delegate.hook(payload)["reason"])
    service.jobs("s1", "detach", receipt["id"])
    self.assertEqual(delegate.hook(payload), {})

  def test_background_argument_dispatches_detached_without_waiting(self) -> None:
    with patch("service.jobs") as jobs:
      receipt = service.run(**self.args, one_shot=True, background=True, wait_seconds=50)
    jobs.assert_not_called()
    self.assertTrue(receipt["background"])
    self.assertFalse(receipt["await_completion"])
    self.assertFalse(st.job(receipt["id"])["await_completion"])
    self.assertEqual(delegate.hook({"session_id":"s1","hook_event_name":"Stop"}), {})

  def test_background_does_not_enable_delegation_or_accept_non_boolean(self) -> None:
    with self.assertRaisesRegex(ValueError,"off/manual"):
      service.run(**self.args,background=True)
    with self.assertRaisesRegex(ValueError,"background must be"):
      service.run(**self.args,one_shot=True,background="yes")

  def test_wait_progress_allows_another_stop_continuation(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    payload = {"session_id": "s1", "hook_event_name": "Stop"}
    self.assertIn(receipt["id"], delegate.hook(payload)["reason"])
    with patch("service.time.monotonic", side_effect=[0, 51]):
      service.jobs("s1", "result", receipt["id"], wait_seconds=50)
    self.assertIn(receipt["id"], delegate.hook({**payload, "stop_hook_active": True})["reason"])

  def test_async_rewake_terminal_job_once_and_only_owner(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    st.update_job(jid, status="completed", result="fixture", finished_at=time.time())
    payload = {"session_id": "s1", "tool_name": "mcp__plugin_delegate_delegate__delegate_run",
               "tool_response": {"content": [{"type": "text", "text": json.dumps(receipt)}]}}
    self.assertFalse(rewake.watch({**payload, "session_id": "other"}))
    self.assertTrue(rewake.watch(payload))
    self.assertFalse(rewake.watch(payload))

  def test_async_rewake_skips_collected_and_non_job_results(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    st.update_job(jid, status="completed", finished_at=time.time())
    service.jobs("s1", "result", jid)
    payload = {"session_id": "s1", "tool_name": "mcp__plugin_delegate_delegate__delegate_run",
               "tool_response": receipt}
    self.assertFalse(rewake.watch(payload))
    self.assertFalse(rewake.watch({**payload, "tool_response": {"id": "p123", "action": "main"}}))

  def test_detached_job_does_not_capture_next_task(self) -> None:
    receipt = service.run(**self.args, one_shot=True, await_completion=False)
    first = st.session("s1")["active_task"]
    delegate.hook({"session_id": "s1", "hook_event_name": "Stop"})
    self.assertIsNone(st.session("s1").get("active_task"))
    second = service.run(**self.args, one_shot=True)
    self.assertNotEqual(st.session("s1")["active_task"], first)
    self.assertNotEqual(st.job(receipt["id"])["task_id"], st.job(second["id"])["task_id"])

  def test_orphaned_job_is_reported_without_wait_loop(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    st.update_job(jid, status="orphaned")
    with patch("service.reconcile", side_effect=lambda _: st.job(jid)):
      payload = {"session_id": "s1", "hook_event_name": "Stop"}
      self.assertIn("orphaned", delegate.hook(payload)["reason"])
      self.assertEqual(service.jobs("s1", "result", jid, wait_seconds=50)["status"], "orphaned")
      self.assertEqual(delegate.hook({**payload, "stop_hook_active": True}), {})
      self.assertIsNone(st.session("s1").get("active_task"))

  def test_legacy_result_delivery_is_acknowledged_by_hook(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    st.update_job(jid, status="completed", result="legacy output", finished_at=time.time())
    response = service.summary(st.job(jid), result=True)  # Old server writes no collected_at.
    payload = {"session_id": "s1", "hook_event_name": "PostToolUse",
               "tool_name": "mcp__plugin_delegate_delegate__delegate_job",
               "tool_input": {"session_id": "s1", "action": "result", "job_id": jid},
               "tool_response": {"content": [{"type": "text", "text": json.dumps(response)}]}}
    delegate.hook(payload)
    self.assertTrue(st.job(jid)["collected_at"])
    self.assertEqual(delegate.hook({"session_id": "s1", "hook_event_name": "Stop"}), {})
    self.assertFalse(rewake.watch({"session_id": "s1", "tool_name": "mcp__x__delegate_run",
                                  "tool_response": receipt}))

  def test_collection_observer_rejects_errors_active_and_wrong_owner(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    st.update_job(jid, status="completed", result="fixture")
    response = service.summary(st.job(jid), result=True)
    payload = {"session_id": "s1", "hook_event_name": "PostToolUse",
               "tool_name": "mcp__x__delegate_job",
               "tool_input": {"session_id": "s1", "action": "result", "job_id": jid},
               "tool_response": response}
    cases = [dict(payload, session_id="other"), dict(payload, agent_id="child"),
             dict(payload, tool_response={"isError": True, "content": [{"text": json.dumps(response)}]}),
             dict(payload, tool_response={**response, "status": "running"}),
             dict(payload, tool_response={k:v for k,v in response.items() if k != "result"}),
             dict(payload, tool_input={**payload["tool_input"], "job_id": "jother"})]
    for case in cases:
      rewake.observe_collection(case)
      self.assertIsNone(st.job(jid).get("collected_at"))

  def test_completed_combined_run_and_failed_result_delivery(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    st.update_job(jid, status="failed", result=None, error="fixture failure")
    rewake.observe_collection({"session_id": "s1", "hook_event_name": "PostToolUse",
      "tool_name": "mcp__x__delegate_run", "tool_input": {"session_id": "s1"},
      "tool_response": json.dumps(service.summary(st.job(jid), result=True))})
    self.assertTrue(st.job(jid)["collected_at"])
    self.assertEqual(st.job(jid)["status"], "failed")

  def test_legacy_failure_without_result_field_is_collected(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    jid = receipt["id"]
    st.update_job(jid, status="timed_out", error=None)
    response = service.summary(st.job(jid), result=True)
    response.pop("result", None)
    rewake.observe_collection({"session_id": "s1", "hook_event_name": "PostToolUse",
      "tool_name": "mcp__x__delegate_job",
      "tool_input": {"session_id": "s1", "action": "result", "job_id": jid},
      "tool_response": response})
    self.assertTrue(st.job(jid)["collected_at"])

  def test_stop_has_per_task_hard_cap_even_if_host_resets_flag(self) -> None:
    receipt = service.run(**self.args, one_shot=True)
    payload = {"session_id": "s1", "hook_event_name": "Stop", "stop_hook_active": False}
    for count in range(3):
      st.update_job(receipt["id"], wait_count=count)
      self.assertEqual(delegate.hook(payload)["decision"], "block")
    for count in range(3, 10):
      st.update_job(receipt["id"], wait_count=count)
      self.assertEqual(delegate.hook(payload), {})
    st.update_job(receipt["id"], status="completed", finished_at=time.time())
    service.jobs("s1", "result", receipt["id"])
    delegate.hook(payload)
    next_job = service.run(**self.args, one_shot=True)
    self.assertIn(next_job["id"], delegate.hook(payload)["reason"])

  def test_config_rejects_secrets_and_does_not_activate(self) -> None:
    with self.assertRaisesRegex(ValueError, "secrets"):
      service.configure("providers", "deepseek", {"api_key": "dummy"})
    service.configure("agents", "custom", {"provider": "codex", "enabled": True, "automatic": True,
                      "workspace": "read-only", "instructions": "Review bounds"})
    self.assertEqual(st.session("s1")["mode"], "off")

  def test_hook_notifies_once_and_never_starts_a_worker(self) -> None:
    first = delegate.hook({"session_id": "s1", "hook_event_name": "SessionStart"})
    self.assertIn("mode=off", json.dumps(first))
    receipt = service.run(**self.args, one_shot=True)
    st.update_job(receipt["id"], status="completed")
    output = delegate.hook({"session_id": "s1", "hook_event_name": "PostToolUse"})
    self.assertIn(receipt["id"], json.dumps(output))
    self.assertEqual(delegate.hook({"session_id": "s1", "hook_event_name": "PostToolUse"}), {})

  def test_claude_quota_absence_is_unknown(self) -> None:
    self.assertIn("unknown", delegate.statusline({"session_id": "s1", "context_window": {"used_percentage": 5}}))
    self.assertEqual(st.read("claude-quota.json")["status"], "unknown")

  def test_anthropic_env_is_child_only_and_no_recursive_tools(self) -> None:
    record = {"provider": {"kind": "anthropic", "command": "claude", "base_url": "https://example.com",
                            "api_key_env": "TEST_PROVIDER_KEY"}, "model": "test", "effort": "low",
              "role": {"workspace": "read-only"}}
    with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "dummy", "ANTHROPIC_BASE_URL": "original"}):
      command, env = worker.claude_command(record)
      self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], "original")
      self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://example.com")
      self.assertIn("--restricted", command)
      self.assertNotIn("Bash", command[command.index("--tools") + 1])
      self.assertEqual(env["DELEGATE_WORKER"], "1")

  def test_mcp_schema_has_required_selection_and_acceptance(self) -> None:
    tool = next(t for t in delegate.tool_list() if t["name"] == "delegate_run")
    self.assertTrue({"model", "effort", "rationale", "acceptance"} <= set(tool["inputSchema"]["required"]))

  def test_resume_requires_cleanup_receipt_and_unchanged_provider(self) -> None:
    old = service.run(**self.args, one_shot=True)
    st.update_job(old["id"], status="completed", thread_id="thread1")
    with self.assertRaisesRegex(ValueError, "finished job"):
      service.run(**self.args, one_shot=True, resume_job=old["id"])
    st.update_job(old["id"], finished_at=time.time())
    service.configure("providers", "codex", {"command": "different-codex"})
    with self.assertRaisesRegex(ValueError, "changed"):
      service.run(**self.args, one_shot=True, resume_job=old["id"])

  def test_resume_old_attempt_uses_latest_thread_counter_baseline(self) -> None:
    first = service.run(**self.args, one_shot=True)
    st.update_job(first["id"], status="completed", thread_id="thread1", finished_at=time.time(),
                  usage={"total": {"inputTokens": 100, "outputTokens": 10}})
    second = service.run(**self.args, one_shot=True, resume_job=first["id"])
    st.update_job(second["id"], status="completed", thread_id="thread1", finished_at=time.time() + 1,
                  usage={"total": {"inputTokens": 300, "outputTokens": 20}})
    st.update_job(first["id"], finished_at=time.time() + 1000)  # Later cleanup of old attempt.
    third = service.run(**self.args, one_shot=True, resume_job=first["id"])
    self.assertEqual(st.job(third["id"])["usage_before"]["inputTokens"], 300)

  def test_crashed_worker_releases_slot_only_when_backend_is_gone(self) -> None:
    old = service.run(**self.args, one_shot=True)
    st.update_job(old["id"], created_at=time.time()-30, worker_identity=None)
    with patch("lifecycle.group_running", return_value=True):
      self.assertEqual(lifecycle.reconcile(old["id"])["status"], "orphaned")
    with patch("lifecycle.group_running", return_value=False):
      self.assertEqual(lifecycle.reconcile(old["id"])["status"], "failed")
    self.assertIn("finished_at", st.job(old["id"]))

  def test_resume_checks_orphan_sibling_missing_from_active_index(self) -> None:
    old = service.run(**self.args, one_shot=True)
    st.update_job(old["id"], status="completed", thread_id="thread1", finished_at=time.time())
    record = dict(st.job(old["id"]), id="old-attempt", status="orphaned", backend_starting=True)
    st.write("job-old-attempt.json", record)
    state = st.session("s1")
    state["jobs"].append("old-attempt")
    st.save_session("s1", state)
    st.write("active.json", [])
    with self.assertRaisesRegex(ValueError, "unconfirmed cleanup"):
      service.run(**self.args, one_shot=True, resume_job=old["id"])

  def test_process_identity_uses_boot_and_start_time(self) -> None:
    record = lifecycle.identity(os.getpid())
    self.assertTrue(lifecycle.alive(record))
    self.assertFalse(lifecycle.alive(dict(record, start="wrong")))

  def test_mcp_survives_valid_json_with_wrong_shape(self) -> None:
    self.spawn.stop()
    requests = '\n'.join(['[]', 'null', '{"id":1,"method":"tools/list","params":[]}',
                         '{"id":2,"method":"tools/list"}']) + '\n'
    process = subprocess.run([sys.executable, str(SCRIPTS / "delegate.py"), "mcp"],
                             input=requests, capture_output=True, text=True, check=True)
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    self.assertEqual(len(responses), 4)
    self.assertEqual(responses[-1]["id"], 2)
    self.assertEqual(len(responses[-1]["result"]["tools"]), 13)
    self.spawn.start()


class WorkspaceTests(unittest.TestCase):
  def test_cleanup_stops_term_ignoring_descendant_after_leader_exits(self) -> None:
    script = ('import os,signal,time\n'
              'pid=os.fork()\n'
              'if pid==0:\n'
              ' signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
              ' print("ready",flush=True)\n'
              ' time.sleep(60)\n'
              'else:\n time.sleep(60)\n')
    process = subprocess.Popen([sys.executable, '-c', script], start_new_session=True,
                               stdout=subprocess.PIPE, text=True)
    record = lifecycle.identity(process.pid, include_zombies=True)
    try:
      self.assertEqual(process.stdout.readline().strip(), 'ready')
      lifecycle.stop_owned(process, record)
      self.assertFalse(lifecycle.group_running(record))
    finally:
      if lifecycle.group_running(record):
        os.killpg(process.pid, 9)
      process.wait(timeout=5)
      process.stdout.close()

  def test_codex_spawn_registered_before_handshake(self) -> None:
    with tempfile.TemporaryDirectory(prefix='delegate-rpc-') as directory:
      fake = Path(directory) / 'fake-codex'
      fake.write_text('#!/usr/bin/env python3\nimport sys,json\n'
                      'for line in sys.stdin:\n'
                      ' m=json.loads(line)\n'
                      ' if m.get("method")=="initialize":\n'
                      '  print(json.dumps({"id":m["id"],"result":{}}),flush=True)\n')
      fake.chmod(0o700)
      seen = []
      with codex_rpc.CodexRPC(str(fake), on_spawn=lambda pid: seen.append(lifecycle.identity(pid))) as client:
        self.assertEqual(len(seen), 1)
        self.assertTrue(lifecycle.alive(seen[0]))
      self.assertFalse(lifecycle.group_running(seen[0]))

  def test_cancel_waits_for_backend_cleanup_even_when_stdin_is_never_read(self) -> None:
    with tempfile.TemporaryDirectory(prefix="delegate-cancel-") as directory:
      fake = Path(directory) / "fake-claude"
      fake.write_text('#!/usr/bin/env python3\nimport signal,time,sys\n'
                      'def stop(*args):\n    time.sleep(2)\n    sys.exit(0)\n'
                      'signal.signal(signal.SIGTERM,stop)\ntime.sleep(60)\n')
      fake.chmod(0o700)
      with patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(Path(directory) / "state")}):
        image = runtime_image.freeze(SCRIPTS)
        value = {"runtime_image": image, "id": "canceltest", "session_id": "s1", "source_cwd": directory, "cwd": directory,
                 "role": {"workspace": "read-only", "instructions": "fixture"},
                 "provider": {"kind": "claude", "command": str(fake)}, "provider_name": "claude",
                 "model": "haiku", "effort": "low", "task": "x" * 90000, "context": "",
                 "acceptance": "fixture", "created_at": time.time(), "timeout_seconds": 30,
                 "status": "queued", "cancel_requested": False}
        st.write("job-canceltest.json", value)
        process = subprocess.Popen([sys.executable, str(Path(image["path"]) / "worker.py"), "canceltest"])
        try:
          deadline = time.monotonic() + 5
          while not st.job("canceltest").get("backend_identity") and time.monotonic() < deadline:
            time.sleep(0.05)
          self.assertTrue(st.job("canceltest").get("backend_identity"))
          time.sleep(0.2)
          st.update_job("canceltest", cancel_requested=True)
          while process.poll() is None and time.monotonic() < deadline + 8:
            current = st.job("canceltest")
            if lifecycle.alive(current.get("backend_identity")):
              self.assertIn(current["status"], lifecycle.ACTIVE)
            time.sleep(0.05)
          process.wait(timeout=5)
          current = st.job("canceltest")
          self.assertEqual(current["status"], "cancelled")
          self.assertFalse(lifecycle.alive(current["backend_identity"]))
          self.assertIn("finished_at", current)
        finally:
          if process.poll() is None:
            process.terminate()
            process.wait(timeout=8)

  def test_write_worktree_is_separate_and_dirty_tree_rejected(self) -> None:
    with tempfile.TemporaryDirectory(prefix="delegate-repo-") as directory:
      source = Path(directory) / "repo"
      source.mkdir()
      subprocess.run(["git", "init", "-q", str(source)], check=True)
      (source / "file.txt").write_text("baseline\n")
      worker.git(str(source), "add", "file.txt")
      worker.git(str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
      with patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(Path(directory) / "state")}):
        value = {"id": "fixture", "source_cwd": str(source), "cwd": str(source),
                 "role": {"workspace": "worktree"}}
        st.write("job-fixture.json", value)
        output = Path(worker.prepare(value))
        self.assertNotEqual(output, source)
        (output / "file.txt").write_text("changed\n")
        self.assertEqual((source / "file.txt").read_text(), "baseline\n")
        (source / "dirty.txt").write_text("user data")
        st.write("job-second.json", dict(value, id="second"))
        with self.assertRaisesRegex(ValueError, "clean source"):
          worker.prepare(dict(value, id="second"))


if __name__ == "__main__":
  unittest.main()

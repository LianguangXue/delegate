"""Unreachable briefs/output permissions must fail before a paid worker starts."""
from pathlib import Path
from unittest.mock import Mock, patch
import copy
import hashlib
import json
import os
import sys
import subprocess
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"))
import input_contract as files
import optimizer
import rewake
import runtime_image
import service
import storage as st
import worker
import workspaces


class InputContractTests(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory(prefix="delegate-input-access-")
    self.root = Path(self.tmp.name)
    self.cwd = self.root / "repo"
    self.cwd.mkdir()
    self.brief = self.root / "BRIEF.md"
    self.brief.write_text("Analyze the supplied evidence")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.root / "state")})
    self.env.start()
    cfg = copy.deepcopy(st.DEFAULT_CONFIG)
    cfg["providers"]["company"] = {"kind": "anthropic", "enabled": True, "command": "fixture",
      "models": ["deepseek-flash", "deepseek-v4-pro"], "efforts": ["low", "medium", "high"]}
    cfg["agents"]["company-reviewer"] = {"provider": "company", "workspace": "read-only",
      "enabled": True, "automatic": True, "instructions": "Review"}
    st.write("config.json", cfg)
    self.args = dict(session_id="s", agent="company-reviewer", model="deepseek-v4-pro", effort="high",
      task="Follow the brief in " + str(self.brief) + " exactly (read it first).", acceptance="Report evidence",
      rationale="Bounded analysis", cwd=str(self.cwd), one_shot=True)

  def tearDown(self) -> None:
    self.env.stop()
    self.tmp.cleanup()

  def test_original_cross_directory_brief_fails_before_catalog_quota_job_or_model(self) -> None:
    with patch("service.catalog") as catalog, patch("service.quota") as quota, patch("service.subprocess.Popen") as spawn:
      with self.assertRaisesRegex(ValueError, "outside this file-tool role's cwd"):
        service.run(**self.args)
    catalog.assert_not_called()
    quota.assert_not_called()
    spawn.assert_not_called()
    self.assertIsNone(st.session("s").get("active_task"))

  def test_auto_filters_file_only_roles_before_discovery_for_external_brief(self) -> None:
    service.control("s", "on", strategy="preserve-claude")
    def catalog(name: str, **kwargs):
      self.assertEqual(name, "codex")
      return {"models": [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]}
    with patch("service.catalog", side_effect=catalog), patch("service.quota", return_value={"status": "unknown"}):
      plan = optimizer.plan("s", cwd=str(self.cwd), required_files=[str(self.brief)])
    self.assertEqual(plan["provider"], "codex")
    self.assertIn("outside", plan["errors"]["agent:company-reviewer"])

  def test_required_output_files_conflict_with_read_only_before_model_selection(self) -> None:
    with patch("service.catalog") as catalog:
      with self.assertRaisesRegex(ValueError, "read-only reviewer"):
        service.run(**{**self.args, "cwd": str(self.root)}, output_files=["report.md"])
      catalog.assert_not_called()
    plan = optimizer.plan("s", cwd=str(self.cwd), output_files=["report.md"])
    self.assertEqual(plan["action"], "replan")

  def test_containing_cwd_can_read_brief_but_no_output_permission_is_inferred(self) -> None:
    report = files.check({"kind": "anthropic"}, "read-only", str(self.root), [str(self.brief)], [], probe=True)
    self.assertEqual(report["status"], "passed")
    self.assertEqual(report["output_files"], [])
    with self.assertRaisesRegex(ValueError, "inside"):
      files.check({"kind": "codex"}, "worktree", str(self.cwd), [], [str(self.root / "report.md")])

  def test_missing_and_special_inputs_fail_without_reading_or_hanging(self) -> None:
    fifo = self.cwd / "input.fifo"
    os.mkfifo(fifo)
    for path in (str(self.cwd / "missing.md"), str(fifo), str(self.cwd)):
      with self.assertRaises(ValueError):
        files.check({"kind": "codex"}, "read-only", str(self.cwd), [path], [], probe=True)
    link = self.cwd / "brief.md"
    link.symlink_to(self.brief)
    with self.assertRaisesRegex(ValueError, "outside"):
      files.check({"kind": "anthropic"}, "read-only", str(self.cwd), ["brief.md"], [])

  def test_output_checks_reject_directories_and_file_ancestors_without_claiming_writability(self) -> None:
    (self.cwd / "blocker").write_text("a regular file")
    for output in (".", "blocker/child/report.md"):
      with self.assertRaises(ValueError):
        files.check({"kind": "codex"}, "worktree", str(self.cwd), [], [output])
    report = files.check({"kind": "codex"}, "worktree", str(self.cwd), [], ["report.md"])
    self.assertFalse(report["write_access_verified"])

  def test_explicit_manifest_can_exclude_historical_absolute_references(self) -> None:
    with patch("service.catalog", return_value={"models": ["deepseek-v4-pro"], "efforts": ["high"]}), \
        patch("service.quota", return_value={"status": "unknown"}), patch("credentials.environment", return_value={}), \
        patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      result = service.run(**{**self.args, "task": "Do not read the historical path " + str(self.brief)}, required_files=[])
    self.assertEqual(result["required_files"], [])

  def test_file_builder_absolute_source_reference_is_rejected_even_with_snapshot(self) -> None:
    cfg = st.config()
    cfg["agents"]["company-builder"] = {**cfg["agents"]["company-reviewer"], "workspace": "worktree"}
    st.write("config.json", cfg)
    draft = self.cwd / "README.md"
    draft.write_text("draft")
    with patch("service.catalog") as catalog:
      with self.assertRaisesRegex(ValueError, "cwd-relative"):
        service.run(**{**self.args, "agent": "company-builder", "task": "Edit " + str(draft)}, source_inputs=["README.md"])
      catalog.assert_not_called()

  def test_file_builder_relative_contract_reads_snapshot_and_preserves_original_task(self) -> None:
    def git(*args):
      return subprocess.check_output(["git", "-C", str(self.cwd), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "commit.gpgsign", "false")
    (self.cwd / "README.md").write_text("base")
    git("add", "README.md")
    git("commit", "-qm", "fixture")
    base = git("rev-parse", "HEAD")
    (self.cwd / "README.md").write_text("draft")
    cfg = st.config()
    cfg["agents"]["company-builder"] = {**cfg["agents"]["company-reviewer"], "workspace": "worktree"}
    st.write("config.json", cfg)
    with patch("service.catalog", return_value={"models": ["deepseek-v4-pro"], "efforts": ["high"]}), \
        patch("service.quota", return_value={"status": "unknown"}), patch("credentials.environment", return_value={}), \
        patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      receipt = service.run(**{**self.args, "agent": "company-builder", "task": "Edit README.md", "operation": "write"},
        required_files=["README.md"], output_files=["report.md"], source_inputs=["README.md"])
    target = Path(workspaces.prepare(st.job(receipt["id"])))
    job = st.job(receipt["id"])
    report = files.check(job["provider"], "worktree", str(target), job["required_files"], job["output_files"], probe=True)
    self.assertEqual(report["status"], "passed")
    self.assertEqual((target / "README.md").read_text(), "draft")
    self.assertEqual((self.cwd / "README.md").read_text(), "draft")
    self.assertEqual(git("rev-parse", "HEAD"), base)
    self.assertIn("FILE CONTRACT:", worker.packet(job))
    self.assertEqual(job["task"], "Edit README.md")

  def test_codex_probe_uses_same_policy_and_never_returns_file_contents(self) -> None:
    job = {"provider": {"kind": "codex"}, "role": {"workspace": "read-only"}, "cwd": str(self.root),
      "required_files": ["BRIEF.md"]}
    rpc = Mock()
    rpc.call.return_value = {"exitCode": 0, "stdout": json.dumps({"files": [{"path": str(self.brief), "ok": True}]})}
    policy = {"type": "readOnly", "networkAccess": False}
    report = files.preflight(rpc, job, policy)
    self.assertTrue(report["execution_policy_verified"])
    self.assertEqual(rpc.call.call_args.args[0], "command/exec")
    self.assertEqual(rpc.call.call_args.args[1]["sandboxPolicy"], policy)
    self.assertNotIn("Analyze the supplied", json.dumps(report))

  def test_failed_codex_file_probe_sends_no_account_thread_or_turn(self) -> None:
    job = {"id": "jprobe", "session_id": "s", "status": "running", "created_at": time.time(),
      "provider": {"kind": "codex", "command": "fixture"}, "provider_name": "codex",
      "role": {"workspace": "read-only"}, "cwd": str(self.root), "source_cwd": str(self.root),
      "model": "fixture", "effort": "medium", "timeout_seconds": 900, "messages": [],
      "cancel_requested": False, "required_files": ["BRIEF.md"], "inference_state": "not_started"}
    st.write("job-jprobe.json", job)
    calls = []
    class RPC:
      def __init__(self, *args, **kwargs): pass
      def __enter__(self): return self
      def __exit__(self, *args): pass
      def call(self, method, params=None, timeout=30):
        calls.append(method)
        if method != "command/exec":raise AssertionError(method)
        return {"exitCode": 0, "stdout": json.dumps({"files": [{"path": str(job["cwd"]) + "/BRIEF.md", "ok": False}]})}
    with patch("worker.CodexRPC", RPC):
      result = worker.codex_worker(job, "Fixture")
    self.assertEqual(result["status"], "failed")
    self.assertEqual(calls, ["command/exec"])
    self.assertEqual(st.job("jprobe")["inference_state"], "not_started")

  def test_version_provenance_and_notice_do_not_mislabel_old_result_as_current_server(self) -> None:
    image = self.root / "image"
    image.mkdir()
    text = '{"serverInfo": {"name": "delegate", "version": "0.14.1"}}'
    (image / "delegate.py").write_text(text)
    job = {"id": "jold", "session_id": "s", "status": "completed", "created_at": time.time(), "timeout_seconds": 0,
      "runtime_image": {"path": str(image), "digest": hashlib.sha256(text.encode()).hexdigest()}}
    st.write("job-jold.json", job)
    self.assertEqual(runtime_image.job_version(job), "0.14.1")
    summary = service.summary(job)
    self.assertEqual(summary["job_runtime_version"], "0.14.1")
    self.assertEqual(summary["server_runtime_version"], runtime_image.VERSION)
    payload = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "mcp__x__delegate_run",
      "tool_response": {"id": "jold", "session_id": "s", "status": "completed"}}
    self.assertIn("SAME session_id=s", rewake.runtime_notice(payload))
    self.assertEqual(rewake.runtime_notice(payload), "")
    self.assertEqual(rewake.runtime_notice({**payload, "tool_name": "mcp__x__delegate_job"}), "")

  def test_contract_plan_cannot_silently_drop_new_task_references(self) -> None:
    service.control("s", "on", agents=["codex-reviewer"], strategy="preserve-claude")
    data = {"models": [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]}
    with patch("service.catalog", return_value=data), patch("service.quota", return_value={"status": "unknown"}):
      plan = optimizer.plan("s", cwd=str(self.cwd), required_files=[str(self.brief)])
      extra = self.root / "extra.md"
      extra.write_text("more input")
      with self.assertRaisesRegex(ValueError, "required_files differs"):
        service.run("s", plan["agent"], plan["model"], plan["effort"], "Also read " + str(extra), "Evidence", "Why",
          str(self.cwd), plan_id=plan["id"])


if __name__ == "__main__":
  unittest.main()

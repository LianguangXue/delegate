"""Subagent permission tiers, worktree relocation, and structured redaction tests."""
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import capabilities
import delegate
import optimizer
import persistent_tasks
import service
import storage as st
import worker
import workspaces


class SubagentPermissionTests(unittest.TestCase):
  """Verify parent-selectable subagent permissions and DeepSeek write/shell support."""

  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-perms-")
    self.root = Path(self.temp.name)
    self.state_dir = self.root / "state"
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.state_dir)})
    self.env.start()
    self.cfg = copy.deepcopy(st.DEFAULT_CONFIG)
    self.cfg["max_concurrency"] = 5
    self.cfg["providers"]["codex"] = {"kind": "codex", "enabled": True, "command": "codex"}
    self.cfg["providers"]["claude"]["enabled"] = False
    self.cfg["providers"]["company"] = {
      "kind": "anthropic",
      "enabled": True,
      "command": "claude",
      "base_url": "https://example.invalid/anthropic",
      "api_key_env": "TEST_COMPANY_KEY",
      "models": ["deepseek-flash", "deepseek-v4-pro"],
      "efforts": ["low", "medium", "high", "max"],
    }
    self.cfg["agents"]["company-reviewer"] = {
      "provider": "company",
      "enabled": True,
      "automatic": True,
      "workspace": "read-only",
      "instructions": "Review requested files.",
    }
    self.cfg["agents"]["company-builder"] = {
      "provider": "company",
      "enabled": True,
      "automatic": True,
      "workspace": "worktree",
      "instructions": "Implement the smallest complete change in the isolated worktree.",
    }
    st.write("config.json", self.cfg)
    self.catalog = {"models": ["deepseek-flash", "deepseek-v4-pro"], "efforts": ["low", "medium", "high", "max"]}

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def test_worktree_root_relocates_outside_dot_claude(self) -> None:
    dot_claude_state = self.root / "home" / ".claude" / "delegate"
    xdg_data = self.root / "xdg_data"
    with patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(dot_claude_state), "XDG_DATA_HOME": str(xdg_data)}):
      wt_root = st.worktree_root()
      self.assertEqual(wt_root, (xdg_data / "claude-delegate" / "worktrees").resolve())
      self.assertNotIn(".claude", wt_root.parts)

    self.assertEqual(st.worktree_root(), (self.state_dir / "worktrees").resolve())

    explicit_wt = self.root / "custom-wt"
    with patch.dict(os.environ, {"DELEGATE_WORKTREE_DIR": str(explicit_wt)}):
      self.assertEqual(st.worktree_root(), explicit_wt.resolve())

    repo = self.root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("base\n")
    worker.git(str(repo), "add", "a.txt")
    worker.git(str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    with patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(dot_claude_state), "XDG_DATA_HOME": str(xdg_data)}):
      job = {"id": "jrelocate1", "source_cwd": str(repo), "cwd": str(repo), "role": {"workspace": "worktree"}}
      st.write("job-jrelocate1.json", job)
      prepared = Path(workspaces.prepare(job))
      self.assertTrue(prepared.is_relative_to(xdg_data.resolve()))
      self.assertNotIn(".claude", prepared.parts)

  def test_claude_command_modes_flags_tools_and_packet_prompting(self) -> None:
    base = {
      "provider": {"kind": "anthropic", "command": "claude", "base_url": "https://example.invalid",
                   "api_key_env": "TEST_DS_KEY"},
      "provider_name": "company",
      "model": "deepseek-flash",
      "effort": "low",
      "cwd": str(self.root),
      "source_cwd": str(self.root),
      "task": "Edit file",
      "acceptance": "Edits applied",
      "context": "",
      "timeout_seconds": 300,
    }
    with patch.dict(os.environ, {"TEST_DS_KEY": "dummy"}):
      ro = {**base, "permissions": "read-only", "role": {"workspace": "read-only", "instructions": "Review."}}
      cmd_ro, _ = worker.claude_command(ro)
      self.assertIn("--restricted", cmd_ro)
      self.assertEqual(cmd_ro[cmd_ro.index("--permission-mode") + 1], "dontAsk")
      self.assertEqual(cmd_ro[cmd_ro.index("--permission-prompts") + 1], "none")
      self.assertEqual(cmd_ro[cmd_ro.index("--tools") + 1], "Read,Glob,Grep")

      ww = {**base, "permissions": "workspace-write",
            "configured_role": {"workspace": "read-only", "instructions": "Review."},
            "role": {"workspace": "worktree", "instructions": "Review."}}
      cmd_ww, _ = worker.claude_command(ww)
      self.assertIn("--restricted", cmd_ww)
      self.assertEqual(cmd_ww[cmd_ww.index("--permission-mode") + 1], "acceptEdits")
      self.assertEqual(cmd_ww[cmd_ww.index("--permission-prompts") + 1], "none")
      self.assertEqual(cmd_ww[cmd_ww.index("--tools") + 1], "Read,Glob,Grep,Edit,Write")
      self.assertIn("PERMISSION GRANT", worker.packet(ww))
      self.assertIn("permission_mode=acceptEdits", worker.environment_brief(ww))

      ro_override = {**base, "permissions": "read-only",
                     "configured_role": {"workspace": "worktree", "instructions": "Build."},
                     "role": {"workspace": "read-only", "instructions": "Build."}}
      self.assertIn("restricted this attempt to permissions=read-only", worker.packet(ro_override))

      auto_job = {**base, "permissions": "auto", "role": {"workspace": "worktree", "instructions": "Build."}}
      cmd_auto, _ = worker.claude_command(auto_job)
      self.assertEqual(cmd_auto[cmd_auto.index("--permission-mode") + 1], "acceptEdits")

      for bad_perm in ("admin", "read", 123):
        bad_job = {**base, "permissions": bad_perm, "role": {"workspace": "worktree", "instructions": "Build."}}
        with self.assertRaisesRegex(ValueError, "Unsupported worker permissions"):
          worker.claude_command(bad_job)
        with self.assertRaisesRegex(ValueError, "Unsupported worker permissions"):
          worker.environment_brief(bad_job)

      ws = {**base, "permissions": "workspace-shell",
            "role": {"workspace": "worktree", "instructions": "Build."},
            "execution_environment": {
              "profile": "workspace-shell",
              "permissions": "workspace-shell",
              "requirements": {"gpu": True, "gpu_devices": ["0"], "ssh": False, "network": False,
                               "ssh_targets": [], "network_targets": []},
            }}
      cmd_ws, env_ws = worker.claude_command(ws)
      self.assertNotIn("--restricted", cmd_ws)
      self.assertIn("--allow-dangerously-skip-permissions", cmd_ws)
      self.assertEqual(cmd_ws[cmd_ws.index("--permission-mode") + 1], "bypassPermissions")
      self.assertEqual(cmd_ws[cmd_ws.index("--permission-prompts") + 1], "none")
      self.assertEqual(cmd_ws[cmd_ws.index("--add-dir") + 1], str(self.root.resolve()))
      self.assertEqual(cmd_ws[cmd_ws.index("--tools") + 1], "Read,Glob,Grep,Edit,Write,Bash")
      self.assertEqual(env_ws["CUDA_VISIBLE_DEVICES"], "0")
      self.assertIn("PERMISSION GRANT", worker.packet(ws))
      self.assertIn("permission_mode=bypassPermissions", worker.environment_brief(ws))

  def test_service_run_explicit_permissions_overrides_role_defaults(self) -> None:
    repo = self.root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "src.txt").write_text("v1\n")
    worker.git(str(repo), "add", "src.txt")
    worker.git(str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
    ext_brief = self.root / "BRIEF.md"
    ext_brief.write_text("External brief")

    with patch("service.catalog", return_value=self.catalog), \
         patch("service.quota", return_value={"status": "unknown"}), \
         patch("credentials.environment", return_value={}), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      # Reviewer role overridden to workspace-write without passing operation="write".
      ww_receipt = service.run(
        "s1", "company-reviewer", "deepseek-flash", "low", "Edit src.txt", "File updated",
        "Parent chose workspace-write", str(repo), one_shot=True, permissions="workspace-write",
        output_files=["report.md"], allow_commit=True, commit_message="Update src", commit_paths=["src.txt"],
      )
      ww_job = st.job(ww_receipt["id"])
      self.assertEqual(ww_job["permissions"], "workspace-write")
      self.assertEqual(ww_job["role"]["workspace"], "worktree")
      self.assertEqual(ww_job["configured_role"]["workspace"], "read-only")

      # Builder role overridden to read-only rejects output_files and allow_commit.
      with self.assertRaisesRegex(ValueError, "read-only"):
        service.run(
          "s1", "company-builder", "deepseek-flash", "low", "Inspect", "Report text",
          "Downgrade to read-only", str(repo), one_shot=True, permissions="read-only", output_files=["report.md"],
        )
      with self.assertRaisesRegex(ValueError, "write worktree"):
        service.run(
          "s1", "company-builder", "deepseek-flash", "low", "Inspect", "Report text",
          "Downgrade to read-only", str(repo), one_shot=True, permissions="read-only",
          allow_commit=True, commit_message="msg", commit_paths=["src.txt"],
        )
      ro_receipt = service.run(
        "s1", "company-builder", "deepseek-flash", "low", "Inspect src.txt", "Report text",
        "Downgrade to read-only", str(repo), one_shot=True, permissions="read-only",
      )
      ro_job = st.job(ro_receipt["id"])
      self.assertEqual(ro_job["permissions"], "read-only")
      self.assertEqual(ro_job["role"]["workspace"], "read-only")

      # workspace-shell satisfies requires_shell=True and external required_files outside cwd.
      ws_receipt = service.run(
        "s1", "company-builder", "deepseek-flash", "low", "Run tests per brief", "Tests pass",
        "Parent chose workspace-shell", str(repo), one_shot=True, permissions="workspace-shell",
        required_files=[str(ext_brief)],
        task_profile={"kind": "implementation", "scope": "bounded", "uncertainty": "low",
                      "risk": "low", "validation": "tests"},
      )
      ws_job = st.job(ws_receipt["id"])
      self.assertEqual(ws_job["permissions"], "workspace-shell")
      self.assertEqual(ws_job["execution_environment"]["profile"], "workspace-shell")
      self.assertTrue(ws_job["execution_environment"]["shell_access"])

    prepared_wt = Path(workspaces.prepare(ww_job))
    self.assertNotEqual(prepared_wt, repo)

    with patch("service.catalog") as catalog_mock, patch("service.subprocess.Popen") as spawn_mock:
      with self.assertRaisesRegex(ValueError, "permissions must be"):
        service.run("s1", "company-builder", "deepseek-flash", "low", "Bad", "Bad", "Bad",
                    str(repo), one_shot=True, permissions="admin-root")
      catalog_mock.assert_not_called()
      spawn_mock.assert_not_called()

    # Role-level permissions validation in configure and persistent_tasks run_spec validation.
    with self.assertRaisesRegex(ValueError, "match workspace"):
      service.configure("agents", "bad-role", {
        "provider": "company", "enabled": True, "automatic": True, "workspace": "read-only",
        "permissions": "workspace-write", "instructions": "Bad",
      })
    service.configure("agents", "shell-role", {
      "provider": "company", "enabled": True, "automatic": True, "workspace": "worktree",
      "permissions": "workspace-shell", "instructions": "Edit and test.",
    })
    with patch("service.catalog", return_value=self.catalog), \
         patch("service.quota", return_value={"status": "unknown"}), \
         patch("credentials.environment", return_value={}), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      inherited = service.run(
        "s1", "shell-role", "deepseek-flash", "low", "Run tests", "Tests pass",
        "Use configured role default", str(repo), one_shot=True,
        task_profile={"kind": "implementation", "scope": "bounded", "uncertainty": "low",
                      "risk": "low", "validation": "tests"},
      )
      self.assertEqual(st.job(inherited["id"])["permissions"], "workspace-shell")

    with patch("persistent_tasks.ensure_controller"):
      loop = persistent_tasks.manage("s-loop", action="start", one_shot=True, run_spec={
        "agent": "company-builder", "model": "deepseek-flash", "effort": "low",
        "task": "Loop edit", "acceptance": "Done", "rationale": "Test", "cwd": str(repo),
        "permissions": "workspace-shell",
      })
      self.assertEqual(loop["status"], "ready")
    st.write("loop-lold.json", {
      "id": "lold", "session_id": "s-loop", "mode": "persistent", "status": "paused",
      "requested": "pause", "task_id": loop["task_id"], "max_cycles": 3, "interval_seconds": 60,
      "token_budget": 0, "cycle": 1, "attempt": 1, "completed_cycles": 0, "one_shot": True,
      "authorized_agents": ["company-builder"], "mode_revision": 0, "trigger_file": "", "stop_file": "",
      "event_seq": 0, "ack_seq": 0, "goal_acceptance": "Done", "current_job": None,
      "runtime_image": {"path": str(self.root / "old-img"), "digest": "0" * 64},
      "run_spec": {"agent": "company-builder", "model": "deepseek-flash", "effort": "low",
                   "task": "Loop edit", "acceptance": "Done", "rationale": "Test", "cwd": str(repo)},
    })
    with patch("persistent_tasks.runtime_image.job_version", return_value="0.26.4"):
      with self.assertRaisesRegex(ValueError, "predates"):
        persistent_tasks.manage("s-loop", action="resume", loop_id="lold",
                                run_spec={"permissions": "workspace-shell"})

    with self.assertRaisesRegex(ValueError, "permissions must be"):
      persistent_tasks.validate_spec("s1", {
        "agent": "company-builder", "model": "deepseek-flash", "effort": "low",
        "task": "Loop", "acceptance": "Done", "rationale": "Test", "cwd": str(repo), "permissions": "bad",
      }, one_shot=True, mode="persistent")

  def test_optimizer_plan_workspace_shell_and_pool_override(self) -> None:
    service.control("s", "on", strategy="preserve-claude")
    codex_models = {"models": [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [
      {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]}]}
    def fake_catalog(name: str, **kwargs: Any) -> dict[str, Any]:
      return codex_models if name == "codex" else self.catalog
    def fake_quota(name: str, **kwargs: Any) -> dict[str, Any]:
      if name == "codex":
        return {"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 100}}}}
      return {"status": "unknown"}
    daily = {"kind": "implementation", "scope": "bounded", "uncertainty": "medium",
             "risk": "medium", "validation": "tests"}

    with patch("service.catalog", side_effect=fake_catalog), patch("service.quota", side_effect=fake_quota):
      blocked = optimizer.plan("s", operation="write", task_profile=daily, permissions="auto")
      self.assertEqual(blocked["action"], "replan")

      planned = optimizer.plan("s", task_profile=daily, permissions="workspace-shell")
      self.assertEqual(planned["action"], "delegate")
      self.assertEqual(planned["agent"], "company-builder")
      self.assertEqual(planned["permissions"], "workspace-shell")
      self.assertEqual(planned["execution_profile"], "workspace-shell")

      inspect_only = {**daily, "validation": "inspection"}
      planned_inspect = optimizer.plan("s", work_item="inspect-shell", task_profile=inspect_only,
                                       permissions="workspace-shell")
      self.assertEqual(planned_inspect["permissions"], "workspace-shell")

    with patch("service.catalog", side_effect=fake_catalog), patch("service.quota", side_effect=fake_quota), \
         patch("credentials.environment", return_value={}), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      with self.assertRaisesRegex(ValueError, "file tools only"):
        service.run("s", planned["agent"], planned["model"], planned["effort"], "Fix bug", "Tests pass",
                    "Use plan", str(self.root), plan_id=planned["id"], work_item=planned["work_item"],
                    permissions="workspace-write")
      with self.assertRaisesRegex(ValueError, "differ from this plan"):
        service.run("s", planned_inspect["agent"], planned_inspect["model"], planned_inspect["effort"],
                    "Edit doc", "Checked", "Use plan", str(self.root), plan_id=planned_inspect["id"],
                    work_item=planned_inspect["work_item"], permissions="workspace-write")
      dispatched = service.run("s", planned["agent"], planned["model"], planned["effort"], "Fix bug", "Tests pass",
                               "Use plan", str(self.root), plan_id=planned["id"], work_item=planned["work_item"])
      self.assertEqual(dispatched["permissions"], "workspace-shell")

    # Session pool restricted to reviewer can still be overridden by explicit workspace-write.
    service.control("s2", "on", agents=["company-reviewer"], strategy="preserve-claude")
    with patch("service.catalog", side_effect=fake_catalog), patch("service.quota", side_effect=fake_quota):
      overridden = optimizer.plan(
        "s2",
        task_profile={"kind": "documentation", "scope": "bounded", "uncertainty": "low",
                      "risk": "low", "validation": "inspection"},
        permissions="workspace-write",
      )
      self.assertEqual(overridden["action"], "delegate")
      self.assertEqual(overridden["agent"], "company-reviewer")
      self.assertEqual(overridden["permissions"], "workspace-write")

  def test_resume_inherits_overridden_permissions_and_guards_workspace_switch(self) -> None:
    with patch("service.catalog", return_value=self.catalog), \
         patch("service.quota", return_value={"status": "unknown"}), \
         patch("credentials.environment", return_value={}), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      first = service.run("s", "company-reviewer", "deepseek-flash", "low", "Edit", "Done", "Why",
                          str(self.root), one_shot=True, permissions="workspace-write")
      wt_dir = self.root / "wt-1"
      wt_dir.mkdir()
      st.update_job(first["id"], status="completed", thread_id="thr1", finished_at=time.time(), cwd=str(wt_dir))

      resumed_auto = service.run("s", "company-reviewer", "deepseek-flash", "low", "Continue", "Done", "Why",
                                 str(self.root), one_shot=True, resume_job=first["id"], permissions="auto")
      self.assertEqual(st.job(resumed_auto["id"])["permissions"], "workspace-write")
      st.update_job(resumed_auto["id"], status="completed", finished_at=time.time())

      upgraded = service.run("s", "company-reviewer", "deepseek-flash", "low", "Run check", "Done", "Why",
                             str(self.root), one_shot=True, resume_job=resumed_auto["id"],
                             permissions="workspace-shell")
      self.assertEqual(st.job(upgraded["id"])["permissions"], "workspace-shell")
      st.update_job(upgraded["id"], status="completed", finished_at=time.time())

      with self.assertRaisesRegex(ValueError, "read-only source cwd and an isolated worktree"):
        service.run("s", "company-reviewer", "deepseek-flash", "low", "Downgrade", "Done", "Why",
                    str(self.root), one_shot=True, resume_job=upgraded["id"], permissions="read-only")

      ro_first = service.run("s", "company-reviewer", "deepseek-flash", "low", "Read", "Done", "Why",
                             str(self.root), one_shot=True, permissions="read-only")
      st.update_job(ro_first["id"], status="completed", thread_id="thr2", finished_at=time.time())
      with self.assertRaisesRegex(ValueError, "read-only source cwd and an isolated worktree"):
        service.run("s", "company-reviewer", "deepseek-flash", "low", "Upgrade", "Done", "Why",
                    str(self.root), one_shot=True, resume_job=ro_first["id"], permissions="workspace-write")

  def test_claude_worker_structured_json_redaction_and_local_preflight(self) -> None:
    fake = self.root / "fake-claude-ok"
    fake.write_text(
      "#!/usr/bin/env python3\n"
      "import json\n"
      "print(json.dumps({\n"
      "  'result': 'Edited cleanly; leak=secret-tok-xyz',\n"
      "  'modelUsage': {'deepseek-flash': {'contextWindow': 128000}},\n"
      "  'permission_denials': [{'tool_name': 'Edit', 'reason': 'fixture'}],\n"
      "  'session_id': 'cli-sess-1',\n"
      "  'is_error': False,\n"
      "}))\n"
    )
    fake.chmod(0o700)
    jid = "j" + "8" * 16
    job = {
      "id": jid,
      "session_id": "s_red",
      "status": "running",
      "provider": {"kind": "anthropic", "command": str(fake), "base_url": "https://example.invalid",
                   "api_key_env": "TEST_API_TOKEN"},
      "provider_name": "company",
      "role": {"workspace": "worktree", "instructions": "Edit."},
      "permissions": "workspace-write",
      "model": "deepseek-flash",
      "effort": "low",
      "cwd": str(self.root),
      "source_cwd": str(self.root),
      "timeout_seconds": 30,
      "cancel_requested": False,
    }
    st.write("job-" + jid + ".json", job)
    with patch.dict(os.environ, {"TEST_API_TOKEN": "secret-tok-xyz", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000"}):
      outcome = worker.claude_worker(job, "Edit file")
    self.assertEqual(outcome["status"], "completed")
    self.assertEqual(outcome["model_usage"]["deepseek-flash"]["contextWindow"], 128000)
    self.assertNotIn("secret-tok-xyz", outcome["result"])
    self.assertIn("[REDACTED]", outcome["result"])
    self.assertEqual(len(outcome["permission_denials"]), 1)
    saved_output = json.loads((st.root() / f"output-{jid}.json").read_text())
    self.assertEqual(saved_output["modelUsage"]["deepseek-flash"]["contextWindow"], 128000)
    self.assertIn("[REDACTED]", saved_output["result"])

    summary = service.summary({**job, **outcome})
    self.assertEqual(summary["permissions"], "workspace-write")
    self.assertEqual(len(summary["permission_denials"]), 1)
    bad_summary = service.summary({**job, **outcome, "permission_denials": 42})
    self.assertNotIn("permission_denials", bad_summary)

    # Blocked local_preflight prevents cli_history.restore and Popen.
    env_spec = capabilities.select(
      self.cfg["agents"]["company-builder"], self.cfg["providers"]["company"],
      {"network": True, "network_targets": [{"host": "127.0.0.1", "port": 1}]},
      permissions="workspace-shell",
    )
    jid2 = "j" + "9" * 16
    job2 = {
      "id": jid2,
      "session_id": "s_red",
      "agent": "company-builder",
      "provider": self.cfg["providers"]["company"],
      "provider_name": "company",
      "role": capabilities.effective_role(self.cfg["agents"]["company-builder"],
                                            self.cfg["providers"]["company"], "workspace-shell"),
      "configured_role": self.cfg["agents"]["company-builder"],
      "permissions": "workspace-shell",
      "execution_environment": env_spec,
      "cwd": str(self.root),
      "source_cwd": str(self.root),
      "model": "deepseek-flash",
      "effort": "low",
      "timeout_seconds": 30,
      "cancel_requested": False,
    }
    st.write("job-" + jid2 + ".json", job2)
    with patch("capabilities.local_preflight", return_value={"status": "blocked", "reason": "TCP refused"}), \
         patch("cli_history.restore") as restore_mock, patch("worker.subprocess.Popen") as popen_mock:
      blocked_outcome = worker.claude_worker(job2, "Run check")
    self.assertEqual(blocked_outcome["status"], "failed")
    self.assertIn("TCP refused", blocked_outcome["error"])
    restore_mock.assert_not_called()
    popen_mock.assert_not_called()

  def test_mcp_schema_and_control_advertise_permissions(self) -> None:
    tools = {t["name"]: t for t in delegate.tool_list()}
    for name in ("delegate_run", "delegate_plan"):
      self.assertEqual(
        tools[name]["inputSchema"]["properties"]["permissions"]["enum"],
        ["auto", "read-only", "workspace-write", "workspace-shell", "network-shell", "full-access"],
      )
      self.assertIn("writable_roots", tools[name]["inputSchema"]["properties"])
      self.assertIn("network_access", tools[name]["inputSchema"]["properties"])
    ctrl = service.control("s-ctrl")
    self.assertIn("subagent-permissions", ctrl["server_runtime"]["capabilities"])
    self.assertIn("parent-sandbox-controls-v26-7", ctrl["server_runtime"]["capabilities"])
    self.assertEqual(
      ctrl["subagent_permissions"]["available"],
      ["auto", "read-only", "workspace-write", "workspace-shell", "network-shell", "full-access"],
    )

  def test_parent_controlled_network_full_access_and_writable_roots(self) -> None:
    """Verify network-shell, full-access, writable_roots, and network_access controls."""
    import input_contract
    import runtime_gate
    shared = (self.root / "shared-data").resolve()
    shared.mkdir()
    outside = (self.root / "outside-unapproved").resolve()
    outside.mkdir()

    # Codex builder on DEFAULT_CONFIG (no execution_profiles configured) gets network or dangerFullAccess.
    codex_role = copy.deepcopy(st.DEFAULT_CONFIG["agents"]["codex-builder"])
    codex_prov = self.cfg["providers"]["codex"]

    net_spec = capabilities.select(
      codex_role, codex_prov, None, permissions="network-shell", writable_roots=[str(shared)],
    )
    self.assertEqual(net_spec["profile"], "network")
    self.assertTrue(net_spec["network_access"])
    self.assertEqual(net_spec["writable_roots"], [str(shared)])
    codex_job = {
      "id": "jcodexnet", "session_id": "s1", "agent": "codex-builder", "provider_name": "codex",
      "provider": codex_prov, "role": codex_role, "configured_role": codex_role,
      "cwd": str(self.root), "source_cwd": str(self.root), "permissions": "network-shell",
      "writable_roots": [str(shared)], "execution_environment": net_spec, "timeout_seconds": 300,
    }
    self.assertEqual(
      capabilities.policy(codex_job),
      {"type": "workspaceWrite", "writableRoots": [str(self.root), str(shared)], "networkAccess": True},
    )

    full_spec = capabilities.select(codex_role, codex_prov, None, permissions="full-access")
    self.assertEqual(full_spec["profile"], "host")
    self.assertFalse(full_spec["filesystem_isolation"])
    self.assertEqual(
      capabilities.policy({**codex_job, "permissions": "full-access", "execution_environment": full_spec}),
      {"type": "dangerFullAccess"},
    )

    # Read-only Codex reviewer with explicit network_access=True keeps readOnly sandbox + networkAccess.
    ro_role = copy.deepcopy(st.DEFAULT_CONFIG["agents"]["codex-reviewer"])
    ro_net = capabilities.select(ro_role, codex_prov, None, permissions="read-only", network_access=True)
    ro_policy = capabilities.policy({
      **codex_job, "agent": "codex-reviewer", "role": ro_role, "configured_role": ro_role,
      "permissions": "read-only", "writable_roots": [], "execution_environment": ro_net,
    })
    self.assertEqual(ro_policy, {"type": "readOnly", "networkAccess": True})

    # writable_roots conflicts with read-only.
    with self.assertRaisesRegex(ValueError, "read-only"):
      capabilities.resolve_permissions(codex_role, codex_prov, "read-only", writable_roots=[str(shared)])

    # Output contract accepts shared writable_roots or full-access, rejects unapproved external paths.
    sub_cwd = (self.root / "sub-cwd").resolve()
    sub_cwd.mkdir()
    input_contract.check(
      codex_prov, "worktree", str(sub_cwd), [], [str(shared / "out.parquet")],
      writable_roots=[str(shared)],
    )
    with self.assertRaisesRegex(ValueError, "writable_roots"):
      input_contract.check(
        codex_prov, "worktree", str(sub_cwd), [], [str(outside / "bad.parquet")],
        writable_roots=[str(shared)],
      )
    input_contract.check(
      codex_prov, "worktree", str(sub_cwd), [], [str(outside / "ok.parquet")],
      permissions="full-access", unrestricted_fs=True,
    )

    # Non-Codex (DeepSeek) network-shell + writable_roots adds --add-dir and grant instructions.
    ds_role = self.cfg["agents"]["company-builder"]
    ds_prov = self.cfg["providers"]["company"]
    ds_spec = capabilities.select(
      ds_role, ds_prov, None, permissions="network-shell", writable_roots=[str(shared)],
    )
    ds_job = {
      "id": "jdsnet", "session_id": "s1", "agent": "company-builder", "provider_name": "company",
      "provider": ds_prov, "role": ds_role, "configured_role": ds_role,
      "model": "deepseek-flash", "effort": "low",
      "cwd": str(self.root), "source_cwd": str(self.root), "permissions": "network-shell",
      "writable_roots": [str(shared)], "execution_environment": ds_spec, "timeout_seconds": 300,
      "task": "Sync shared data", "acceptance": "Written", "context": "",
    }
    with patch.dict(os.environ, {"TEST_DS_KEY": "dummy"}):
      ds_prov_key = {**ds_prov, "api_key_env": "TEST_DS_KEY"}
      ds_job["provider"] = ds_prov_key
      cmd, _ = worker.claude_command(ds_job)
      self.assertIn("--add-dir", cmd)
      self.assertIn(str(shared), cmd)
      pkt = worker.packet(ds_job)
      self.assertIn("PERMISSION GRANT", pkt)
      self.assertIn("WRITABLE SHARED ROOTS", pkt)

    # Auto planning + service.run + resume upgrade on Codex.
    codex_models = {"models": [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [
      {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]}]}
    service.control("s-net", "on", strategy="preserve-claude")
    with patch("service.catalog", return_value=codex_models), \
         patch(
           "service.quota",
           return_value={"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 10}}}},
         ), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      planned = optimizer.plan(
        "s-net", work_item="sync-gitlab", permissions="network-shell",
        writable_roots=[str(shared)], network_access=True, cwd=str(self.root),
        task_profile={"kind": "implementation", "scope": "bounded", "uncertainty": "medium",
                      "risk": "medium", "validation": "tests"},
      )
      self.assertEqual(planned["action"], "delegate")
      self.assertEqual(planned["execution_profile"], "network")
      self.assertEqual(planned["writable_roots"], [str(shared)])
      self.assertTrue(planned["network_access"])

      dispatched = service.run(
        "s-net", "auto", "auto", "auto", "Write shared file", "Verified", "Need shared write",
        str(self.root), permissions="network-shell", writable_roots=[str(shared)],
        network_access=True, output_files=[str(shared / "artifact.json")],
        task_profile={"kind": "implementation", "scope": "bounded", "uncertainty": "medium",
                      "risk": "medium", "validation": "tests"},
      )
      self.assertEqual(dispatched["permissions"], "network-shell")
      self.assertEqual(dispatched["writable_roots"], [str(shared)])
      st.update_job(dispatched["id"], status="completed", thread_id="thr-up", finished_at=time.time())

      upgraded = service.run(
        "s-net", "codex-builder", "gpt-6.1-sol", "medium", "Push tag or run unrestricted", "Done",
        "Parent grants full-access", str(self.root), resume_job=dispatched["id"], permissions="full-access",
      )
      self.assertEqual(st.job(upgraded["id"])["execution_environment"]["profile"], "host")

    # runtime_gate requires >=0.26.7 for writable_roots / network-shell / full-access.
    sess = st.session("s-gate")
    sess["runtime_handshakes"] = {"mcp__delegate": {
      "version": "0.26.6",
      "capabilities": ["model-deny", "sol-first", "snapshot-required-files"],
      "identity": {"pid": os.getpid(), "start": "0", "boot": "0"},
    }}
    st.save_session("s-gate", sess)
    with patch("runtime_gate.live_identity", return_value=True):
      denied = runtime_gate.rejection({
        "session_id": "s-gate",
        "tool_name": "mcp__delegate__delegate_run",
        "tool_input": {"session_id": "s-gate", "writable_roots": [str(shared)]},
      })
      self.assertIn("0.26.7", denied)
      sess["runtime_handshakes"]["mcp__delegate"]["version"] = "0.26.7"
      st.save_session("s-gate", sess)
      self.assertEqual(runtime_gate.rejection({
        "session_id": "s-gate",
        "tool_name": "mcp__delegate__delegate_run",
        "tool_input": {"session_id": "s-gate", "permissions": "full-access", "writable_roots": [str(shared)]},
      }), "")

  def test_validator_regression_edge_cases_for_v26_7(self) -> None:
    """Cover non-Codex output confinement, resume downgrades, explicit work_item, and plan binding."""
    sub_cwd = (self.root / "work-cwd").resolve()
    sub_cwd.mkdir()
    shared = (self.root / "shared-out").resolve()
    shared.mkdir()
    other = (self.root / "other-out").resolve()
    other.mkdir()
    codex_models = {"models": [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [
      {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]}]}
    quota_ok = {"status": "known", "data": {"rateLimits": {"primary": {"usedPercent": 10}}}}

    with patch("service.catalog", side_effect=lambda name, **kw: codex_models if name == "codex" else self.catalog), \
         patch("service.quota", return_value=quota_ok), \
         patch("credentials.environment", return_value={}), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())) as spawn:
      # 1. Non-Codex workspace-write/workspace-shell/network-shell must reject unapproved outside output_files.
      for perm in ("workspace-write", "workspace-shell", "network-shell"):
        with self.assertRaisesRegex(ValueError, "writable_roots"):
          service.run(
            "s-reg", "company-builder", "deepseek-flash", "low", "Write outside", "Done", "Check",
            str(sub_cwd), one_shot=True, permissions=perm, output_files=[str(other / "leak.txt")],
          )
      spawn.assert_not_called()
      full_res = service.run(
        "s-reg", "company-builder", "deepseek-flash", "low", "Write outside", "Done", "Check",
        str(sub_cwd), one_shot=True, permissions="full-access", output_files=[str(other / "ok.txt")],
      )
      self.assertEqual(full_res["permissions"], "full-access")

    # 2. Resume downgrades: network-shell -> workspace-shell and network_access=True -> False on Codex.
    with patch("service.catalog", return_value=codex_models), \
         patch("service.quota", return_value=quota_ok), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      first = service.run(
        "s-down", "codex-builder", "gpt-6.1-sol", "medium", "Sync repo", "Synced", "Need net",
        str(sub_cwd), one_shot=True, permissions="network-shell", network_access=True,
      )
      self.assertTrue(st.job(first["id"])["execution_environment"]["network_access"])
      st.update_job(first["id"], status="completed", thread_id="thr-down", finished_at=time.time())

      downgraded = service.run(
        "s-down", "codex-builder", "gpt-6.1-sol", "medium", "Local tests", "Pass", "Drop net",
        str(sub_cwd), one_shot=True, resume_job=first["id"], permissions="workspace-shell", network_access=False,
      )
      self.assertFalse(st.job(downgraded["id"])["execution_environment"]["network_access"])
      self.assertEqual(st.job(downgraded["id"])["execution_environment"]["profile"], "sandboxed")

    # 3. claude_command includes --add-dir for writable_roots even under workspace-write.
    with patch.dict(os.environ, {"TEST_DS_KEY": "dummy"}):
      ds_ww = {
        "provider": {**self.cfg["providers"]["company"], "api_key_env": "TEST_DS_KEY"},
        "provider_name": "company",
        "model": "deepseek-flash",
        "effort": "low",
        "cwd": str(sub_cwd),
        "source_cwd": str(sub_cwd),
        "permissions": "workspace-write",
        "writable_roots": [str(shared)],
        "role": {"workspace": "worktree", "instructions": "Edit."},
        "timeout_seconds": 300,
      }
      cmd_ww, _ = worker.claude_command(ds_ww)
      self.assertIn("--add-dir", cmd_ww)
      self.assertIn(str(shared), cmd_ww)

    # 4. Explicit agent dispatch includes writable_roots in default_work_item.
    with patch("service.catalog", return_value=codex_models), \
         patch("service.quota", return_value=quota_ok), \
         patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      j_a = service.run(
        "s-wi", "codex-builder", "gpt-6.1-sol", "medium", "Same task", "Done", "Why",
        str(sub_cwd), one_shot=True, permissions="workspace-shell", writable_roots=[str(shared)],
      )
      j_b = service.run(
        "s-wi", "codex-builder", "gpt-6.1-sol", "medium", "Same task", "Done", "Why",
        str(sub_cwd), one_shot=True, permissions="workspace-shell", writable_roots=[str(other)],
      )
      self.assertNotEqual(j_a["work_item"], j_b["work_item"])

    # 5. Non-Codex workspace-shell rejects network_access=False.
    with self.assertRaisesRegex(ValueError, "network_access=false"):
      capabilities.select(
        self.cfg["agents"]["company-builder"], self.cfg["providers"]["company"],
        None, permissions="workspace-shell", network_access=False,
      )

    # 6. Legacy unexpired plan without writable_roots fails closed on new writable_roots dispatch.
    old_plan = {
      "id": "pold266", "action": "delegate", "session_id": "s-plan", "task_id": "t-plan",
      "agent": "codex-builder", "model": "gpt-6.1-sol", "effort": "medium", "work_item": "w1",
      "complexity": "standard", "category": "implementation", "created_at": time.time(),
      "cwd": str(sub_cwd), "required_files": [], "output_files": [],
      "requirements": capabilities.normalize(None), "execution_profile": "sandboxed", "permissions": "workspace-shell",
    }
    st.write("plan-pold266.json", old_plan)
    with patch("service.catalog", return_value=codex_models), \
         patch("service.quota", return_value=quota_ok), \
         patch("service.subprocess.Popen") as spawn2:
      with self.assertRaisesRegex(ValueError, "Plan predates parent sandbox controls"):
        service.run(
          "s-plan", "codex-builder", "gpt-6.1-sol", "medium", "Use old plan", "Done", "Why",
          str(sub_cwd), one_shot=True, plan_id="pold266", work_item="w1", writable_roots=[str(shared)],
        )
      spawn2.assert_not_called()


if __name__ == "__main__":
  unittest.main()

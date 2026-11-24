"""Unit tests for Antigravity (agy) worker provider, fast CLI subcommands, and multi-profile host installer."""
from pathlib import Path
from typing import Any
from unittest.mock import patch
import contextlib
import copy
import io
import json
import os
import runpy
import shlex
import stat
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agy_worker
import cli
import delegate
import hosts
import storage as st
import worker

INSTALLER_API = runpy.run_path(str(REPO / "scripts" / "install-hosts.py"))


class AgyWorkerTests(unittest.TestCase):
  """Verify Antigravity catalog caching, CLI execution/permissions, and fast CLI ergonomics."""

  def setUp(self) -> None:
    """Create isolated temporary state, config, and host directories."""
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-agy-test-")
    self.root = Path(self.temp.name)
    self.state_dir = self.root / "state"
    self.env = patch.dict(
      os.environ,
      {
        "DELEGATE_STATE_DIR": str(self.state_dir),
        "CODEX_HOME": str(self.root / "codex"),
        "DSH_HOME": str(self.root / "dsh"),
        "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
        "GEMINI_CONFIG_DIR": str(self.root / "gemini" / "config"),
      },
    )
    self.env.start()
    for var in (
      "DELEGATE_CALLER_HOST",
      "DELEGATE_WORKER",
      "ANTIGRAVITY_CONVERSATION_ID",
      "CODEX_THREAD_ID",
      "CLAUDE_SESSION_ID",
      "DSH_SESSION_ID",
    ):
      os.environ.pop(var, None)
    agy_worker.clear_catalog_cache()
    cfg = copy.deepcopy(st.DEFAULT_CONFIG)
    cfg["providers"]["antigravity"] = {
      "kind": "antigravity",
      "enabled": True,
      "command": "agy",
      "models": [],
      "efforts": list(agy_worker.DEFAULT_EFFORTS),
    }
    cfg["agents"]["agy-advisor"] = {
      "provider": "antigravity",
      "enabled": True,
      "automatic": False,
      "workspace": "read-only",
      "instructions": "Provide independent architecture and UX advice.",
    }
    cfg["agents"]["agy-builder"] = {
      "provider": "antigravity",
      "enabled": True,
      "automatic": False,
      "workspace": "worktree",
      "instructions": "Implement requested changes cleanly.",
    }
    cfg["providers"]["deepseek"]["enabled"] = True
    cfg["agents"]["deepseek-docs"]["enabled"] = True
    st.write("config.json", cfg)

  def tearDown(self) -> None:
    """Restore environment variables and remove temporary state directory."""
    agy_worker.clear_catalog_cache()
    self.env.stop()
    self.temp.cleanup()

  def _make_fake_agy(self, script_body: str) -> Path:
    """Create an executable mock ``agy`` script inside ``self.root``."""
    path = self.root / "bin" / "agy"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path

  def test_catalog_parses_spinner_output_and_caches_in_memory_and_disk(self) -> None:
    """Verify ``catalog`` parses ``agy models`` and spawns 0 subprocesses across memory + disk TTL hits."""
    counter_file = self.root / "models_count.txt"
    fake_agy = self._make_fake_agy(
      "from pathlib import Path\n"
      "import sys\n"
      f"counter = Path({str(counter_file)!r})\n"
      "n = int(counter.read_text()) if counter.exists() else 0\n"
      "counter.write_text(str(n + 1))\n"
      "if sys.argv[1:] == ['models']:\n"
      "  sys.stdout.write('\\r\\u280b Fetching available models...\\r\\u2819 Fetching available models...\\r')\n"
      "  sys.stdout.write('gemini-4-neon-high             Gemini 4 Neon (High)\\n')\n"
      "  sys.stdout.write('gemini-4-carbon-medium         Gemini 4 Carbon (Medium)\\n')\n"
      "  sys.stdout.write('gemini-3.8-flash-low           Gemini 3.8 Flash (Low)\\n')\n"
    )
    provider: dict[str, Any] = {
      "kind": "antigravity",
      "enabled": True,
      "command": str(fake_agy),
      "models": [],
      "efforts": list(agy_worker.DEFAULT_EFFORTS),
    }

    first = agy_worker.catalog(provider)
    self.assertEqual(first["provider_kind"], "antigravity")
    self.assertEqual(
      first["models"],
      ["gemini-4-neon-high", "gemini-4-carbon-medium", "gemini-3.8-flash-low"],
    )
    self.assertEqual(first["default_model"], "gemini-4-neon-high")
    self.assertEqual(first["efforts"], ["low", "medium", "high", "xhigh", "max"])
    self.assertEqual(first["source"], "agy models")
    self.assertEqual(first["model_details"][0]["label"], "Gemini 4 Neon (High)")
    self.assertEqual(int(counter_file.read_text()), 1)

    second = agy_worker.catalog(provider)
    self.assertEqual(second["models"], first["models"])
    self.assertEqual(int(counter_file.read_text()), 1)

    agy_worker.clear_catalog_cache()
    third = agy_worker.catalog(provider)
    self.assertEqual(third["models"], first["models"])
    self.assertEqual(int(counter_file.read_text()), 1)

    refreshed = agy_worker.catalog(provider, refresh=True)
    self.assertEqual(refreshed["models"], first["models"])
    self.assertEqual(int(counter_file.read_text()), 2)

  def test_catalog_falls_back_cleanly_when_binary_unavailable(self) -> None:
    """Verify ``catalog`` falls back to ``DEFAULT_MODELS`` without raising when ``agy`` is missing."""
    provider: dict[str, Any] = {
      "kind": "antigravity",
      "enabled": True,
      "command": str(self.root / "missing-agy"),
      "models": [],
      "efforts": list(agy_worker.DEFAULT_EFFORTS),
    }
    cat = agy_worker.catalog(provider)
    self.assertEqual(cat["provider_kind"], "antigravity")
    self.assertEqual(cat["models"], agy_worker.DEFAULT_MODELS)
    self.assertEqual(cat["default_model"], agy_worker.DEFAULT_MODELS[0])
    self.assertIn("unavailable", cat["source"])

  def test_run_agy_permission_mapping_resume_json_and_text_fallback(self) -> None:
    """Verify permission flags, ``--conversation`` resume, ``DELEGATE_WORKER=1``, and JSON/text parsing."""
    capture_file = self.root / "agy_invocations.jsonl"
    shared_dir = self.root / "shared-data"
    shared_dir.mkdir(parents=True)
    fake_agy = self._make_fake_agy(
      "from pathlib import Path\n"
      "import json, os, sys\n"
      f"log = Path({str(capture_file)!r})\n"
      "entry = {\n"
      "  'argv': sys.argv[1:],\n"
      "  'delegate_worker': os.environ.get('DELEGATE_WORKER'),\n"
      "  'caller_host': os.environ.get('DELEGATE_CALLER_HOST'),\n"
      "}\n"
      "with log.open('a', encoding='utf-8') as f:\n"
      "  f.write(json.dumps(entry) + '\\n')\n"
      "mode = os.environ.get('FAKE_AGY_OUTPUT_MODE', 'json')\n"
      "if mode == 'json':\n"
      "  print(json.dumps({\n"
      "    'result': 'Architecture looks solid.',\n"
      "    'conversation_id': 'agy-conv-999',\n"
      "    'usage': {'inputTokens': 120, 'outputTokens': 45},\n"
      "    'modelUsage': {'gemini-4-neon-high': {'inputTokens': 120, 'outputTokens': 45}},\n"
      "    'permission_denials': [{'tool': 'Bash'}],\n"
      "  }))\n"
      "elif mode == 'text':\n"
      "  sys.stdout.write('\\r\\u280b Thinking...\\rPlain text fallback answer\\n')\n"
      "else:\n"
      "  sys.stderr.write('boom failure\\n')\n"
      "  sys.exit(1)\n"
    )
    provider = {"kind": "antigravity", "command": str(fake_agy), "enabled": True}
    ro_agent = {"workspace": "read-only"}
    wt_agent = {"workspace": "worktree"}

    with patch.dict(os.environ, {"DELEGATE_CALLER_HOST": "codex"}):
      res_ro = agy_worker.run_agy(
        {
          "task": "Review architecture",
          "cwd": str(self.root),
          "model": "gemini-4-neon-high",
          "effort": "high",
          "permissions": "read-only",
        },
        ro_agent,
        provider,
      )
    self.assertEqual(res_ro["result"], "Architecture looks solid.")
    self.assertEqual(res_ro["cli_session"], "agy-conv-999")
    self.assertEqual(res_ro["usage"]["input_tokens"], 120)
    self.assertEqual(res_ro["usage"]["output_tokens"], 45)
    self.assertIn("gemini-4-neon-high", res_ro["model_usage"])
    self.assertEqual(res_ro["permission_denials"], [{"tool": "Bash"}])

    res_ww = agy_worker.run_agy(
      {
        "task": "Apply UX copy fix",
        "cwd": str(self.root),
        "model": "gemini-4-carbon-medium",
        "effort": "medium",
        "permissions": "workspace-write",
        "writable_roots": [str(shared_dir)],
        "cli_session": "agy-conv-999",
      },
      wt_agent,
      provider,
    )
    self.assertEqual(res_ww["cli_session"], "agy-conv-999")

    with patch.dict(os.environ, {"FAKE_AGY_OUTPUT_MODE": "text"}):
      res_ws = agy_worker.run_agy(
        {
          "task": "Run shell check",
          "cwd": str(self.root),
          "model": "gemini-4-neon-high",
          "effort": "xhigh",
          "permissions": "workspace-shell",
          "writable_roots": [str(shared_dir)],
          "cli_session": "agy-conv-resume-2",
        },
        wt_agent,
        provider,
      )
    self.assertEqual(res_ws["result"], "Plain text fallback answer")
    self.assertEqual(res_ws["cli_session"], "agy-conv-resume-2")
    self.assertEqual(res_ws["usage"], {})

    agy_worker.run_agy(
      {
        "task": "Full host task",
        "cwd": str(self.root),
        "model": "gemini-4-neon-high",
        "effort": "max",
        "permissions": "full-access",
      },
      wt_agent,
      provider,
    )

    calls = [json.loads(line) for line in capture_file.read_text(encoding="utf-8").splitlines()]
    self.assertEqual(len(calls), 4)
    for item in calls:
      self.assertEqual(item["delegate_worker"], "1")
      self.assertIsNone(item["caller_host"])
      self.assertIn("-p", item["argv"])
      self.assertIn("--output-format", item["argv"])
      self.assertIn("json", item["argv"])
      self.assertIn("--disable-slash-commands", item["argv"])

    argv_ro = calls[0]["argv"]
    self.assertEqual(argv_ro[argv_ro.index("--mode") + 1], "plan")
    self.assertIn("--sandbox", argv_ro)
    self.assertNotIn("--conversation", argv_ro)

    argv_ww = calls[1]["argv"]
    self.assertEqual(argv_ww[argv_ww.index("--mode") + 1], "accept-edits")
    self.assertIn("--sandbox", argv_ww)
    self.assertEqual(argv_ww[argv_ww.index("--add-dir") + 1], str(shared_dir))
    self.assertEqual(argv_ww[argv_ww.index("--conversation") + 1], "agy-conv-999")

    argv_ws = calls[2]["argv"]
    self.assertEqual(argv_ws[argv_ws.index("--mode") + 1], "auto")
    self.assertIn("--sandbox", argv_ws)
    self.assertEqual(argv_ws[argv_ws.index("--add-dir") + 1], str(shared_dir))
    self.assertEqual(argv_ws[argv_ws.index("--conversation") + 1], "agy-conv-resume-2")

    argv_full = calls[3]["argv"]
    self.assertIn("--dangerously-skip-permissions", argv_full)
    self.assertNotIn("--sandbox", argv_full)

    with patch.dict(os.environ, {"FAKE_AGY_OUTPUT_MODE": "error"}):
      with self.assertRaisesRegex(RuntimeError, "boom failure"):
        agy_worker.run_agy({"task": "Fail test", "cwd": str(self.root)}, ro_agent, provider)

  def test_worker_dispatches_antigravity_provider_kind(self) -> None:
    """Verify ``worker.antigravity_worker`` wires ``provider['kind'] == 'antigravity'`` to ``run_agy``."""
    job_record: dict[str, Any] = {
      "id": "jagyworker01",
      "session_id": "s1",
      "agent": "agy-advisor",
      "provider_name": "antigravity",
      "provider": {"kind": "antigravity", "command": "agy", "enabled": True},
      "role": {"workspace": "read-only", "instructions": "Advise."},
      "permissions": "read-only",
      "model": "gemini-4-neon-high",
      "effort": "high",
      "cwd": str(self.root),
      "source_cwd": str(self.root),
      "required_files": [],
      "output_files": [],
      "execution_environment": {"permissions": "read-only", "preflight": {"status": "not_requested"}},
    }
    st.write("job-jagyworker01.json", job_record)
    with patch(
      "agy_worker.run_agy",
      return_value={
        "result": "Blind second opinion ready.",
        "usage": {"input_tokens": 50, "output_tokens": 25},
        "model_usage": {},
        "cli_session": "conv-worker-77",
        "permission_denials": [],
      },
    ) as mocked_run, patch("persistent_tasks.check_child_launch"), patch("dispatch_policy.check_job"):
      outcome = worker.antigravity_worker(job_record, "PACKET PROMPT")
    mocked_run.assert_called_once()
    before_spawn = mocked_run.call_args.kwargs["before_spawn"]
    with patch("capabilities.validate_grant", side_effect=ValueError("Execution grant changed")) as validate, \
         patch("persistent_tasks.check_child_launch"), patch("dispatch_policy.check_job"), \
         patch("agy_worker.build_command", return_value=(["agy"], {}, None)), \
         patch("agy_worker.subprocess.Popen") as popen:
      with self.assertRaisesRegex(ValueError, "Execution grant changed"):
        agy_worker.run_agy(job_record | {"prompt": "Fixture"}, job_record["role"], job_record["provider"],
                           before_spawn=before_spawn)
    validate.assert_called_once()
    popen.assert_not_called()
    self.assertNotIn("inference_attempted_at", st.job(job_record["id"]))
    self.assertEqual(outcome["status"], "completed")
    self.assertEqual(outcome["result"], "Blind second opinion ready.")
    self.assertEqual(outcome["cli_session"], "conv-worker-77")
    self.assertEqual(outcome["thread_id"], "conv-worker-77")

  def test_cli_advise_compact_text_and_json_modes(self) -> None:
    """Verify ``delegate-agent advise`` runs in 0 model tokens across hosts in text and JSON formats."""
    buf_text = io.StringIO()
    with contextlib.redirect_stdout(buf_text):
      code = cli.main(
        ["advise", "Audit auth token refresh", "--host", "agy", "--workflow", "gate-review", "--files", "src/auth.py"],
        delegate.dispatch,
        delegate.tool_list,
      )
    self.assertEqual(code, 0)
    lines = [line for line in buf_text.getvalue().strip().splitlines() if line.strip()]
    self.assertGreaterEqual(len(lines), 2)
    self.assertLessEqual(len(lines), 3)
    self.assertIn("host=antigravity", lines[0])

    buf_json = io.StringIO()
    with contextlib.redirect_stdout(buf_json):
      code_json = cli.main(
        ["advise", "Small typo fix", "--host", "claude", "--lines", "12", "--json"],
        delegate.dispatch,
        delegate.tool_list,
      )
    self.assertEqual(code_json, 0)
    payload = json.loads(buf_json.getvalue())
    self.assertEqual(payload["action"], "keep_inline")

  def test_cli_ask_shorthands_session_reuse_and_compact_output(self) -> None:
    """Verify ``delegate-agent ask`` shorthand targets, deterministic session IDs, and minimal stdout."""
    dispatched: list[dict[str, Any]] = []

    def _fake_start(**kwargs: Any) -> dict[str, Any]:
      dispatched.append(kwargs)
      jid = f"jask{len(dispatched):04d}"
      job_doc = {
        "id": jid,
        "session_id": kwargs["session_id"],
        "status": "completed",
        "agent": kwargs["agent"],
        "provider_name": "codex",
        "model": kwargs["model"] if kwargs["model"] != "auto" else "gpt-6.2-sol",
        "effort": kwargs["effort"] if kwargs["effort"] != "auto" else "high",
        "result": "NO BLOCKING FINDINGS.\n- ADVISORY: clean structure.",
      }
      st.write("job-" + jid + ".json", job_doc)
      return {
        "id": jid,
        "session_id": kwargs["session_id"],
        "status": "completed",
        "agent": job_doc["agent"],
        "model": job_doc["model"],
        "effort": job_doc["effort"],
      }

    with patch.dict(os.environ, {"ANTIGRAVITY_CONVERSATION_ID": "conv-agy-fixed"}), patch(
      "service.run", side_effect=_fake_start
    ):
      out1 = io.StringIO()
      with contextlib.redirect_stdout(out1):
        rc1 = cli.main(
          [
            "ask", "codex-review", "Audit auth module",
            "--cwd", str(self.root), "--review-gate", "--files", "a.py,b.py",
          ],
          delegate.dispatch,
          delegate.tool_list,
        )
      out2 = io.StringIO()
      with contextlib.redirect_stdout(out2):
        rc2 = cli.main(
          ["ask", "gemini", "UX copy check", "--cwd", str(self.root), "--blind", "--background"],
          delegate.dispatch,
          delegate.tool_list,
        )
    self.assertEqual(rc1, 0)
    self.assertEqual(rc2, 0)
    self.assertEqual(len(dispatched), 2)
    self.assertEqual(dispatched[0]["session_id"], dispatched[1]["session_id"])
    self.assertTrue(dispatched[0]["session_id"].startswith("antigravity-"))
    self.assertEqual(dispatched[0]["agent"], "codex-reviewer")
    self.assertEqual(dispatched[0]["required_files"], ["a.py", "b.py"])
    self.assertEqual(dispatched[1]["agent"], "agy-advisor")
    self.assertTrue(dispatched[1]["background"])

    printed = out1.getvalue().splitlines()
    self.assertEqual(printed[0], "[codex-reviewer gpt-6.2-sol/xhigh jask0001]")
    self.assertEqual(printed[1], "NO BLOCKING FINDINGS.")
    self.assertIsNotNone(st.job("jask0001").get("collected_at"))

    cfg = st.config()
    for shorthand, expected in (
      ("codex", "codex-reviewer"),
      ("codex-build", "codex-builder"),
      ("agy", "agy-advisor"),
      ("claude", "claude-reviewer"),
      ("claude-build", "claude-builder"),
      ("deepseek", "deepseek-docs"),
    ):
      mapped, _ = cli.resolve_ask_target(
        shorthand,
        cfg,
        caller_host="codex",
        task="Check code",
        workflow="auto",
        files=[],
        permissions="auto",
      )
      self.assertEqual(mapped, expected)

  def test_install_hosts_multi_profile_dsh_and_launcher_env(self) -> None:
    """Verify ``--dsh-profile web,tui`` skips missing/installed profiles and exports cross-host env."""
    home = self.root / "install-home"
    dsh_home = home / ".dsh"
    web_patch = dsh_home / "profiles" / "web" / "cordis.patch.yml"
    tui_patch = dsh_home / "profiles" / "tui" / "cordis.patch.yml"
    web_patch.parent.mkdir(parents=True)
    tui_patch.parent.mkdir(parents=True)
    web_patch.write_text("# already configured\n- insert:\n    - id: delegate-host-mcp\n", encoding="utf-8")
    tui_patch.write_text("# fresh tui\n[]\n", encoding="utf-8")

    with patch.dict(
      os.environ,
      {
        "HOME": str(home),
        "DSH_HOME": str(dsh_home),
        "CODEX_HOME": str(home / "custom-codex"),
        "CLAUDE_CONFIG_DIR": str(home / "custom-claude"),
        "DELEGATE_STATE_DIR": str(home / "custom-state"),
      },
    ), patch("shutil.which", return_value="/fixture/dsh"), patch(
      "pathlib.Path.home", return_value=home
    ), patch.object(
      sys, "argv", ["install-hosts.py", "dsh", "--dsh-profile", "web,tui,missing"]
    ), contextlib.redirect_stdout(io.StringIO()):
      INSTALLER_API["main"]()

    self.assertEqual(web_patch.read_text(encoding="utf-8").count("delegate-host-mcp"), 1)
    tui_text = tui_patch.read_text(encoding="utf-8")
    self.assertIn("delegate-host-mcp", tui_text)
    self.assertIn("CODEX_HOME", tui_text)
    self.assertIn("CLAUDE_CONFIG_DIR", tui_text)
    self.assertFalse((dsh_home / "profiles" / "missing").exists())
    launcher = (home / ".local" / "bin" / "delegate-agent").read_text(encoding="utf-8")
    self.assertIn(".local/bin", launcher)
    self.assertIn("DELEGATE_STATE_DIR=", launcher)
    self.assertIn("CODEX_HOME=", launcher)
    self.assertIn("CLAUDE_CONFIG_DIR=", launcher)
    self.assertIn("host-mcp:antigravity", launcher)

  def test_ask_auto_enforces_automatic_pool_and_keep_inline_gate(self) -> None:
    """Verify ``ask auto`` skips ``automatic=False``/disabled roles and exits 0 on ``keep_inline``."""
    cfg = st.config()
    mapped_dual, rec_dual = cli.resolve_ask_target(
      "auto",
      cfg,
      caller_host="claude",
      task="Design a distributed multi-region event bus architecture across 6 services",
      workflow="dual-plan",
      files=["src/bus.py"],
      permissions="read-only",
    )
    self.assertEqual((rec_dual or {}).get("agent"), "agy-advisor")
    self.assertFalse(cfg["agents"]["agy-advisor"]["automatic"])
    self.assertEqual(mapped_dual, "codex-reviewer")
    self.assertTrue(cfg["agents"][mapped_dual]["automatic"])

    cfg_disabled = copy.deepcopy(cfg)
    cfg_disabled["providers"]["codex"]["enabled"] = False
    cfg_disabled["providers"]["claude"]["enabled"] = True
    mapped_claude, _ = cli.resolve_ask_target(
      "auto",
      cfg_disabled,
      caller_host="dsh",
      task="Audit complex module invariants across repository",
      workflow="gate-review",
      files=["src/core.py"],
      permissions="read-only",
    )
    self.assertEqual(mapped_claude, "claude-explorer")

    with patch("service.start") as mock_start, patch("service.run") as mock_run:
      buf_inline = io.StringIO()
      with contextlib.redirect_stdout(buf_inline):
        rc_inline = cli.main(
          ["ask", "auto", "Fix a typo in button label", "--host", "claude", "--cwd", str(self.root), "--lines", "5"],
          delegate.dispatch,
          delegate.tool_list,
        )
      self.assertEqual(rc_inline, 0)
      mock_start.assert_not_called()
      mock_run.assert_not_called()
      inline_lines = [ln for ln in buf_inline.getvalue().strip().splitlines() if ln.strip()]
      self.assertEqual(len(inline_lines), 2)
      self.assertIn("action=keep_inline", inline_lines[0])

      with patch(
        "cli._recommend",
        return_value={
          "action": "escalate_to_user",
          "workflow": "gate-review",
          "agent": None,
          "model": None,
          "effort": None,
          "reason": "2 review rounds reached; escalate trade-off to user.",
        },
      ):
        buf_esc = io.StringIO()
        with contextlib.redirect_stdout(buf_esc):
          rc_esc = cli.main(
            ["ask", "auto", "Re-review module", "--host", "codex", "--cwd", str(self.root)],
            delegate.dispatch,
            delegate.tool_list,
          )
      self.assertEqual(rc_esc, 0)
      mock_start.assert_not_called()
      self.assertIn("action=escalate_to_user", buf_esc.getvalue())

  def test_advise_shell_quotes_untrusted_task_metacharacters(self) -> None:
    """Verify ``advise`` single-quotes ``$VAR``, ``$(...)``, and backticks in ``next:`` hint."""
    dangerous = "Audit $SECRET $(touch /tmp/pwn) `id` flow"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
      rc = cli.main(
        ["advise", dangerous, "--host", "codex", "--workflow", "dual-plan"],
        delegate.dispatch,
        delegate.tool_list,
      )
    self.assertEqual(rc, 0)
    lines = buf.getvalue().strip().splitlines()
    next_line = lines[-1]
    self.assertTrue(next_line.startswith("next: delegate-agent ask "))
    tokens = shlex.split(next_line[len("next: "):])
    self.assertEqual(tokens[:2], ["delegate-agent", "ask"])
    self.assertEqual(tokens[3], dangerous)
    self.assertIn(shlex.quote(dangerous), next_line)

  def test_agy_structured_credential_redaction_and_per_job_readonly_spillover(self) -> None:
    """Verify credential-keyed JSON/stderr scrubbing and isolated per-job spillover on read-only."""
    raw_json = json.dumps(
      {
        "result": "Checked token flow.",
        "refresh_token": "rt_secret_not_in_env_12345",
        "accessToken": "at_secret_not_in_env_67890",
        "credentials": {"api_key": "ak_nested_secret", "password": "pw_nested"},
        "usage": {
          "input_tokens": 321,
          "output_tokens": 123,
          "inputTokens": 321,
          "outputTokens": 123,
          "cachedInputTokens": 40,
        },
        "modelUsage": {"gemini-4-neon-high": {"inputTokens": 321, "outputTokens": 123}},
      }
    )
    parsed = agy_worker.parse_output(raw_json, env={"PATH": "/usr/bin"})
    self.assertEqual(parsed["usage"]["input_tokens"], 321)
    self.assertEqual(parsed["usage"]["output_tokens"], 123)
    self.assertEqual(parsed["usage"]["cachedInputTokens"], 40)
    redacted_obj = agy_worker._redact_data(json.loads(raw_json), {"PATH": "/usr/bin"}, [])
    self.assertEqual(redacted_obj["refresh_token"], "[REDACTED]")
    self.assertEqual(redacted_obj["accessToken"], "[REDACTED]")
    self.assertEqual(redacted_obj["credentials"], "[REDACTED]")
    self.assertEqual(redacted_obj["usage"]["input_tokens"], 321)

    spill_inspect = self.root / "spill_inspect.json"
    user_writable = self.root / "user-writable"
    user_writable.mkdir(parents=True)
    fake_agy = self._make_fake_agy(
      "from pathlib import Path\n"
      "import json, os, re, stat, sys\n"
      "argv = sys.argv[1:]\n"
      "if os.environ.get('FAKE_STDERR_SECRET') == '1':\n"
      "  sys.stderr.write('Auth failed: refresh_token=1//0g_super_secret api_key=sk-abcdefghijklmnop\\n')\n"
      "  sys.exit(2)\n"
      "prompt = argv[argv.index('-p') + 1]\n"
      "m = re.search(r'from (/\\S+/prompt\\.md) first', prompt)\n"
      "spill_path = Path(m.group(1))\n"
      "spill_dir = spill_path.parent\n"
      f"Path({str(spill_inspect)!r}).write_text(json.dumps({{\n"
      "  'argv': argv,\n"
      "  'spill_dir': str(spill_dir),\n"
      "  'dir_mode': stat.S_IMODE(spill_dir.stat().st_mode),\n"
      "  'prompt_len': len(spill_path.read_text(encoding='utf-8')),\n"
      "}))\n"
      "print(json.dumps({'result': 'Spillover read ok', 'conversation_id': 'conv-spill'}))\n"
    )
    provider = {"kind": "antigravity", "command": str(fake_agy), "enabled": True}
    huge_task = "X" * 130000
    res = agy_worker.run_agy(
      {
        "task": huge_task,
        "cwd": str(self.root),
        "permissions": "read-only",
        "writable_roots": [str(user_writable)],
      },
      {"workspace": "read-only"},
      provider,
    )
    self.assertEqual(res["result"], "Spillover read ok")
    recorded = json.loads(spill_inspect.read_text(encoding="utf-8"))
    self.assertEqual(recorded["dir_mode"], 0o700)
    self.assertEqual(recorded["prompt_len"], 130000)
    self.assertIn("--add-dir", recorded["argv"])
    add_dirs = [
      recorded["argv"][i + 1] for i, tok in enumerate(recorded["argv"]) if tok == "--add-dir"
    ]
    self.assertEqual(add_dirs, [recorded["spill_dir"]])
    self.assertNotIn(str(user_writable), add_dirs)
    self.assertFalse(Path(recorded["spill_dir"]).exists())

    with patch.dict(os.environ, {"FAKE_STDERR_SECRET": "1"}):
      with self.assertRaises(RuntimeError) as ctx:
        agy_worker.run_agy({"task": "Check", "cwd": str(self.root)}, {"workspace": "read-only"}, provider)
    err_msg = str(ctx.exception)
    self.assertNotIn("1//0g_super_secret", err_msg)
    self.assertNotIn("sk-abcdefghijklmnop", err_msg)
    self.assertIn("[REDACTED]", err_msg)

  def test_round2_camelcase_auth_headers_numeric_secrets_spill_cleanup_and_review_gate(self) -> None:
    """Verify camelCase/Basic/Digest/numeric redaction, spillover error cleanup, and ``ask auto --review-gate``."""
    raw_obj = {
      "password": 123456,
      "apiKey": 987654,
      "token": "tok_abc",
      "usage": {
        "input_tokens": 120,
        "output_tokens": 45,
        "inputTokens": 120,
        "outputTokens": 45,
        "cachedInputTokens": 30,
      },
    }
    redacted_data = agy_worker._redact_data(raw_obj, {}, [])
    self.assertEqual(redacted_data["password"], "[REDACTED]")
    self.assertEqual(redacted_data["apiKey"], "[REDACTED]")
    self.assertEqual(redacted_data["token"], "[REDACTED]")
    self.assertEqual(redacted_data["usage"]["inputTokens"], 120)
    self.assertEqual(redacted_data["usage"]["input_tokens"], 120)
    self.assertEqual(redacted_data["usage"]["cachedInputTokens"], 30)

    stderr_sample = (
      "accessToken=camel_tok_1 refreshToken: camel_rt_2 idToken=idt_3 authToken=at_4 "
      "oauthToken=ot_5 bearerToken=bt_6 sessionToken=st_7 apiKey=ak_8 clientSecret=cs_9 "
      "apiSecret=as_10 privateKey=pk_11 secretKey=sk_12 password=654321\n"
      "Authorization: Basic dXNlcjpwYXNz\n"
      "Authorization: Digest username=\"alice\", response=\"deadbeef\"\n"
      "Authorization: Token synthetic_tok_99\n"
      "proxyAuthorization: Basic cHJveHk6c2VjcmV0\n"
      "setCookie: sid=cookie_secret_val; HttpOnly\n"
      "counters: input_tokens=120 inputTokens=120 cachedInputTokens=30"
    )
    scrubbed = agy_worker._redact_text(stderr_sample, {}, [])
    for leaked in (
      "camel_tok_1", "camel_rt_2", "idt_3", "at_4", "ot_5", "bt_6", "st_7",
      "ak_8", "cs_9", "as_10", "pk_11", "sk_12", "654321",
      "dXNlcjpwYXNz", "deadbeef", "synthetic_tok_99", "cHJveHk6c2VjcmV0", "cookie_secret_val",
    ):
      self.assertNotIn(leaked, scrubbed)
    self.assertIn("input_tokens=120", scrubbed)
    self.assertIn("inputTokens=120", scrubbed)
    self.assertIn("cachedInputTokens=30", scrubbed)

    spill_base = st.root() / "agy-prompts"
    with self.assertRaisesRegex(ValueError, "Unsupported worker permissions"):
      agy_worker.run_agy(
        {"task": "Z" * 130000, "cwd": str(self.root), "permissions": "bogus-perm"},
        {"workspace": "read-only"},
        {"kind": "antigravity", "command": "agy", "enabled": True},
      )
    leftover = list(spill_base.iterdir()) if spill_base.exists() else []
    self.assertEqual(leftover, [])

    captured_calls: list[dict[str, Any]] = []

    def _fake_gate_start(**kwargs: Any) -> dict[str, Any]:
      captured_calls.append(kwargs)
      jid = f"jgate{len(captured_calls):02d}"
      doc = {
        "id": jid,
        "session_id": kwargs["session_id"],
        "status": "completed",
        "agent": kwargs["agent"],
        "provider_name": "codex",
        "model": "gpt-6.2-sol",
        "effort": kwargs["effort"] if kwargs["effort"] != "auto" else "high",
        "result": "GATE PASS",
      }
      st.write("job-" + jid + ".json", doc)
      return {
        "id": jid,
        "session_id": kwargs["session_id"],
        "status": "completed",
        "agent": doc["agent"],
        "model": doc["model"],
        "effort": doc["effort"],
      }

    target_mod = "service.start" if hasattr(__import__("service"), "start") else "service.run"
    with patch(target_mod, side_effect=_fake_gate_start):
      for task_str in ("Fix a typo", "Inspect auth.py"):
        buf_gate = io.StringIO()
        with contextlib.redirect_stdout(buf_gate):
          rc_gate = cli.main(
            ["ask", "auto", task_str, "--host", "claude", "--cwd", str(self.root), "--review-gate"],
            delegate.dispatch,
            delegate.tool_list,
          )
        self.assertEqual(rc_gate, 0)
        self.assertIn("GATE PASS", buf_gate.getvalue())

    self.assertEqual(len(captured_calls), 2)
    for call_kw in captured_calls:
      self.assertEqual(call_kw["agent"], "codex-reviewer")
      self.assertEqual(call_kw["operation"], "read")
      if "workflow" in call_kw:
        self.assertEqual(call_kw["workflow"], "gate-review")
      if "review_gate" in call_kw:
        self.assertTrue(call_kw["review_gate"])


if __name__ == "__main__":
  unittest.main()

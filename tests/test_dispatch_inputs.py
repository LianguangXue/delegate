"""Fixed-role auto selection, discoverable operations and isolated draft delivery."""
from pathlib import Path
from unittest.mock import Mock, patch
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"))
import commits
import delegate
import optimizer
import service
import source_inputs
import storage as st
import workspaces
import accounting


class DispatchInputsTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-inputs-")
    self.root = Path(self.temp.name)
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.root / "state")})
    self.env.start()
    self.cfg = copy.deepcopy(st.DEFAULT_CONFIG)
    self.provider = {"kind": "anthropic", "enabled": True, "command": "fixture",
      "models": ["deepseek-flash", "deepseek-v4-pro"], "efforts": ["low", "medium", "high"]}
    self.role = {"provider": "company", "enabled": True, "automatic": True,
      "workspace": "read-only", "instructions": "Review"}
    self.cfg["providers"]["company"] = self.provider
    self.cfg["agents"]["company-reviewer"] = self.role
    st.write("config.json", self.cfg)
    self.catalog = {"models": self.provider["models"], "efforts": self.provider["efforts"]}

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def test_fixed_role_auto_auto_dispatches_without_changing_provider(self) -> None:
    with patch("service.catalog", return_value=self.catalog), patch("service.quota", return_value={"status": "unknown"}), \
        patch("credentials.environment", return_value={}), patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      result = service.run("s", "company-reviewer", "auto", "auto", "Review", "Evidence", "Bounded review", str(self.root),
        one_shot=True, complexity="standard", operation="read-only review")
    self.assertEqual((result["provider_name"], result["model"], result["effort"]), ("company", "deepseek-v4-pro", "medium"))
    self.assertEqual(result["selection"]["requested"], {"model": "auto", "effort": "auto"})
    self.assertEqual(st.session("s")["mode"], "off")

  def test_role_pair_respects_explicit_choices_allowlists_and_complex_floor(self) -> None:
    cases = [("auto", "auto", "simple", ("deepseek-flash", "low")),
      ("deepseek-flash", "auto", "standard", ("deepseek-flash", "medium")),
      ("auto", "high", "standard", ("deepseek-v4-pro", "high")),
      ("auto", "auto", "complex", ("deepseek-v4-pro", "high"))]
    for model, effort, complexity, expected in cases:
      self.assertEqual(optimizer.role_pair(self.role, self.provider, self.catalog, model, effort, complexity, "company"), expected)
    role = {**self.role, "models": ["deepseek-flash"]}
    with self.assertRaisesRegex(ValueError, "No allowed"):
      optimizer.role_pair(role, self.provider, self.catalog, "auto", "auto", "complex", "company")
    with self.assertRaisesRegex(ValueError, "No allowed"):
      optimizer.role_pair(self.role, self.provider, self.catalog, "nonexistent", "auto", "simple", "company")

  def test_off_and_manual_role_permissions_are_checked_before_discovery(self) -> None:
    with patch("service.catalog") as catalog:
      with self.assertRaisesRegex(ValueError, "off/manual"):
        service.run("s", "company-reviewer", "auto", "auto", "Review", "Evidence", "Why", str(self.root))
    catalog.assert_not_called()

  def test_same_tier_auto_respects_catalog_order_and_retry_effort(self) -> None:
    data = {"models": [{"model": m, "supportedReasoningEfforts": [{"reasoningEffort": e}
      for e in ("low", "medium", "high")]} for m in ("gpt-6.1-sol", "gpt-5.6-sol")]}
    self.assertEqual(optimizer.role_pair(self.role, {"kind": "codex"}, data, "auto", "auto", "standard", "codex"),
      ("gpt-6.1-sol", "medium"))
    prior = {"model": "deepseek-v4-pro", "effort": "high", "review": "needs-work"}
    self.assertEqual(optimizer.role_pair(self.role, self.provider, self.catalog, "auto", "auto", "simple", "company", prior),
      ("deepseek-v4-pro", "high"))

  def test_parent_complexity_inherited_for_fixed_role_auto_selection(self) -> None:
    accounting.begin("s", complexity="complex")
    with patch("service.catalog", return_value=self.catalog), patch("service.quota", return_value={"status": "unknown"}), \
        patch("credentials.environment", return_value={}), patch("service.subprocess.Popen", return_value=Mock(pid=os.getpid())):
      result = service.run("s", "company-reviewer", "auto", "auto", "Review", "Evidence", "Why", str(self.root), one_shot=True)
    self.assertEqual((result["complexity"], result["model"], result["effort"]), ("complex", "deepseek-v4-pro", "high"))

  def test_catalog_exposes_all_roles_but_scopes_model_lists(self) -> None:
    with patch("service._catalog", return_value={"models": []}) as discovery:
      overview = service.catalog()
      self.assertIn("company-reviewer", [r["name"] for r in overview["agents"]])
      self.assertNotIn("company-reviewer", [r["name"] for r in overview["selected_provider_agents"]])
      company = next(p for p in overview["configured_providers"] if p["name"] == "company")
      self.assertEqual(company["models"], self.provider["models"])
      service.catalog(agent_name="company-reviewer")
      discovery.assert_called_with("company", False)

  def test_operation_schema_and_errors_are_explicit_and_fail_before_task_creation(self) -> None:
    for tool in delegate.tool_list():
      if tool["name"] in {"delegate_run", "delegate_plan"}:
        self.assertEqual(tool["inputSchema"]["properties"]["operation"]["enum"], ["read", "write"])
    self.assertEqual(optimizer.normalize_operation("read-only review"), "read")
    with self.assertRaisesRegex(ValueError, "operation must be 'read'.*'write'"):
      service.run("s", "auto", "auto", "auto", "x", "y", "z", str(self.root), operation="anything")
    self.assertIsNone(st.session("s").get("active_task"))
    with self.assertRaisesRegex(ValueError, "estimated_main_tokens"):
      optimizer.plan("s", estimated_main_tokens=-1)


class DraftWorkspaceTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-drafts-")
    self.root = Path(self.temp.name)
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.root / "state")})
    self.env.start()
    self.repo = self.root / "repo"
    self.repo.mkdir()
    self.git("init", "-q")
    self.git("config", "user.name", "Fixture")
    self.git("config", "user.email", "fixture@example.invalid")
    self.git("config", "commit.gpgsign", "false")
    for name, text in {"README.md": "committed\n", "old.md": "old\n", "unselected.md": "old\n", ".gitignore": "cache/\n"}.items():
      (self.repo / name).write_text(text)
    self.git("add", ".")
    self.git("commit", "-qm", "fixture")
    self.job = {"id": "jfixture", "source_cwd": str(self.repo), "cwd": str(self.repo),
      "role": {"workspace": "worktree"}, "cancel_requested": False, "status": "running"}

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def git(self, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

  def prepare(self, inputs: list[str]) -> Path:
    self.job["source_inputs"] = inputs
    st.write("job-jfixture.json", self.job)
    return Path(workspaces.prepare(self.job))

  def test_modified_staged_untracked_and_deleted_inputs_arrive_without_source_changes(self) -> None:
    (self.repo / "README.md").write_text("staged\n")
    self.git("add", "README.md")
    (self.repo / "README.md").write_text("current draft\n")
    (self.repo / "doc").mkdir()
    (self.repo / "doc/说明.md").write_text("new draft\n")
    (self.repo / "old.md").unlink()
    (self.repo / "unselected.md").write_text("not selected\n")
    before = (self.git("status", "--porcelain"), self.git("diff", "--cached"), self.git("rev-parse", "HEAD"))
    target = self.prepare(["README.md", "doc/说明.md", "old.md"])
    self.assertEqual((target / "README.md").read_text(), "current draft\n")
    self.assertEqual((target / "doc/说明.md").read_text(), "new draft\n")
    self.assertFalse((target / "old.md").exists())
    self.assertEqual((target / "unselected.md").read_text(), "old\n")
    self.assertEqual(before, (self.git("status", "--porcelain"), self.git("diff", "--cached"), self.git("rev-parse", "HEAD")))
    job = st.job("jfixture")
    self.assertEqual(job["source_snapshot"]["status"], "applied")
    self.assertTrue(source_inputs.verify(job, target))
    self.assertFalse(commits.git(target, "diff", "--cached", "--name-only"))

  def test_snapshot_commit_still_requires_explicit_paths_and_keeps_source_index(self) -> None:
    (self.repo / "README.md").write_text("inherited draft\n")
    (self.repo / "notes.md").write_text("preserve input\n")
    before = self.git("status", "--porcelain")
    target = self.prepare(["README.md", "notes.md"])
    value = st.update_job("jfixture", allow_commit=True, commit_message="Sync documentation", commit_paths=["README.md"])
    commits.prepare(value)
    (target / "README.md").write_text("builder synchronized\n")
    receipt = commits.create(st.job("jfixture"))
    self.assertEqual(receipt["files"], ["README.md"])
    self.assertEqual(self.git("status", "--porcelain"), before)
    self.assertEqual((self.repo / "README.md").read_text(), "inherited draft\n")
    self.assertTrue((target / "notes.md").exists())

  def test_default_dirty_source_is_rejected_with_snapshot_hint(self) -> None:
    (self.repo / "README.md").write_text("draft\n")
    st.write("job-jfixture.json", self.job)
    with self.assertRaisesRegex(ValueError, "source_inputs"):
      workspaces.prepare(self.job)

  def test_no_recursive_copy_of_ignored_files_directories_or_symlinks(self) -> None:
    (self.repo / "cache").mkdir()
    (self.repo / "cache/secret.txt").write_text("not copied")
    (self.repo / "link.md").symlink_to(self.root / "outside")
    (self.repo / "link-dir").symlink_to(self.root, target_is_directory=True)
    for paths in (["cache/secret.txt"], ["cache"], ["link.md"], ["link-dir/x"], [".env"], ["../x"], [".git/config"], ["missing.md"]):
      with self.subTest(paths=paths), self.assertRaises((ValueError, OSError)):
        self.prepare(paths)
      self.assertFalse(st.job("jfixture").get("worktree"))

  def test_large_special_files_and_source_edits_during_capture_are_rejected(self) -> None:
    (self.repo / "large.md").write_bytes(b"x" * (source_inputs.MAX_FILE + 1))
    os.mkfifo(self.repo / "fifo.md")
    for name in ("large.md", "fifo.md"):
      with self.assertRaises(ValueError):
        self.prepare([name])
    original = source_inputs.read
    calls = [0]
    def changed(root: Path, name: str):
      calls[0] += 1
      return (b"changed", 0o644) if calls[0] == 2 else original(root, name)
    with patch("source_inputs.read", side_effect=changed), self.assertRaisesRegex(ValueError, "changed during capture"):
      self.prepare(["README.md"])

  def test_literal_filename_and_subdirectory_cwd(self) -> None:
    (self.repo / "doc").mkdir()
    (self.repo / "doc/*.md").write_text("literal wildcard filename\n")
    self.job.update(source_cwd=str(self.repo / "doc"), cwd=str(self.repo / "doc"))
    target_cwd = self.prepare(["doc/*.md"])
    self.assertEqual(target_cwd.name, "doc")
    self.assertEqual((target_cwd / "*.md").read_text(), "literal wildcard filename\n")


if __name__ == "__main__":
  unittest.main()

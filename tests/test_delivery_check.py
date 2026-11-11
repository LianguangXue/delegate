"""Static delivery evidence must detect hidden helpers without executing project code."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import delivery_check
import diagnostics
import storage as st


class DeliveryTests(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory(prefix="delegate-delivery-")
    self.root = Path(self.tmp.name)
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.root / "state")})
    self.env.start()
    self.repo = self.root / "repo"
    self.repo.mkdir()
    self.git("init", "-q")
    self.git("config", "core.hooksPath", "/dev/null")
    self.git("config", "commit.gpgsign", "false")
    self.git("config", "user.name", "Fixture")
    self.git("config", "user.email", "fixture@example.invalid")
    (self.repo / "tests").mkdir()
    (self.repo / ".gitignore").write_text("output/\n__pycache__/\n")
    (self.repo / "tests/test_example.py").write_text("import unittest\n")
    self.git("add", ".gitignore", "tests/test_example.py")
    self.git("commit", "-qm", "fixture")
    self.base = self.git("rev-parse", "HEAD").strip()
    self.job = {"id": "j", "session_id": "s", "status": "running", "cwd": str(self.repo),
      "source_cwd": str(self.repo), "worktree": str(self.repo), "base_commit": self.base,
      "role": {"workspace": "worktree"}, "cancel_requested": False}
    st.write("job-j.json", self.job)

  def tearDown(self) -> None:
    self.env.stop()
    self.tmp.cleanup()

  def git(self, *args: str) -> str:
    return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                          text=True, timeout=10).stdout

  def loader(self) -> None:
    (self.repo / "tests/test_example.py").write_text('''import importlib.util
from pathlib import Path
HELPER = Path(__file__).resolve().parents[1] / "output" / "helper.py"
spec = importlib.util.spec_from_file_location("helper", HELPER)
''')

  def test_ignored_output_helper_is_reported_without_import_or_mutation(self) -> None:
    self.loader()
    (self.repo / "output").mkdir()
    helper = self.repo / "output/helper.py"
    helper.write_text("raise RuntimeError('MUST_NOT_EXECUTE')\n")
    before = self.git("status", "--porcelain")
    result = delivery_check.check(self.job)
    self.assertEqual(result["status"], "warning")
    self.assertIn("output/helper.py", [r["dependency"] for r in result["findings"]])
    self.assertEqual(result["findings"][0]["reason"], "not_committed_in_git")
    self.assertEqual(result["inference_calls"], 0)
    self.assertEqual(self.git("status", "--porcelain"), before)
    self.assertEqual(helper.read_text(), "raise RuntimeError('MUST_NOT_EXECUTE')\n")

  def test_missing_output_helper_is_reported(self) -> None:
    self.loader()
    result = delivery_check.check(self.job)
    self.assertEqual(result["findings"][0]["dependency"], "output/helper.py")
    self.assertEqual(result["findings"][0]["reason"], "missing_local_file")

  def test_staged_helper_is_not_confused_with_committed_helper(self) -> None:
    self.loader()
    (self.repo / "output").mkdir()
    (self.repo / "output/helper.py").write_text("VALUE = 1\n")
    self.git("add", "-f", "output/helper.py")
    self.assertEqual(delivery_check.check(self.job)["status"], "warning")
    self.git("add", "tests/test_example.py")
    self.git("commit", "-qm", "include helper")
    self.assertEqual(delivery_check.check(self.job)["status"], "passed")

  def test_local_namespace_import_finds_untracked_helper(self) -> None:
    (self.repo / "tests/test_example.py").write_text("from output import helper\n")
    (self.repo / "output").mkdir()
    (self.repo / "output/helper.py").write_text("VALUE = 1\n")
    findings = delivery_check.check(self.job)["findings"]
    self.assertEqual([f["dependency"] for f in findings], ["output/helper.py"])

  def test_transitive_reference_identifies_actual_importing_file(self) -> None:
    (self.repo / "tests/test_example.py").write_text("import support\n")
    (self.repo / "support.py").write_text('import runpy\nrunpy.run_path("output/helper.py")\n')
    self.git("add", "support.py", "tests/test_example.py")
    self.git("commit", "-qm", "support")
    findings = delivery_check.check(self.job)["findings"]
    self.assertEqual(findings[0]["referenced_by"], "support.py")
    self.assertEqual(findings[0]["test"], "tests/test_example.py")

  def test_explicit_source_path_is_reported_without_reading_it(self) -> None:
    external = self.root / "private.py"
    external.write_text("DO_NOT_READ = 'PRIVATE_CONTENT'\n")
    (self.repo / "tests/test_example.py").write_text("import runpy\nrunpy.run_path(" + repr(str(external)) + ")\n")
    result = delivery_check.check(self.job)
    self.assertEqual(result["findings"][0]["reason"], "outside_worktree")
    self.assertNotIn("PRIVATE_CONTENT", json.dumps(result))

  def test_oversized_python_and_dynamic_loader_do_not_claim_passed(self) -> None:
    path = self.repo / "tests/test_example.py"
    path.write_text("#" * 65537)
    self.assertEqual(delivery_check.check(self.job)["status"], "incomplete")
    path.write_text("import importlib.util\nimportlib.util.spec_from_file_location('helper', unknown())\n")
    self.assertEqual(delivery_check.check(self.job)["status"], "incomplete")

  def test_validation_target_parsing_does_not_execute_shell(self) -> None:
    self.assertEqual(delivery_check.test_targets("python -m pytest tests/test_example.py::test_one", str(self.repo)),
                     [str(self.repo / "tests/test_example.py")])
    self.assertEqual(delivery_check.test_targets("echo tests/test_example.py", str(self.repo)), [])
    result = delivery_check.test_targets("/bin/bash -lc 'pytest tests/test_example.py && echo done'", str(self.repo))
    self.assertEqual(result, [str(self.repo / "tests/test_example.py")])

  def test_argv_credentials_are_redacted_and_bounded(self) -> None:
    value = diagnostics.command_line(["python", "replay.py", "--api-key", "PRIVATE_KEY", "--password=PRIVATE_PASS",
      "Authorization=Bearer PRIVATE_BEARER", "CUSTOM_TOKEN=PRIVATE_ENV", "--label", "known-secret", "x" * 5000], ["known-secret"])
    self.assertNotIn("PRIVATE_", json.dumps(value))
    self.assertNotIn("known-secret", json.dumps(value))
    self.assertTrue(value["arguments_truncated"])
    self.assertLessEqual(len(value["command_line"]), 2048)

  def test_basic_auth_url_and_shell_forms_are_redacted(self) -> None:
    commands = [["curl", "-u", "alice:PRIVATE_VALUE", "https://example.invalid"],
      ["curl", "-ualice:PRIVATE_VALUE", "https://example.invalid"],
      ["curl", "https://alice:PRIVATE_VALUE@example.invalid/path"],
      ["bash", "-lc", "curl -u alice:PRIVATE_VALUE https://example.invalid"],
      ["bash", "-lc", "cd /tmp && env FOO=bar curl --user='alice:PRIVATE_VALUE with spaces' https://example.invalid"],
      ["bash", "-lc", "(curl -u alice:PRIVATE_VALUE https://example.invalid)"],
      ["bash", "-lc", "echo begin\ncurl -u alice:PRIVATE_VALUE https://example.invalid"],
      ["bash", "-lc", "command -p curl -u alice:PRIVATE_VALUE https://example.invalid"],
      ["bash", "-lc", "eval 'curl -u alice:PRIVATE_VALUE https://example.invalid'"],
      ["bash", "-lc", "$(echo PRIVATE_VALUE)"],
      ["sshpass", "-pPRIVATE_VALUE", "ssh", "host"]]
    for command in commands:
      with self.subTest(command=command[0]):
        self.assertNotIn("PRIVATE_VALUE", json.dumps(diagnostics.command_line(command, [])))

  def test_external_test_directory_is_not_enumerated(self) -> None:
    self.git("rm", "tests/test_example.py")
    self.git("commit", "-qm", "remove test")
    if (self.repo / "tests").exists():
      (self.repo / "tests").rmdir()
    external = self.root / "external"
    external.mkdir()
    (external / "test_private.py").write_text("PRIVATE_CONTENT\n")
    (self.repo / "tests").symlink_to(external, target_is_directory=True)
    original = os.scandir
    def guard(path):
      self.assertNotEqual(Path(path), self.repo / "tests")
      return original(path)
    with patch("delivery_check.os.scandir", side_effect=guard):
      result = delivery_check.check(self.job)
    self.assertEqual(result["status"], "incomplete")
    self.assertNotIn("PRIVATE_CONTENT", json.dumps(result))

  def test_metadata_probe_budget_bounds_large_import_lists(self) -> None:
    (self.repo / "tests/test_example.py").write_text("\n".join("import missing_module_" + str(i) for i in range(500)))
    result = delivery_check.check(self.job)
    self.assertEqual(result["status"], "incomplete")
    self.assertLessEqual(result["metadata_probes"], 1024)

  def test_loader_and_path_aliases_are_resolved_without_execution(self) -> None:
    (self.repo / "tests/test_example.py").write_text('''from pathlib import Path as P
from importlib.util import spec_from_file_location as load
helper = P(__file__).resolve().parents[1] / "output" / "helper.py"
spec = load("helper", helper)
''')
    result = delivery_check.check(self.job)
    self.assertEqual(result["status"], "warning")
    self.assertEqual(result["findings"][0]["dependency"], "output/helper.py")

  def test_committed_extensionless_loader_target_is_not_reported_untracked(self) -> None:
    (self.repo / "bin").mkdir()
    (self.repo / "bin/helper").write_text("VALUE = 1\n")
    (self.repo / "tests/test_example.py").write_text('import runpy\nrunpy.run_path("bin/helper")\n')
    self.git("add", "bin/helper", "tests/test_example.py")
    self.git("commit", "-qm", "tracked loader target")
    self.assertEqual(delivery_check.check(self.job)["status"], "passed")


if __name__ == "__main__":
  unittest.main()

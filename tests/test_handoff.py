"""Cross-program delivery fixtures: real am CLI, disposable stores, no provider calls."""
from pathlib import Path
from unittest.mock import patch
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import handoff
import exchange
import delivery
import storage as st

AM = Path(os.environ.get("AM_TEST_BINARY", str(Path(__file__).resolve().parents[2] / "am/bin/am"))).resolve()


@unittest.skipUnless(AM.is_file(), "Optional am integration: set AM_TEST_BINARY or clone sibling am")
class HandoffTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix="delegate-am-")
    self.base = Path(self.temp.name)
    self.root = self.base / ".agents"
    (self.root / "tasks").mkdir(parents=True)
    (self.root / "config.json").write_text('{}')
    (self.root / "tasks/task.json").write_text('{"id":"task","status":"open"}')
    binary_dir = self.base / "bin"
    binary_dir.mkdir()
    (binary_dir / "am").symlink_to(AM)
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": str(self.base / "delegate"),
                                      "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"]})
    self.env.start()
    st.save_session("s", {"mode": "off", "jobs": ["j1"]})
    st.write("job-j1.json", {"id": "j1", "session_id": "s", "status": "completed", "review": "pending"})

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def test_real_cli_import_duplicate_and_stale_revision(self) -> None:
    first = handoff.link("s", "bind", "task", str(self.root))
    self.assertTrue(first["delivered"])
    packet = st.read("handoff-s.json")["packet"]
    second = handoff.link("s", "next", next_step="Inspect result then run focused tests")
    self.assertGreater(second["revision"], first["revision"])
    for _ in range(2):
      result = subprocess.run([str(AM), "--root", str(self.root), "delegate", "import"],
        input=json.dumps(packet), text=True, capture_output=True, check=True)
      self.assertEqual(json.loads(result.stdout)["revision"], second["revision"])
    imported = json.loads(subprocess.check_output([str(AM), "--root", str(self.root),
                          "delegate", "report", "--task", "task"]))
    self.assertEqual(len(imported), 1)
    self.assertIn("Inspect result", imported[0]["next_step"])
    self.assertEqual(json.loads((self.root / "tasks/task.json").read_text())["status"], "open")

  def test_failed_delivery_retries_without_new_revision(self) -> None:
    with patch("handoff.subprocess.run", side_effect=OSError("fixture offline")):
      first = handoff.link("s", "bind", "task", str(self.root))
    self.assertFalse(first["delivered"])
    self.assertTrue(st.read("handoff-s.json")["packet"])
    second = handoff.sync("s")
    self.assertTrue(second["delivered"])
    self.assertEqual(first["revision"], second["revision"])

  def test_large_session_has_bounded_summary_and_full_index_reference(self) -> None:
    ids = []
    for i in range(105):
      jid = "j" + str(i)
      ids.append(jid)
      st.write("job-" + jid + ".json", {"id": jid, "status": "completed", "review_evidence": "x" * 20000})
    st.save_session("s", {"mode": "off", "jobs": ids})
    self.assertTrue(handoff.link("s", "bind", "task", str(self.root))["delivered"])
    packet = st.read("handoff-s.json")["packet"]
    self.assertEqual(packet["jobs_omitted"], 5)
    self.assertTrue(Path(packet["session_record_path"]).exists())
    self.assertLess(len(json.dumps(packet)), 900000)
    render = runpy.run_path(str(AM))["delegate_brief"]
    brief = render(self.root, "task")
    self.assertIn("untrusted task data", brief)
    self.assertIn("j104", brief)
    self.assertLess(len(brief), 3200)

  def test_lost_ack_can_replay_identical_packet(self) -> None:
    self.assertTrue(handoff.link("s", "bind", "task", str(self.root))["delivered"])
    outbox = st.read("handoff-s.json")
    revision = outbox["packet"]["revision"]
    outbox["acked_revision"] = 0
    st.write("handoff-s.json", outbox)
    self.assertEqual(handoff.sync("s")["revision"], revision)
    self.assertEqual(st.read("handoff-s.json")["acked_revision"], revision)

  def test_exchange_uses_real_am_without_inference_or_sender_spoofing(self) -> None:
    evidence=self.base/'manifest.json';evidence.write_text('{"rows":10}')
    result=exchange.exchange('s','publish',{'id':'artifact','project':'fixture','task_id':'task',
      'session_id':'spoofed','summary':'frozen replay result','evidence':'fixture checked',
      'files':[str(evidence)]},str(self.root))
    self.assertTrue(result['ok'],result)
    self.assertEqual(result['result']['session_id'],'s')
    search=exchange.exchange('s','search',{'project':'fixture','query':'replay'},str(self.root))
    self.assertEqual(search['result']['inference_calls'],0)
    self.assertFalse(search['inference_started'])

  def test_no_link_no_export_and_binding_requires_existing_task(self) -> None:
    self.assertEqual(handoff.sync("s"), {"linked": False})
    with self.assertRaises(ValueError):
      handoff.link("s", "bind", "missing", str(self.root))

  def test_notifications_survive_session_restart_until_collection(self) -> None:
    first = delivery.notice("j1")
    self.assertIn("completion-j1", first)
    self.assertEqual(delivery.notice("j1"), "")
    self.assertEqual(delivery.notice("j1", force=True), first)
    st.update_job("j1", collected_at=123)
    self.assertEqual(delivery.notice("j1", force=True), "")
    self.assertEqual(st.read("delivery-j1.json")["status"], "acknowledged")
    self.assertEqual(st.job("j1")["review"], "pending")

  def test_orphan_requests_attention_and_never_claims_success(self) -> None:
    st.update_job("j1", status="orphaned")
    self.assertIn("has status orphaned", delivery.notice("j1"))

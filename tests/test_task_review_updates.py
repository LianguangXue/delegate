"""Review-only task updates and actual legacy delivery must not reopen collection notices."""
from unittest.mock import patch
import json
import unittest

from test_result_notifications import DeliveryFixture
import accounting as ac
import delegate
import delivery
import result_delivery
import rewake
import service
import storage as st


class TaskReviewUpdateTests(DeliveryFixture):
  def test_update_accepts_children_without_reclassifying_or_closing_parent(self) -> None:
    task = ac.load(self.task["id"])
    task.update(label="Frozen research", category="research", complexity="complex", case_key="frozen-inputs", arm="baseline")
    ac.save(task)
    result = ac.task_tool("s", "update", accepted_jobs=["jfixture"], evidence="Inspected report and replay evidence")
    for field in ("label", "category", "complexity", "case_key", "arm", "status", "verdict", "evidence"):
      self.assertEqual(ac.load(task["id"])[field], task[field])
    self.assertEqual(result["jobs"][0]["review"], "accepted")
    self.assertEqual(st.session("s")["active_task"], task["id"])
    self.assertIsNone(st.job("jfixture").get("collected_at"))  # Review alone is not report delivery.

  def test_partial_classification_update_preserves_unsupplied_fields(self) -> None:
    task = ac.load(self.task["id"])
    task.update(jobs=[], label="Preserve this", category="research", arm="baseline", case_key="same-case")
    ac.save(task)
    ac.task_tool("s", "update", complexity="complex")
    updated = ac.load(task["id"])
    for field in ("label", "category", "arm", "case_key"):
      self.assertEqual(updated[field], task[field])
    self.assertEqual(updated["complexity"], "complex")

  def test_reclassification_after_dispatch_rejects_entire_update(self) -> None:
    before = st.job("jfixture")
    with self.assertRaisesRegex(ValueError, "classification is frozen"):
      ac.task_tool("s", "update", complexity="complex", accepted_jobs=["jfixture"], evidence="Inspected")
    self.assertEqual(st.job("jfixture"), before)

  def test_update_requires_evidence_and_checks_entire_batch_before_any_review(self) -> None:
    before = st.job("jfixture")
    with self.assertRaisesRegex(ValueError, "validation evidence"):
      ac.task_tool("s", "update", accepted_jobs=["jfixture"])
    with self.assertRaisesRegex(ValueError, "outside this task"):
      ac.task_tool("s", "update", accepted_jobs=["jfixture", "foreign"], evidence="Inspected")
    self.assertEqual(st.job("jfixture"), before)

  def test_update_rejects_failed_active_and_foreign_children(self) -> None:
    for fields in ({"status": "running"}, {"status": "failed"}, {"error": "failure"},
                   {"session_id": "other"}, {"task_id": "other"}):
      st.write("job-jfixture.json", {**self.job, **fields})
      with self.assertRaises(ValueError):
        ac.task_tool("s", "update", accepted_jobs=["jfixture"], evidence="Inspected")
      self.assertEqual(st.job("jfixture")["review"], "pending")

  def test_review_retry_preserves_existing_accepted_evidence_and_collection(self) -> None:
    service.jobs("s", "result", "jfixture")
    service.jobs("s", "verify", "jfixture", verdict="accepted", evidence="Original detailed evidence")
    before = st.job("jfixture")
    for _ in range(2):
      ac.task_tool("s", "update", accepted_jobs=["jfixture", "jfixture"], evidence="Retry summary")
    self.assertEqual(st.job("jfixture"), before)

  def test_closed_task_can_record_child_review_without_being_reopened(self) -> None:
    task = ac.load(self.task["id"])
    task.update(status="closed", finished_at=task["created_at"], verdict="accepted", evidence="Parent evidence")
    ac.save(task)
    ac.task_tool("s", "update", task_id=task["id"], accepted_jobs=["jfixture"], evidence="Child evidence")
    after = ac.load(task["id"])
    self.assertEqual((after["status"], after["verdict"], after["evidence"]), ("closed", "accepted", "Parent evidence"))
    self.assertEqual(st.job("jfixture")["review"], "accepted")

  def test_finish_retains_batch_review_and_parent_finish_semantics(self) -> None:
    service.jobs("s", "result", "jfixture")
    result = ac.task_tool("s", "finish", accepted_jobs=["jfixture"], verdict="accepted", evidence="All evidence checked")
    self.assertEqual(result["status"], "closing")
    self.assertEqual(result["verdict"], "accepted")
    self.assertEqual(result["jobs"][0]["review"], "accepted")


class LegacyVerifyCollectionTests(DeliveryFixture):
  def payload(self, response: dict) -> dict:
    return {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "mcp__x__delegate_job",
      "tool_input": {"session_id": "s", "action": "verify", "job_id": "jfixture", "verdict": "accepted", "evidence": "Checked"},
      "tool_response": {"content": [{"type": "text", "text": json.dumps(response)}]}}

  def test_first_full_legacy_verify_is_delivered_then_acknowledged_and_silent(self) -> None:
    text = "All evidence\n" * 1000
    st.update_job("jfixture", result=text, review="accepted", review_evidence="Checked")
    response = st.job("jfixture")
    payload = self.payload(response)
    self.assertIsNone(rewake.compact_echo(payload))
    with patch("delegate.handoff.sync"):
      output = delegate.hook(payload)
    self.assertNotIn("updatedMCPToolOutput", output.get("hookSpecificOutput", {}))
    self.assertTrue(result_delivery.proof(st.job("jfixture"))[2])
    self.assertTrue(st.job("jfixture")["collected_at"])
    for _ in range(4):
      self.assertEqual(delivery.notice("jfixture", force=True), "")
      self.assertEqual(delegate.stop_feedback("s", False), "")
    self.assertEqual(st.read("delivery-jfixture.json")["status"], "acknowledged")
    self.assertIsNotNone(rewake.compact_echo(payload))

  def test_partial_or_receipt_only_verify_never_acknowledges_unseen_text(self) -> None:
    st.update_job("jfixture", review="accepted", review_evidence="Checked")
    base = st.job("jfixture")
    for response in ({**base, "result": "preview", "result_truncated": True},
                     {**base, "result": "other text"},
                     {k: v for k, v in base.items() if k != "result"},
                     {**base, "result_text_included": False}):
      rewake.observe_collection(self.payload(response))
      self.assertIsNone(st.job("jfixture").get("collected_at"))
    self.assertTrue(delivery.notice("jfixture"))
    self.assertTrue(delegate.stop_feedback("s", False))

  def test_legacy_failure_verify_delivers_error_without_changing_blocked_verdict(self) -> None:
    st.update_job("jfixture", status="failed", result="", error="Workspace preparation failed",
                  review="blocked", review_evidence="Failure inspected")
    response = st.job("jfixture")
    payload = self.payload(response)
    payload["tool_input"]["verdict"] = "blocked"
    rewake.observe_collection(payload)
    self.assertTrue(st.job("jfixture")["collected_at"])
    self.assertEqual(st.job("jfixture")["review"], "blocked")
    self.assertEqual(delivery.notice("jfixture", force=True), "")

  def test_verify_response_errors_and_foreign_ids_do_not_collect(self) -> None:
    base = st.job("jfixture")
    payload = self.payload(base)
    payload["tool_response"]["isError"] = True
    rewake.observe_collection(payload)
    payload = self.payload(base)
    payload["tool_input"]["job_id"] = "other"
    rewake.observe_collection(payload)
    payload = self.payload({**base, "session_id": "other"})
    rewake.observe_collection(payload)
    self.assertIsNone(st.job("jfixture").get("collected_at"))

  def test_failure_diagnostics_must_match_before_empty_report_is_acknowledged(self) -> None:
    st.update_job("jfixture", status="failed", result="", error="Current failure")
    rewake.observe_collection(self.payload({**st.job("jfixture"), "error": "Different failure"}))
    self.assertIsNone(st.job("jfixture").get("collected_at"))


if __name__ == "__main__":
  unittest.main()

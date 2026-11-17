"""Different new work and ended-but-unmetered native agents must not become permanent blockers."""
from pathlib import Path
from unittest.mock import patch, Mock
import copy
import os
import time
import unittest

import test_routing_policy as routing_fixtures
import test_optimizer as meter_fixtures
from test_routing_policy import DAILY
import accounting as ac
import optimizer
import dispatch_policy
import service
import storage as st


class WorkIdentityTests(unittest.TestCase):
  setUp=routing_fixtures.RoutingPolicyTests.setUp
  tearDown=routing_fixtures.RoutingPolicyTests.tearDown

  def seed(self) -> dict:
    service.control("s", "on", agents=["codex-reviewer"], strategy="preserve-claude")
    job={'id':'jold','task_id':self.task['id'],'session_id':'s','work_item':'main','status':'completed',
      'model':'gpt-6.1-sol','effort':'medium','agent':'codex-reviewer','provider_name':'codex',
      'provider':{'kind':'codex'},'role':{'workspace':'read-only'},'source_cwd':self.tmp.name,'cwd':self.tmp.name,
      'task':'Old reviewed work','acceptance':'Original evidence','context':'','review':'accepted',
      'review_evidence':'Checked original output','finished_at':time.time(),'inference_state':'attempted'}
    st.write('job-jold.json',job)
    task=ac.load(self.task['id']);task['jobs']=['jold'];ac.save(task)
    state=st.session('s');state['jobs']=['jold'];st.save_session('s',state)
    return job

  def args(self) -> dict:
    return dict(session_id='s',agent='auto',model='auto',effort='auto',task='New independent work',
      acceptance='New evidence',rationale='New objective',cwd=self.tmp.name,
      task_profile={**DAILY,'validation':'inspection'})

  def test_unrelated_new_run_does_not_reuse_accepted_main_or_fabricate_checkpoint(self) -> None:
    old=self.seed()
    with patch('service.subprocess.Popen',return_value=Mock(pid=os.getpid())):
      result=service.run(**self.args())
    self.assertTrue(result['work_item'].startswith('auto-'))
    self.assertNotEqual(result['id'],old['id'])
    self.assertEqual(st.job('jold'),old)
    with patch('service.subprocess.Popen') as launch:
      repeated=service.run(**self.args())
      self.assertEqual(repeated['action'],'wait')
      launch.assert_not_called()

  def test_same_legacy_intent_and_explicit_main_still_require_revalidation(self) -> None:
    old=self.seed()
    for args in ({**self.args(),'task':old['task'],'acceptance':old['acceptance']},
                 {**self.args(),'work_item':'main'}):
      with patch('service.subprocess.Popen') as launch:
        result=service.run(**args)
      self.assertEqual(result['action'],'replan')
      self.assertEqual(result['job_id'],'jold')
      self.assertIn('checkpoint',result['reason'])
      self.assertIn('delegate_inspect',result['recovery'])
      launch.assert_not_called()

  def test_intent_key_is_stable_but_changed_inputs_or_acceptance_are_distinct(self) -> None:
    def key(**changes):
      return optimizer.default_work_item(**{**dict(task='Work',acceptance='Pass',context='Input A',cwd=self.tmp.name,operation='read'),**changes})
    self.assertEqual(key(),key())
    self.assertEqual(key(required_files=['a','b']),key(required_files=['b','a']))
    for change in ({'task':'Different'},{'acceptance':'Other checks'},{'context':'Input B'},{'operation':'write'}, {'required_files':['other.py']}):
      self.assertNotEqual(key(),key(**change))

  def test_mixed_legacy_main_history_cannot_reuse_an_unrelated_accepted_result(self) -> None:
    old=self.seed();old['evidence_checkpoint']={'fixture':True};st.write('job-jold.json',old)
    unrelated={**old,'id':'jother','task':'Other accepted work'};st.write('job-jother.json',unrelated)
    task=ac.load(self.task['id']);task['jobs'].append('jother');ac.save(task)
    key=optimizer.default_work_item(old['task'],old['acceptance'],old['context'],old['source_cwd'],'read')
    with patch('inspection.inspect',return_value={'blockers':[]}) as check:
      result=optimizer.work_state(task,key)
    self.assertEqual(result['job_id'],'jold')
    check.assert_called_once_with('s',job_id='jold')
    st.update_job('jother',status='running')
    with patch('inspection.inspect',return_value={'blockers':[]}):
      self.assertEqual(optimizer.work_state(task,key)['action'],'reuse')

  def test_legacy_quality_evidence_matches_at_delayed_worker_check(self) -> None:
    old=self.seed();old.update(review='needs-work',review_evidence='Concrete wrong invariant')
    st.write('job-jold.json',old)
    cfg=st.config();cfg['model_policy']={'mode':'sol-first'};st.write('config.json',cfg)
    key=optimizer.default_work_item(old['task'],old['acceptance'],old['context'],old['source_cwd'],'read')
    job={**old,'id':'jnew','work_item':key,'model':'gpt-6-astra','quality_escalation_from':'jold','escalate':False}
    dispatch_policy.check_job(job)
    with self.assertRaisesRegex(ValueError,'sol-first'):
      dispatch_policy.check_job({**job,'work_item':'auto-unrelated'})
    with self.assertRaisesRegex(ValueError,'sol-first'):
      dispatch_policy.check_job({**job,'session_id':'other'})

  def test_fixed_role_new_work_gets_a_separate_stable_item(self) -> None:
    self.seed()
    with patch('service.subprocess.Popen',return_value=Mock(pid=os.getpid())):
      result=service.run(**{**self.args(),'agent':'codex-reviewer'})
    self.assertTrue(result['work_item'].startswith('auto-'))


class NativeCompletionTests(unittest.TestCase):
  setUp=meter_fixtures.MeterTests.setUp
  tearDown=meter_fixtures.MeterTests.tearDown
  append=meter_fixtures.MeterTests.append
  collect=meter_fixtures.MeterTests.collect

  def test_stop_without_readable_usage_allows_close_but_never_claims_complete_metering(self) -> None:
    task=ac.begin('s');self.append(meter_fixtures.message('parent',10))
    self.collect('SubagentStart',agent_id='native')
    self.collect('SubagentStop',agent_id='native',agent_transcript_path=str(Path(self.temp.name)/'missing.jsonl'))
    result=ac.task_tool('s','finish',verdict='accepted',evidence='Terminal output checked')
    self.assertEqual(result['status'],'closed')
    self.collect('Stop')
    result=ac.report(ac.load(task['id']))
    self.assertEqual(result['status'],'closed')
    self.assertFalse(result['measurement_complete'])
    self.assertEqual(result['native_agents'][0]['usage_status'],'unknown')
    self.assertFalse(result['native_agents'][0]['usage_complete'])
    self.assertFalse(ac.load(task['id'])['native']['native']['complete'])

  def test_legacy_stop_path_with_missing_file_is_not_a_live_agent(self) -> None:
    task=ac.begin('s');task['native']={'legacy':{'path':str(Path(self.temp.name)/'missing'),'complete':False,'observed':False}};ac.save(task)
    result=ac.task_tool('s','finish',verdict='accepted',evidence='Original validation')
    self.assertEqual(result['native_agents'][0]['completion_source'],'legacy-stop-record')
    self.assertEqual(result['status'],'closed')
    self.assertFalse(result['measurement_complete'])

  def test_running_execution_blocks_even_when_usage_is_read_to_eof(self) -> None:
    task=ac.begin('s');task['native']={'live':{'execution_state':'running','complete':True,'observed':True}};ac.save(task)
    with self.assertRaisesRegex(ValueError,'live.*resolve-native'):
      ac.task_tool('s','finish',verdict='accepted',evidence='Parent finished')
    self.collect('Stop')
    self.assertEqual(ac.load(task['id'])['status'],'open')

  def test_parent_resolution_requires_exact_id_evidence_and_preserves_unknown_usage(self) -> None:
    task=ac.begin('s');self.collect('SubagentStart',agent_id='lost')
    for args in ({'native_agent_id':'other','evidence':'Checked'}, {'native_agent_id':'lost','evidence':''}):
      with self.assertRaises(ValueError):ac.task_tool('s','resolve-native',**args)
    with self.assertRaisesRegex(ValueError,'another session'):
      ac.task_tool('other','resolve-native',task_id=task['id'],native_agent_id='lost',evidence='Checked')
    result=ac.task_tool('s','resolve-native',native_agent_id='lost',evidence='Host TaskOutput confirms terminal completion')
    self.assertEqual(result['native_agents'][0]['execution_state'],'stopped')
    self.assertFalse(result['native_agents'][0]['usage_complete'])
    ac.task_tool('s','finish',verdict='accepted',evidence='Validated deliverables')
    self.collect('Stop')
    self.assertEqual(ac.load(task['id'])['status'],'closed')
    self.assertFalse(ac.report(ac.load(task['id']))['measurement_complete'])

  def test_incomplete_usage_close_still_requires_plugin_result_collection(self) -> None:
    task=ac.begin('s')
    task['native']={'ended':{'path':None,'complete':False,'observed':False}}
    task['jobs']=['junread'];ac.save(task)
    job={'id':'junread','session_id':'s','task_id':task['id'],'status':'completed','await_completion':True,
      'provider':{'kind':'codex'},'provider_name':'codex','agent':'codex-reviewer','model':'fixture',
      'effort':'low','result':'Uncollected evidence','review':'pending'}
    st.write('job-junread.json',job)
    with self.assertRaisesRegex(ValueError,'junread.*acceptance is not collection'):
      ac.task_tool('s','finish',verdict='accepted',accepted_jobs=['junread'],evidence='Checked')
    self.assertEqual(ac.load(task['id'])['status'],'open')
    self.assertEqual(st.session('s')['active_task'],task['id'])
    self.assertEqual(st.job('junread')['review'],'pending')
    service.jobs('s','result','junread',result_view='file')
    result=ac.task_tool('s','finish',verdict='accepted',accepted_jobs=['junread'],evidence='Read exported evidence')
    self.assertEqual(result['status'],'closed')
    self.assertIsNone(st.session('s').get('active_task'))

  def test_close_boundary_excludes_later_parent_turns_and_advances_meter(self) -> None:
    task=ac.begin('s');self.append(meter_fixtures.message('before',10))
    self.collect('SubagentStop',agent_id='ended')
    boundary=self.path.stat().st_size
    result=ac.task_tool('s','finish',verdict='accepted',evidence='Checked')
    self.assertEqual(result['status'],'closed')
    self.assertEqual(ac.load(task['id'])['end_offset'],boundary)
    self.assertEqual(st.read('meter-s.json')['turn_start'],boundary)
    self.append(meter_fixtures.message('later',10000))
    result=ac.task_tool('s','report',task_id=task['id'])
    self.assertEqual(result['parent']['processed_tokens'],11)
    self.assertFalse(result['parent_final_response_metered'])
    self.assertFalse(result['measurement_complete'])

  def test_late_usage_can_be_collected_without_reopening_execution(self) -> None:
    task=ac.begin('s');self.append(meter_fixtures.message('parent',10))
    path=Path(self.temp.name)/'later.jsonl'
    self.collect('SubagentStop',agent_id='native',agent_transcript_path=str(path))
    ac.task_tool('s','finish',verdict='accepted',evidence='Checked')
    self.collect('Stop')
    import json
    path.write_text(json.dumps(meter_fixtures.message('child',25))+'\n')
    result=ac.task_tool('s','report',task_id=task['id'])
    self.assertEqual(result['status'],'closed')
    self.assertEqual(result['native_agents'][0]['usage_status'],'complete')
    self.assertEqual(result['total']['processed_tokens'],37)


if __name__=='__main__':unittest.main()

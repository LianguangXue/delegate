"""Policy gates and declared-input snapshots must fail before paid inference."""
from pathlib import Path
from unittest.mock import Mock, patch
import copy
import json
import os
import time
import unittest

import test_routing_policy as routing_fixtures
import test_dispatch_inputs as input_fixtures
from test_routing_policy import HARD, DAILY
import dispatch_policy
import runtime_gate
import accounting
import delegate
import input_contract
import optimizer
import service
import storage as st
import workspaces
import lifecycle
import worker


class ModelPolicyTests(unittest.TestCase):
  setUp = routing_fixtures.RoutingPolicyTests.setUp
  tearDown = routing_fixtures.RoutingPolicyTests.tearDown

  def test_global_provider_and_role_deny_apply_before_selection(self) -> None:
    for scope in ('global','provider','role'):
      cfg=copy.deepcopy(self.cfg)
      target=cfg.setdefault('model_policy',{}) if scope=='global' else cfg['providers']['codex'] if scope=='provider' else cfg['agents']['codex-reviewer']
      target['deny_models']=['*astra*']
      st.write('config.json',cfg)
      service.control('s','on',agents=['codex-reviewer'],strategy='preserve-claude')
      result=optimizer.plan('s',task_profile=HARD)
      self.assertEqual(result['action'],'replan')
      self.assertIn('forbidden',json.dumps(result))
      with patch('service.subprocess.Popen') as launch:
        with self.assertRaisesRegex(ValueError,'forbidden'):
          service.run('s','codex-reviewer','gpt-6-astra','high','Review','Evidence','Why',self.tmp.name,escalate=True,task_profile=HARD)
        launch.assert_not_called()

  def test_sol_first_requires_explicit_or_evidenced_quality_escalation(self) -> None:
    cfg=copy.deepcopy(self.cfg);cfg['model_policy']={'mode':'sol-first'};st.write('config.json',cfg)
    service.control('s','on',agents=['codex-reviewer'],strategy='preserve-claude')
    blocked=optimizer.plan('s',task_profile=HARD)
    self.assertEqual(blocked['action'],'replan')
    self.assertIn('escalate=true',json.dumps(blocked))
    allowed=optimizer.plan('s',task_profile=HARD,escalate=True)
    self.assertEqual(allowed['model'],'gpt-6-astra')
    # sol-first blocks Astra on non-complex tasks even when escalate=True unless Sol needs-work failed.
    non_complex_blocked=dispatch_policy.reason('gpt-6-astra',{},{},escalate=True,cfg=cfg,complexity='standard')
    self.assertIn('strictly for complex',non_complex_blocked)
    with patch('service.subprocess.Popen') as launch:
      with self.assertRaisesRegex(ValueError,'strictly for complex'):
        service.run(
          's','codex-reviewer','gpt-6-astra','high','Review','Evidence','Casual astra',
          self.tmp.name,escalate=True,task_profile=DAILY,
        )
      launch.assert_not_called()
    previous={'model':'gpt-6.1-sol','review':'needs-work','review_evidence':'Incorrect invariant','status':'completed'}
    self.assertFalse(dispatch_policy.reason('gpt-6-astra',{}, {},previous=previous,complexity='standard'))
    for previous_bad in ({**previous,'status':'failed'}, {**previous,'error':'Transport error'}, {**previous,'review_evidence':''}):
      self.assertTrue(dispatch_policy.reason('gpt-6-astra',{}, {},previous=previous_bad))
    cfg['model_policy']['deny_models']=['*astra*'];st.write('config.json',cfg)
    self.assertTrue(dispatch_policy.reason('gpt-6-astra',{}, {},escalate=True,previous=previous))

  def test_default_behavior_and_config_validation(self) -> None:
    self.assertEqual(optimizer.plan('s',task_profile=HARD)['model'],'gpt-6-astra')
    for value in ({'mode':'bad'},{'deny_models':'*'}, {'deny_models':['']},{'quality_escalation':1},{'unknown':True}):
      with self.assertRaises(ValueError):dispatch_policy.validate(value)

  def test_policy_change_during_codex_setup_stops_before_turn_start(self) -> None:
    job={'id':'jlate','session_id':'s','status':'running','created_at':time.time(),
      'provider':{'kind':'codex','command':'fixture'},'provider_name':'codex','agent':'codex-reviewer',
      'role':{'workspace':'read-only'},'cwd':self.tmp.name,'source_cwd':self.tmp.name,
      'model':'gpt-6.1-sol','effort':'medium','timeout_seconds':900,'messages':[],
      'cancel_requested':False,'inference_state':'not_started'}
    st.write('job-jlate.json',job)
    cfg=copy.deepcopy(self.cfg);calls=[]
    class RPC:
      def __init__(self,*args,**kwargs):pass
      def __enter__(self):return self
      def __exit__(self,*args):pass
      def call(self,method,params=None,timeout=30):
        calls.append(method)
        if method=='account/read':return {'account':{'type':'chatgpt'}}
        if method=='thread/start':
          cfg['model_policy']={'deny_models':['*sol*']};st.write('config.json',cfg)
          return {'thread':{'id':'fixture-thread'}}
        raise AssertionError(method)
    with patch('worker.CodexRPC',RPC):
      with self.assertRaisesRegex(ValueError,'forbidden'):worker.codex_worker(job,'Fixture')
    self.assertNotIn('turn/start',calls)
    self.assertEqual(st.job('jlate')['inference_state'],'not_started')

  def test_policy_change_during_cli_setup_stops_before_popen(self) -> None:
    job={'id':'jlate','session_id':'s','status':'running','provider':{'kind':'anthropic'},
      'provider_name':'company','agent':'company-reviewer','role':{'workspace':'read-only'},
      'cwd':self.tmp.name,'model':'deepseek-flash','effort':'low','inference_state':'not_started'}
    st.write('job-jlate.json',job)
    def command(value):
      cfg=copy.deepcopy(self.cfg);cfg['model_policy']={'deny_models':['deepseek*']};st.write('config.json',cfg)
      return ['fixture','fixture-thread'], {'CLAUDE_CONFIG_DIR':self.tmp.name}
    with patch('worker.claude_command',side_effect=command), patch('cli_history.restore'), patch('worker.subprocess.Popen') as launch:
      with self.assertRaisesRegex(ValueError,'forbidden'):worker.claude_worker(job,'Fixture')
      launch.assert_not_called()
    self.assertEqual(st.job('jlate')['inference_state'],'not_started')


class RuntimeGateTests(unittest.TestCase):
  setUp = routing_fixtures.RoutingPolicyTests.setUp
  tearDown = routing_fixtures.RoutingPolicyTests.tearDown

  def payload(self, action: str='run', prefix: str='mcp__delegate') -> dict:
    return {'session_id':'s','hook_event_name':'PreToolUse','tool_name':prefix+'__delegate_'+action,
      'tool_input':{'session_id':'s'}}

  def handshake(self, version: str, caps: list | None=None, prefix: str='mcp__delegate', error: bool=False) -> None:
    runtime_gate.observe({'session_id':'s','hook_event_name':'PostToolUse','tool_name':prefix+'__delegate_control',
      'tool_input':{'session_id':'s','action':'status'},'tool_response':{'isError':error,'content':[{'type':'text','text':json.dumps(
        {'session_id':'s','server_runtime':{'version':version,'capabilities':caps or [],'identity':lifecycle.identity(os.getpid())}})}]}})

  def test_unknown_old_and_missing_capabilities_reject_launch_only(self) -> None:
    self.assertTrue(runtime_gate.rejection(self.payload()))
    self.handshake('0.23.2')
    self.assertTrue(runtime_gate.rejection(self.payload()))
    self.handshake('0.26.0')
    self.assertTrue(runtime_gate.rejection(self.payload()))
    self.assertFalse(runtime_gate.rejection(self.payload('job')))
    for action in ('status','cancel','result','verify'):
      p=self.payload('loop');p['tool_input']['action']=action
      self.assertFalse(runtime_gate.rejection(p))

  def test_capable_namespace_does_not_authorize_other_connection_or_errors(self) -> None:
    self.handshake('0.26.0',runtime_gate.CAPABILITIES,error=True)
    self.assertTrue(runtime_gate.rejection(self.payload()))
    self.handshake('0.26.0',runtime_gate.CAPABILITIES)
    self.assertFalse(runtime_gate.rejection(self.payload()))
    self.assertTrue(runtime_gate.rejection(self.payload(prefix='mcp__other')))
    p=self.payload('loop');p['tool_input']['action']='start'
    self.assertFalse(runtime_gate.rejection(p))

  def test_hook_denies_without_starting_history_or_models(self) -> None:
    with patch('cli_history.prepare_resume') as restore:
      output=delegate.hook(self.payload())
      restore.assert_not_called()
    self.assertEqual(output['hookSpecificOutput']['permissionDecision'],'deny')

  def test_dead_or_recycled_server_and_reopened_session_require_handshake(self) -> None:
    self.handshake('0.26.0',runtime_gate.CAPABILITIES)
    state=st.session('s');state['runtime_handshakes']['mcp__delegate']['identity']['start']='wrong'
    st.save_session('s',state)
    self.assertTrue(runtime_gate.rejection(self.payload()))
    self.handshake('0.26.0',runtime_gate.CAPABILITIES)
    runtime_gate.reset({'session_id':'s','hook_event_name':'SessionStart'})
    self.assertTrue(runtime_gate.rejection(self.payload()))
    for value in (None, 'bad', {}, {'pid':0}, {'pid':'1','start':'x','boot':'x'}):
      self.assertFalse(runtime_gate.live_identity(value))


class AutomaticInputTests(unittest.TestCase):
  setUp = input_fixtures.DraftWorkspaceTests.setUp
  tearDown = input_fixtures.DraftWorkspaceTests.tearDown
  git = input_fixtures.DraftWorkspaceTests.git

  def test_declared_staged_untracked_drafts_copied_and_unrelated_edits_excluded(self) -> None:
    (self.repo/'README.md').write_text('staged draft\n');self.git('add','README.md')
    (self.repo/'notes.md').write_text('untracked\n')
    (self.repo/'unselected.md').write_text('other session\n')
    before=(self.git('status','--porcelain'),self.git('diff','--cached'),self.git('rev-parse','HEAD'))
    self.job.update(snapshot_required_files=True,required_files=['README.md','notes.md'])
    st.write('job-jfixture.json',self.job)
    target=Path(workspaces.prepare(self.job))
    self.assertEqual((target/'README.md').read_text(),'staged draft\n')
    self.assertEqual((target/'notes.md').read_text(),'untracked\n')
    self.assertEqual((target/'unselected.md').read_text(),'old\n')
    self.assertEqual(before,(self.git('status','--porcelain'),self.git('diff','--cached'),self.git('rev-parse','HEAD')))

  def test_cwd_base_is_explicit_and_does_not_guess_same_named_root_file(self) -> None:
    (self.repo/'sub').mkdir();(self.repo/'sub/README.md').write_text('sub original\n')
    self.git('add','sub');self.git('commit','-qm','sub fixture')
    (self.repo/'sub/README.md').write_text('sub draft\n')
    self.job.update(source_cwd=str(self.repo/'sub'),source_inputs_base='cwd',source_inputs=['README.md'])
    st.write('job-jfixture.json',self.job)
    target=Path(workspaces.prepare(self.job))
    self.assertEqual((target/'README.md').read_text(),'sub draft\n')
    self.assertEqual((target.parent/'README.md').read_text(),'committed\n')
    self.assertEqual(st.job('jfixture')['source_snapshot']['files'][0]['path'],'sub/README.md')

  def test_ignored_required_draft_fails_actual_worktree_preflight(self) -> None:
    (self.repo/'cache').mkdir();(self.repo/'cache/input.md').write_text('ignored\n')
    self.job.update(snapshot_required_files=True,required_files=['cache/input.md'])
    st.write('job-jfixture.json',self.job)
    target=Path(workspaces.prepare(self.job))
    with self.assertRaisesRegex(ValueError,'unavailable'):
      input_contract.check({'kind':'codex'},'worktree',str(target),['cache/input.md'],[])


if __name__=='__main__':unittest.main()

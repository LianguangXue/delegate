"""Unbounded total runtime retains bounded control RPCs and observable cancellation."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
import time
import threading
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/delegate/scripts'))
import runtime,storage as st,service,worker,codex_rpc

class RuntimeTests(unittest.TestCase):
  def setUp(self):
    self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'DELEGATE_STATE_DIR':self.tmp.name});self.env.start()
  def tearDown(self):self.env.stop();self.tmp.cleanup()
  def test_zero_is_explicit_and_completed_elapsed_is_frozen(self):
    runtime.validate(0,1800)
    for v in (-1,True,1801):
      with self.assertRaises(ValueError):runtime.validate(v,1800)
    self.assertEqual(runtime.deadline({'timeout_seconds':0},100),float('inf'))
    with patch('runtime.time.time',return_value=500):
      r=runtime.observe({'status':'completed','created_at':100,'finished_at':200,'timeout_seconds':0})
    self.assertEqual(r['elapsed_seconds'],100)
  def test_live_runtime_change_and_cancelling_or_legacy_rejection(self):
    st.save_session('s',{'jobs':['j'],'mode':'off'})
    st.write('job-j.json',{'id':'j','session_id':'s','status':'running','created_at':100,'timeout_seconds':7200,
                         'worker_identity':None,'runtime_control_version':1})
    with patch('service.reconcile',side_effect=lambda _:st.job('j')):
      self.assertEqual(service.jobs('s','runtime','j',timeout_seconds=0)['runtime']['mode'],'unbounded')
      st.update_job('j',status='cancelling')
      with self.assertRaises(ValueError):service.jobs('s','runtime','j',timeout_seconds=0)
      st.update_job('j',status='running',runtime_control_version=0)
      with self.assertRaises(ValueError):service.jobs('s','runtime','j',timeout_seconds=0)
  def test_codex_survives_two_hour_clock_jump_with_no_total_limit(self):
    job={'id':'j','session_id':'s','timeout_seconds':0,'provider':{'command':'codex'},'role':{'workspace':'read-only'},
         'model':'fixture','effort':'low','cwd':self.tmp.name,'cancel_requested':False,'messages':[]}
    st.write('job-j.json',job);clock=[0];calls=[]
    class RPC:
      def __init__(self,*a,**kw):pass
      def __enter__(self):return self
      def __exit__(self,*a):pass
      def call(self,method,params=None,timeout=30):
        calls.append((method,timeout));return {'account':{'type':'chatgpt'},'thread':{'id':'thread'},'turn':{'id':'turn'}}
      def receive(self,timeout):
        if clock[0]==0:clock[0]=10000;return None
        return {'method':'turn/completed','params':{'turn':{'id':'turn','status':'completed'}}}
    with patch('worker.CodexRPC',RPC),patch('worker.time.monotonic',side_effect=lambda:clock[0]):
      result=worker.codex_worker(job,'fixture')
    self.assertEqual(result['status'],'completed')
    self.assertNotIn('turn/interrupt',[m for m,t in calls])
    self.assertEqual(dict(calls)['turn/start'],120)
  def test_rpc_budget_can_change_without_extending_control_timeout(self):
    rpc=codex_rpc.CodexRPC.__new__(codex_rpc.CodexRPC);limit=[10]
    rpc.deadline=lambda:limit[0]
    self.assertEqual(rpc.budget_deadline(),10)
    limit[0]=float('inf');self.assertEqual(rpc.budget_deadline(),float('inf'))
  def test_send_rechecks_extended_budget_after_empty_select(self):
    rpc=codex_rpc.CodexRPC.__new__(codex_rpc.CodexRPC)
    clock=[0];budget=[2];rpc.deadline=lambda:budget[0];rpc.cancelled=None
    rpc.proc=Mock();rpc.proc.stdin.fileno.return_value=99
    calls=[]
    def ready(*args):
      calls.append(1)
      if len(calls)==1:clock[0]=2;budget[0]=float('inf');return ([],[],[])
      return ([],[99],[])
    with patch('codex_rpc.time.monotonic',side_effect=lambda:clock[0]), \
         patch('codex_rpc.select.select',side_effect=ready), \
         patch('codex_rpc.os.write',side_effect=lambda fd,data:len(data)):
      rpc.send({'method':'fixture'})
    self.assertEqual(len(calls),2)

  def test_heartbeats_are_not_progress(self):
    with patch('runtime.time.time',return_value=1000):
      r=runtime.observe({'status':'running','created_at':100,'heartbeat':999,'last_progress_at':100,'timeout_seconds':0})
    self.assertEqual(r['heartbeat_age_seconds'],1)
    self.assertEqual(r['progress_age_seconds'],900)
    self.assertTrue(r['warnings'])

  def active_job(self, **updates):
    st.save_session('s',{'jobs':['j'],'mode':'off'})
    job={'id':'j','session_id':'s','status':'running','created_at':1000,'execution_started_at':1000,
      'timeout_seconds':100,'runtime_control_version':1,'runtime_revision':0,'runtime_ack_expected':True,
      'cancel_requested':False,'messages':[],**updates}
    st.write('job-j.json',job)
    return job

  def test_invalid_timeout_reports_received_value_and_numeric_bounds(self):
    with self.assertRaises(ValueError) as error:runtime.validate(18000,14400)
    for text in ('18000','10..14400','0 (no total limit)','running'):
      self.assertIn(text,str(error.exception))

  def test_extend_active_task_then_observe_worker_acknowledgement(self):
    self.active_job()
    with patch('service.reconcile',side_effect=lambda _:st.job('j')),patch('time.time',return_value=1050):
      receipt=service.jobs('s','extend','j',extend_seconds=60)
      self.assertEqual(receipt['runtime']['timeout_seconds'],160)
      self.assertEqual(receipt['runtime']['remaining_seconds'],110)
      self.assertEqual(receipt['runtime']['change_state'],'pending')
      self.assertEqual(receipt['runtime']['allowed_timeout_seconds']['maximum'],1800)
      runtime.settings('j')
      receipt=service.jobs('s','runtime','j')
      self.assertEqual(receipt['runtime']['change_state'],'applied')
      self.assertEqual(receipt['runtime']['worker_applied_timeout_seconds'],160)
      self.assertEqual(receipt['runtime']['worker_applied_revision'],1)

  def test_extension_does_not_change_limits_or_revive_cancelling_work(self):
    self.active_job()
    with patch('service.reconcile',side_effect=lambda _:st.job('j')),patch('time.time',return_value=1050):
      with self.assertRaisesRegex(ValueError,'10..1800'):service.jobs('s','extend','j',extend_seconds=2000)
      self.assertEqual(st.job('j')['timeout_seconds'],100)
      for value in (0,-1,True):
        with self.assertRaises(ValueError):service.jobs('s','extend','j',extend_seconds=value)
      with self.assertRaises(ValueError):service.jobs('s','runtime','j',timeout_seconds=200,extend_seconds=10)
      st.update_job('j',status='cancelling')
      with self.assertRaises(ValueError):service.jobs('s','extend','j',extend_seconds=60)
      st.update_job('j',status='running',execution_finished_at=1050)
      with self.assertRaises(ValueError):service.jobs('s','extend','j',extend_seconds=60)

  def test_atomic_extension_wins_before_timeout_claim(self):
    self.active_job()
    with patch('service.reconcile',side_effect=lambda _:st.job('j')),patch('time.time',return_value=1200),patch('time.monotonic',return_value=200):
      receipt=service.jobs('s','extend','j',extend_seconds=60)
      self.assertEqual(receipt['runtime']['timeout_seconds'],260)
      self.assertEqual(receipt['runtime']['remaining_seconds'],60)
      self.assertIsNone(runtime.claim_stop('j',0))
      self.assertEqual(st.job('j')['status'],'running')
    self.active_job()
    with patch('time.monotonic',return_value=200):self.assertEqual(runtime.claim_stop('j',0),'timed_out')
    with patch('service.reconcile',side_effect=lambda _:st.job('j')):
      with self.assertRaises(ValueError):service.jobs('s','extend','j',extend_seconds=60)

  def test_rpc_rechecks_extension_when_previous_deadline_just_expired(self):
    self.active_job()
    original=runtime.settings;reads=[0]
    def raced(job_id):
      result=original(job_id);reads[0]+=1
      if reads[0]==1:st.update_job(job_id,timeout_seconds=300,runtime_revision=1)
      return result
    with patch('runtime.settings',side_effect=raced),patch('time.monotonic',return_value=150):
      self.assertEqual(runtime.live_deadline('j',0),300)
    self.assertEqual(st.job('j')['status'],'running')

  def test_rpc_clock_crossing_rechecks_dynamic_callback_before_abort(self):
    rpc=codex_rpc.CodexRPC.__new__(codex_rpc.CodexRPC)
    clock=[9];calls=[]
    def budget():
      calls.append(1)
      if len(calls)==1:clock[0]=11;return 10
      return 30
    rpc.deadline=budget
    with patch('time.monotonic',side_effect=lambda:clock[0]):
      self.assertEqual(rpc.wait_deadline(100,'fixture'),30)
    self.assertEqual(len(calls),2)
    rpc.deadline=lambda:float('inf')
    with patch('time.monotonic',return_value=101):
      with self.assertRaisesRegex(TimeoutError,'per-request timeout'):
        rpc.wait_deadline(100,'fixture')

  def test_unbounded_and_legacy_workers_get_explicit_control_results(self):
    self.active_job(timeout_seconds=0)
    with patch('service.reconcile',side_effect=lambda _:st.job('j')):
      with self.assertRaisesRegex(ValueError,'already has timeout_seconds=0'):
        service.jobs('s','extend','j',extend_seconds=60)
      st.update_job('j',timeout_seconds=100,runtime_ack_expected=False)
      receipt=service.jobs('s','runtime','j',timeout_seconds=0)
      self.assertEqual(receipt['runtime']['change_state'],'unconfirmed')
      st.update_job('j',runtime_control_version=0)
      with self.assertRaisesRegex(ValueError,'cannot safely extend'):
        service.jobs('s','extend','j',extend_seconds=60)

  def test_legacy_setter_without_revision_still_gets_correct_ack_value(self):
    self.active_job();runtime.settings('j')
    st.update_job('j',timeout_seconds=300)
    runtime.settings('j')
    self.assertEqual(runtime.observe(st.job('j'))['worker_applied_timeout_seconds'],300)

  def test_codex_runs_past_original_limit_after_extension(self):
    job=self.active_job(timeout_seconds=10,provider={'command':'codex'},role={'workspace':'read-only'},
      model='fixture',effort='low',cwd=self.tmp.name)
    clock=[0];calls=[]
    class RPC:
      def __init__(self,*args,**kwargs):pass
      def __enter__(self):return self
      def __exit__(self,*args):pass
      def call(self,method,params=None,timeout=30):
        calls.append(method)
        return {'account':{'type':'chatgpt'},'thread':{'id':'thread'},'turn':{'id':'turn'}}
      def receive(self,timeout):
        if clock[0]==0:
          clock[0]=8;service.jobs('s','extend','j',extend_seconds=20);return None
        if clock[0]==8:clock[0]=15;return None
        return {'method':'turn/completed','params':{'turn':{'id':'turn','status':'completed'}}}
    with patch('worker.CodexRPC',RPC),patch('time.monotonic',side_effect=lambda:clock[0]), \
         patch('time.time',side_effect=lambda:1000+clock[0]),patch('service.reconcile',side_effect=lambda _:st.job('j')):
      outcome=worker.codex_worker(job,'fixture')
    self.assertEqual(outcome['status'],'completed')
    self.assertNotIn('turn/interrupt',calls)
    self.assertEqual(st.job('j')['runtime_applied_timeout_seconds'],30)

  def test_extend_can_wait_in_code_for_worker_confirmation(self):
    self.active_job()
    def reader():
      until=time.monotonic()+3
      while time.monotonic()<until:
        if st.job('j').get('runtime_revision')==1:
          runtime.settings('j');return
        time.sleep(.01)
    thread=threading.Thread(target=reader);thread.start()
    try:
      with patch('service.reconcile',side_effect=lambda _:st.job('j')),patch('time.time',return_value=1050):
        result=service.jobs('s','extend','j',extend_seconds=60,wait_seconds=2)
    finally:thread.join(timeout=4)
    self.assertEqual(result['runtime']['change_state'],'applied')
    self.assertEqual(result['runtime']['worker_applied_timeout_seconds'],160)

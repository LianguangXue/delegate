"""Unbounded total runtime retains bounded control RPCs and observable cancellation."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
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

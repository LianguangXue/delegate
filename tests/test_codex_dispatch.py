"""Startup failures must not turn cosmetic operations into task failures."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
import time
import hashlib
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/delegate/scripts'))
import worker
import service
import codex_rpc
import storage as st

class DispatchTests(unittest.TestCase):
  def setUp(self):
    self.tmp=tempfile.TemporaryDirectory()
    self.env=patch.dict(os.environ,{'DELEGATE_STATE_DIR':self.tmp.name});self.env.start()
    self.job={'id':'jtest','provider_name':'codex','provider':{'command':'codex'},'model':'fixture',
      'effort':'low','timeout_seconds':300,'role':{'workspace':'read-only','instructions':'fixture'},
      'cwd':self.tmp.name,'source_cwd':self.tmp.name,'session_id':'s','cancel_requested':False,
      'messages':[],'task':'fixture','acceptance':'fixture','context':''}
    st.write('job-jtest.json',self.job)
  def tearDown(self):
    self.env.stop();self.tmp.cleanup()
  def test_no_name_rpc_and_bounded_resume(self):
    calls=[]
    class RPC:
      def __init__(self,*a,**kw):pass
      def __enter__(self):return self
      def __exit__(self,*a):pass
      def call(self,method,params=None,timeout=30):
        calls.append((method,timeout))
        if method=='thread/name/set':raise TimeoutError('cosmetic operation')
        return {'account':{'type':'chatgpt'},'thread':{'id':'thread'},'turn':{'id':'turn'}}
      def receive(self,timeout):return {'method':'turn/completed','params':{'turn':{'id':'turn','status':'completed'}}}
    with patch('worker.CodexRPC',RPC),patch('worker.thread_cleanup.restore'):
      outcome=worker.codex_worker({**self.job,'thread_id':'thread','resume_job':'prior'},'fixture')
    self.assertEqual(outcome['status'],'completed')
    self.assertNotIn('thread/name/set',[m for m,t in calls])
    self.assertEqual(dict(calls)['thread/resume'],120)
    self.assertEqual(dict(calls)['turn/start'],120)
  def test_large_instructions_are_referenced_without_aborting_or_duplicating(self):
    p=Path(self.tmp.name)/'AGENTS.md';p.write_text('x'*40001)
    (p.parent/'CLAUDE.md').symlink_to(p)
    prompt=worker.packet(self.job)
    self.assertIn('read this entire file',prompt)
    self.assertEqual(prompt.count('REQUIRED INSTRUCTION FILE:'),1)
    self.assertNotIn('x'*40001,prompt)

  def test_expired_deadline_and_cancel_never_send_request(self):
    rpc=codex_rpc.CodexRPC.__new__(codex_rpc.CodexRPC)
    rpc.deadline=time.monotonic()-1;rpc.send=Mock();rpc.cancelled=None
    with self.assertRaises(TimeoutError):rpc.call('turn/start')
    rpc.send.assert_not_called()
    rpc.deadline=None;rpc.cancelled=lambda: True
    with self.assertRaises(InterruptedError):rpc.call('thread/resume')
    rpc.send.assert_not_called()

  def test_model_cooldown_survives_quota_refresh_and_expires(self):
    model='gpt-6.1-sol'
    name='cooldown-codex-'+hashlib.sha256(model.encode()).hexdigest()+'.json'
    st.write(name,{'expires_at':time.time()+60,'model':model})
    st.write('quota-codex.json',{'status':'known','observed_at':time.time(),'data':{}})
    self.assertIsNotNone(service.model_cooldown('codex',model))
    self.assertIsNone(service.model_cooldown('codex','other/model'))
    st.write(name,{'expires_at':time.time()-1,'model':model})
    self.assertIsNone(service.model_cooldown('codex',model))

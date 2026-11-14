"""Cross-version locking, runtime retention and notification repair without real jobs."""
from pathlib import Path
from unittest.mock import patch
import fcntl
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/delegate/scripts'))
import accounting
import delegate
import gc_store
import registration
import result_delivery
import storage as st
import thread_cleanup


class CrossVersionSafetyTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix='delegate-cross-version-')
    self.env = patch.dict(os.environ, {'DELEGATE_STATE_DIR': self.temp.name})
    self.env.start()

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def job(self, jid: str) -> dict:
    task = accounting.begin('s')
    return {'id': jid, 'session_id': 's', 'task_id': task['id'], 'status': 'completed',
      'result': 'Full fixture result', 'review': 'pending', 'provider': {'kind': 'codex'},
      'provider_name': 'codex', 'model': 'fixture', 'agent': 'codex-reviewer', 'effort': 'low',
      'finished_at': time.time(), 'await_completion': True}

  def publish_legacy(self, job: dict) -> None:
    """Emulate legacy registration that updates jobs but knows nothing about pending indexes."""
    with st.locked():
      st.write('job-'+job['id']+'.json', job)
      state = st.session('s');state['jobs'].append(job['id']);st.save_session('s', state)
      task = accounting.load(job['task_id']);task['jobs'].append(job['id']);accounting.save(task)

  def hook(self) -> dict:
    return delegate.hook({'session_id': 's', 'hook_event_name': 'PostToolUse', 'tool_name': 'Read'})

  def test_both_historical_lock_paths_block_new_callers(self) -> None:
    for name in ('thread-x', 'rewake-x', 'loop-controller-x', 'loop-rewake-x', 'claude-usage-refresh'):
      for held_path in (st.root()/(name+'.lock'), st.lock_path(name)):
        with held_path.open('a') as held:
          fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
          with st.coordination_files(name) as streams:
            with self.assertRaises(BlockingIOError):
              st.try_coordination_lock(streams)
        with st.coordination_files(name) as streams:
          st.try_coordination_lock(streams)

  def test_guard_and_partial_lock_release(self) -> None:
    legacy = st.root()/'thread-x.lock'
    modern = st.lock_path('thread-x')
    with modern.open('a') as held:
      fcntl.flock(held, fcntl.LOCK_EX)
      with self.assertRaisesRegex(ValueError, 'in progress'):
        with thread_cleanup.guard('x'):
          self.fail('Entered while modern runtime holds the lock')
      with legacy.open('a') as free:
        fcntl.flock(free, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Partial acquisition was released.
    with thread_cleanup.guard('x'):
      for path in (legacy, modern):
        with path.open('a') as contender:
          with self.assertRaises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

  def test_gc_never_removes_idle_or_held_coordination_inodes(self) -> None:
    paths = [st.root()/'thread-x.lock', st.lock_path('thread-x'), st.root()/'idle.lock', st.lock_path('idle')]
    old = time.time()-3*86400
    for path in paths:
      path.touch();os.utime(path,(old,old))
    before = {p:p.stat().st_ino for p in paths}
    with paths[0].open('a') as held:
      fcntl.flock(held, fcntl.LOCK_EX)
      for apply in (False,True):
        result = gc_store.collect_garbage(apply=apply)
        self.assertEqual(result['counts']['locks'],0)
        self.assertEqual({p:p.stat().st_ino for p in paths},before)
      with self.assertRaises(ValueError):
        with thread_cleanup.guard('x'):
          self.fail('GC split the lock inode')

  def test_gc_preserves_paused_loop_runtime_without_children_and_idle_server_image(self) -> None:
    old = time.time()-3*86400
    for digest in ('a'*64,'b'*64):
      path=st.root()/'runtimes'/digest;path.mkdir(parents=True)
      (path/'delegate.py').write_text('# fixture controller\n');os.utime(path,(old,old))
    runtime=st.root()/'runtimes'/('a'*64)
    st.write('loop-lfixture.json',{'id':'lfixture','status':'paused','children':[],
      'runtime_image':{'digest':'a'*64,'path':str(runtime)}})
    with patch('gc_store._launcher_runtime_digest',return_value=None):
      for apply in (False,True):
        result=gc_store.collect_garbage(apply=apply)
        self.assertEqual(result['counts']['runtimes'],0)
        self.assertEqual(result['counts']['runtimes_retained'],1)
        for digest in ('a'*64,'b'*64):
          self.assertTrue((st.root()/'runtimes'/digest/'delegate.py').is_file())

  def test_retained_completed_loop_keeps_its_resume_child(self) -> None:
    job=self.job('jresume');old=time.time()-30*86400
    job.update(finished_at=old,collected_at=old)
    self.publish_legacy(job)
    task=accounting.load(job['task_id']);task['status']='closed';accounting.save(task)
    st.write('loop-lfixture.json',{'id':'lfixture','status':'completed','current_job':job['id'],'children':[]})
    result=gc_store.collect_garbage(apply=True)
    self.assertEqual(result['counts']['jobs'],0)
    self.assertEqual(st.job(job['id'])['result'],job['result'])

  def test_legacy_registration_after_index_initialization_is_discovered(self) -> None:
    first=self.job('jfirst')
    with st.locked():registration.finish_locked({'job':first})
    result_delivery.acknowledge(first,result_delivery.fingerprint(first['result']),0,len(first['result']),len(first['result']))
    self.hook()
    self.assertEqual(st.session('s')['pending_jobs'],[])
    second=self.job('jlegacy');self.publish_legacy(second)
    output=self.hook()
    self.assertIn('completion-jlegacy',json.dumps(output))
    self.assertEqual(st.session('s')['pending_jobs'],['jlegacy'])
    result_delivery.acknowledge(second,result_delivery.fingerprint(second['result']),0,len(second['result']),len(second['result']))
    self.hook()
    with patch('service.reconcile') as reconcile:
      self.hook();reconcile.assert_not_called()

  def test_v25_index_without_seen_marker_backfills_once(self) -> None:
    job=self.job('jlegacy');self.publish_legacy(job)
    state=st.session('s');state['pending_jobs']=[];st.save_session('s',state)
    self.assertIn('completion-jlegacy',json.dumps(self.hook()))
    self.assertEqual(st.session('s')['pending_jobs_seen'],['jlegacy'])

  def test_concurrent_registration_is_not_lost_during_pending_prune(self) -> None:
    first=self.job('jfirst');self.publish_legacy(first)
    second=self.job('jsecond')
    def notification(jid: str, **kwargs) -> str:
      self.publish_legacy(second)
      return ''
    with patch('delivery.notice',side_effect=notification):
      self.hook()
    self.assertIn('jsecond',st.session('s')['pending_jobs'])
    self.assertIn('completion-jsecond',json.dumps(self.hook()))


if __name__ == '__main__':
  unittest.main()

"""Task separation and evidence invalidation with disposable files."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/delegate/scripts'))
import storage as st
import handoff
import inspection
import optimizer

class EnhancementTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory()
    self.env = patch.dict(os.environ, {'DELEGATE_STATE_DIR':self.temp.name})
    self.env.start()
    self.root = Path(self.temp.name)
    st.save_session('s', {'mode':'off','jobs':['j1','j2']})
    for i in (1,2):
      st.write(f'task-t{i}.json', {'id':f't{i}','session_id':'s'})
      st.write(f'job-j{i}.json', {'id':f'j{i}','task_id':f't{i}','session_id':'s','work_item':'w',
        'cwd':str(self.root),'status':'completed','review':'accepted','provider':{'kind':'codex'}})
    (self.root/'file.txt').write_text('original')
    self.am = self.root/'am'
    (self.am/'tasks').mkdir(parents=True)
    (self.am/'config.json').write_text('{}')
    for n in ('a','b'):(self.am/'tasks'/f'{n}.json').write_text('{}')

  def tearDown(self) -> None:
    self.env.stop();self.temp.cleanup()

  def test_scopes_never_mix_tasks_even_with_same_work_item(self) -> None:
    with patch('handoff.subprocess.run', side_effect=OSError('offline')):
      handoff.link('s','bind','a',str(self.am),delegate_task_id='t1')
      handoff.link('s','bind','b',str(self.am),delegate_task_id='t2')
      a=handoff.link('s',delegate_task_id='t1')['snapshot']
      b=handoff.link('s',delegate_task_id='t2')['snapshot']
    self.assertEqual([j['id'] for j in a['jobs']],['j1'])
    self.assertEqual([j['id'] for j in b['jobs']],['j2'])
    self.assertNotEqual(a['source_id'],b['source_id'])
    with self.assertRaises(ValueError):
      handoff.link('s','bind','b',str(self.am),delegate_task_id='t1',work_item='w')

  def test_changed_evidence_blocks_reuse_and_checkpoint_requires_revalidation(self) -> None:
    inspection.inspect('s','checkpoint','j1',['file.txt'])
    self.assertEqual(st.job('j1')['review'],'pending')
    st.update_job('j1', review='accepted')
    task={'jobs':['j1'],'max_attempts':3}
    self.assertEqual(optimizer.work_state(task,'w')['action'],'reuse')
    (self.root/'file.txt').write_text('changed')
    self.assertEqual(inspection.inspect('s',job_id='j1')['freshness'],'stale')
    self.assertEqual(optimizer.work_state(task,'w')['action'],'replan')
    with self.assertRaises(ValueError):inspection.inspect('s','checkpoint','j1',['../outside'])

  def test_symlinks_fifo_and_live_siblings_are_not_fresh_evidence(self) -> None:
    (self.root/'link').symlink_to(self.root/'file.txt')
    with self.assertRaises(ValueError):inspection.inspect('s','checkpoint','j1',['link'])
    os.mkfifo(self.root/'pipe')
    with self.assertRaises(ValueError):inspection.inspect('s','checkpoint','j1',['pipe'])
    inspection.inspect('s','checkpoint','j1',['file.txt'])
    st.update_job('j2',status='running')
    self.assertTrue(inspection.inspect('s',job_id='j1')['blockers'])

  def test_preflight_reports_live_and_unknown_and_metrics_missing_usage(self) -> None:
    st.update_job('j1',status='running')
    self.assertTrue(inspection.inspect('s',job_id='j1')['blockers'])
    with self.assertRaises(ValueError):inspection.inspect('s','checkpoint','j1',['file.txt'])
    report=inspection.inspect('s','metrics')
    self.assertEqual(sum(g['missing_usage'] for g in report['groups'].values()),2)

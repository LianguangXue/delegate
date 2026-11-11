"""Explicit commits in disposable worktrees never touch the source checkout."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import signal
import json
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/delegate/scripts'))
import commits
import storage as st
import worker
import lifecycle

class CommitTests(unittest.TestCase):
  def setUp(self):
    self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    self.env=patch.dict(os.environ,{'DELEGATE_STATE_DIR':str(self.root/'state')});self.env.start()
    self.src=self.root/'source';self.src.mkdir()
    def run(*args):subprocess.run(['git','-C',str(self.src),*args],check=True,capture_output=True)
    run('init');run('config','user.name','Fixture');run('config','user.email','fixture@example.test');run('config','commit.gpgsign','false')
    (self.src/'answer.txt').write_text('old');run('add','answer.txt');run('commit','-m','base')
    self.wt=self.root/'worktree';run('worktree','add','--detach',str(self.wt))
    self.job={'id':'jfixture','allow_commit':True,'commit_message':'Implement answer','commit_paths':['answer.txt'],
      'source_cwd':str(self.src),'cwd':str(self.wt),'worktree':str(self.wt),'role':{'workspace':'worktree'}}
    st.write('job-jfixture.json',self.job)
  def tearDown(self):self.env.stop();self.tmp.cleanup()
  def test_commit_only_explicit_paths_preserves_source_and_other_edits(self):
    commits.prepare(self.job)
    (self.wt/'answer.txt').write_text('new');(self.wt/'other.txt').write_text('keep uncommitted')
    result=commits.create(st.job('jfixture'))
    self.assertEqual(result['status'],'committed')
    self.assertFalse(result['pushed'])
    self.assertEqual((self.src/'answer.txt').read_text(),'old')
    self.assertIn('other.txt',commits.git(self.wt,'status','--porcelain'))
    self.assertEqual(commits.git(self.src,'rev-parse','HEAD'),result['parent'])
  def test_no_authorization_read_only_and_escaping_paths_rejected(self):
    for paths in [['../answer.txt'],['.'],['.git/config'],['/tmp/file']]:
      with self.assertRaises(ValueError):commits.validate(True,'message',paths,'worktree')
    with self.assertRaises(ValueError):commits.validate(True,'message',['x'],'read-only')
    with self.assertRaises(ValueError):commits.validate(False,'message',['x'],'worktree')
    commits.validate(False,'',None,'read-only')
  def test_dirty_start_and_unexpected_staging_or_head_are_preserved(self):
    (self.wt/'answer.txt').write_text('existing')
    with self.assertRaises(ValueError):commits.prepare(self.job)
    commits.git(self.wt,'restore','answer.txt');commits.prepare(self.job)
    (self.wt/'answer.txt').write_text('new');commits.git(self.wt,'add','answer.txt')
    with self.assertRaises(ValueError):commits.create(st.job('jfixture'))
    self.assertTrue(commits.git(self.wt,'diff','--cached','--name-only'))
  def test_malformed_paths_and_source_checkout_are_rejected(self):
    with self.assertRaises(ValueError):commits.validate(True,'message','answer.txt','worktree')
    with self.assertRaises(ValueError):commits.validate(True,None,['answer.txt'],'worktree')
    sub=self.src/'sub';sub.mkdir()
    with self.assertRaises(ValueError):commits.workspace({**self.job,'source_cwd':str(sub),'worktree':str(self.src)})

  def test_literal_pathspec_and_git_environment_cannot_broaden_commit(self):
    commits.prepare(self.job)
    (self.wt/'answer.txt').write_text('new')
    (self.wt/'*.txt').write_text('literal wildcard filename')
    value={**st.job('jfixture'),'commit_paths':['*.txt']}
    with patch.dict(os.environ,{'GIT_WORK_TREE':str(self.src),'GIT_INDEX_FILE':str(self.root/'wrong-index')}):
      result=commits.create(value)
    self.assertEqual(result['files'],['*.txt'])
    self.assertIn('answer.txt',commits.git(self.wt,'status','--porcelain'))
    self.assertFalse((self.root/'wrong-index').exists())

  def test_cancel_during_staging_prevents_commit_and_keeps_edits(self):
    commits.prepare(self.job);(self.wt/'answer.txt').write_text('new')
    original=commits.git
    def cancel_after_add(cwd,*args):
      result=original(cwd,*args)
      if args[0]=='add':st.update_job('jfixture',cancel_requested=True)
      return result
    with patch('commits.git',side_effect=cancel_after_add):
      with self.assertRaises(InterruptedError):commits.create(st.job('jfixture'))
    self.assertEqual(commits.git(self.wt,'rev-parse','HEAD'),commits.git(self.src,'rev-parse','HEAD'))
    self.assertTrue(commits.git(self.wt,'diff','--cached','--name-only'))

  def test_worker_finalizes_commit_after_file_backend_success(self):
    value={**self.job,'provider':{'kind':'claude'},'cancel_requested':False,'resume_job':'prior',
      'role':{'workspace':'worktree','instructions':'fixture'},'task':'implement','acceptance':'fixture','context':''}
    st.write('job-jfixture.json',value)
    def backend(job,prompt):
      (self.wt/'answer.txt').write_text('new')
      return {'status':'completed','result':'fixture checked'}
    with patch('worker.claude_worker',side_effect=backend), patch('worker.signal.signal'), \
         patch('worker.runtime_image.require_worker'):
      worker.main('jfixture')
    result=st.job('jfixture')
    self.assertEqual(result['status'],'completed')
    self.assertEqual(result['git_commit']['status'],'committed')
    self.assertEqual((self.src/'answer.txt').read_text(),'old')

  def test_git_process_identity_survives_worker_crash(self):
    script = """import sys,os,time
sys.path.insert(0,sys.argv[1])
import commits,storage as st,lifecycle
st.update_job('jfixture',status='running',created_at=time.time()-60,worker_identity=lifecycle.identity(os.getpid()))
token=commits._CURRENT_JOB.set('jfixture')
commits.git(__import__('pathlib').Path(sys.argv[2]),'-c','alias.pause=!sleep 20','pause')
"""
    proc=subprocess.Popen([sys.executable,'-c',script,str(Path(commits.__file__).parent),str(self.wt)])
    try:
      for _ in range(100):
        record=st.job('jfixture').get('backend_identity')
        if record and lifecycle.group_running(record):break
        time.sleep(.02)
      self.assertTrue(record)
      proc.kill();proc.wait(timeout=5)
      self.assertEqual(lifecycle.reconcile('jfixture')['status'],'orphaned')
      lifecycle.cancel_orphan('jfixture')
      self.assertFalse(lifecycle.group_running(record))
    finally:
      if proc.poll() is None:proc.kill();proc.wait()
      record=st.job('jfixture').get('backend_identity')
      if record and lifecycle.alive(record):
        try:os.killpg(record['pid'],signal.SIGKILL)
        except ProcessLookupError:pass

  def test_file_only_backends_are_not_granted_shell(self):
    record={'provider':{'kind':'claude','command':'claude'},'model':'haiku','effort':'low',
      'role':{'workspace':'worktree'},'allow_commit':True}
    cmd,_=worker.claude_command(record)
    self.assertNotIn('Bash',cmd[cmd.index('--tools')+1])
    self.assertIn('runtime will create',worker.commit_instruction(self.job))
    self.assertIn('not authorized',worker.commit_instruction({}))

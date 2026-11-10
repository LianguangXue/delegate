"""Host ownership, parent accounting boundaries and no recursive delegation."""
from pathlib import Path
from unittest.mock import patch
import os,sys,tempfile,json,subprocess,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/delegate/scripts'))
import hosts,delegate,storage as st,accounting as ac,service,optimizer

class HostTests(unittest.TestCase):
  def setUp(self):
    self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'DELEGATE_STATE_DIR':self.tmp.name,'DELEGATE_CALLER_HOST':'codex'});self.env.start()
  def tearDown(self):self.env.stop();self.tmp.cleanup()
  def test_native_ids_stable_and_host_namespaces_distinct(self):
    a=hosts.connect('native');b=hosts.connect('native')
    self.assertEqual(a['session_id'],b['session_id']);hosts.check({'session_id':a['session_id']})
    with patch.dict(os.environ,{'DELEGATE_CALLER_HOST':'dsh'}):
      d=hosts.connect('native');self.assertNotEqual(a['session_id'],d['session_id'])
      self.assertIsNone(d['parent_provider'])
      with self.assertRaises(ValueError):hosts.check({'session_id':a['session_id']})
  def test_worker_cannot_connect_or_dispatch(self):
    with patch.dict(os.environ,{'DELEGATE_WORKER':'1'}):
      with self.assertRaises(ValueError):hosts.connect('native')
      with self.assertRaises(ValueError):delegate.dispatch('delegate_catalog',{})
  def test_finish_without_claude_stop_hook_closes_and_usage_stays_unknown(self):
    sid=hosts.connect('native')['session_id'];ac.begin(sid)
    r=ac.task_tool(sid,'finish',verdict='accepted',evidence='fixture checked')
    self.assertEqual(r['status'],'closed');self.assertFalse(r['measurement_complete'])
    self.assertIsNone(r['parent'])
    self.assertIsNone(st.session(sid).get('active_task'))
    self.assertEqual(ac.begin(sid)['status'],'open')
  def test_host_checks_are_bidirectional(self):
    sid=hosts.connect('native')['session_id']
    with patch.dict(os.environ,{'DELEGATE_CALLER_HOST':'claude'}):
      with self.assertRaises(ValueError):hosts.check({'session_id':sid})
  def test_dsh_unknown_quota_does_not_borrow_claude_pressure(self):
    with patch.dict(os.environ,{'DELEGATE_CALLER_HOST':'dsh'}):sid=hosts.connect('native')['session_id']
    service.control(sid,'on',agents=['codex-reviewer'])
    ac.begin(sid,category='lookup',complexity='simple')
    st.write('claude-quota-'+sid+'.json',{'status':'known','observed_at':__import__('time').time(),
      'data':{'five_hour':{'used_percentage':99}}})
    with patch('service.catalog',return_value={'models':[{'model':'fixture-luna','supportedReasoningEfforts':[{'reasoningEffort':'low'}]}]}), \
         patch('service.quota',return_value={'status':'unknown'}) as quota:
      plan=optimizer.plan(sid,refresh=True)
    self.assertEqual(plan['action'],'main')
    self.assertEqual(plan['funding'],'parent cost unknown')
    self.assertFalse(any(c.args[0]=='claude' for c in quota.call_args_list))
  def test_preserve_parent_excludes_same_provider_workers(self):
    sid=hosts.connect('native')['session_id']
    service.control(sid,'on',agents=['codex-reviewer'],strategy='preserve-parent')
    ac.begin(sid,category='lookup',complexity='simple')
    with patch('service.quota',return_value={'status':'unknown'}):
      self.assertEqual(optimizer.plan(sid)['action'],'replan')

  def test_worker_does_not_create_invalid_missing_mcp_table(self):
    import codex_rpc
    from unittest.mock import Mock
    proc=Mock(pid=12345)
    with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,{'CODEX_HOME':home}), \
         patch('codex_rpc.subprocess.Popen',return_value=proc) as spawn, \
         patch('codex_rpc.threading.Thread'), patch('codex_rpc.identity',return_value={'pid':12345}), \
         patch('codex_rpc.os.set_blocking'),patch.object(codex_rpc.CodexRPC,'call',return_value={}), \
         patch.object(codex_rpc.CodexRPC,'send'):
      codex_rpc.CodexRPC('codex')
      self.assertNotIn('mcp_servers.delegate.enabled=false',spawn.call_args.args[0])
      self.assertEqual(spawn.call_args.kwargs['env']['DELEGATE_WORKER'],'1')
      __import__('pathlib').Path(home,'config.toml').write_text('[mcp_servers.delegate]\ncommand="python3"\n')
      codex_rpc.CodexRPC('codex')
      self.assertIn('mcp_servers.delegate.enabled=false',spawn.call_args.args[0])

  def test_host_mcp_entrypoint_and_tool_discovery(self):
    script=Path(delegate.__file__)
    reqs=[{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'delegate_host','arguments':{'native_session_id':'fixture'}}},
          {'jsonrpc':'2.0','id':2,'method':'tools/list'}]
    p=subprocess.run([sys.executable,str(script),'host-mcp','dsh'],input=''.join(json.dumps(r)+'\n' for r in reqs),capture_output=True,text=True,check=True,timeout=15)
    rows=[json.loads(l) for l in p.stdout.splitlines()]
    value=json.loads(rows[0]['result']['content'][0]['text']);self.assertEqual(value['host'],'dsh')
    self.assertTrue(any(t['name']=='delegate_host' for t in rows[1]['result']['tools']))

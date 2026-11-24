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

  def test_antigravity_host_registration_alias_and_claude_roles(self) -> None:
    """Support antigravity/agy host registration, MCP handshake, waiter/export hints, and Claude roles."""
    with patch.dict(
      os.environ,
      {'DELEGATE_CALLER_HOST': 'agy', 'ANTIGRAVITY_CONVERSATION_ID': 'agy-conv-1234'},
    ):
      agy = hosts.connect()
    self.assertTrue(agy['session_id'].startswith('antigravity-'))
    self.assertEqual(agy['native_session_id'], 'agy-conv-1234')
    self.assertEqual(agy['host'], 'antigravity')
    with patch.dict(os.environ, {'DELEGATE_CALLER_HOST': 'antigravity'}):
      hosts.check({'session_id': agy['session_id']})
      self.assertIn('claude-reviewer', st.DEFAULT_CONFIG['agents'])
      self.assertIn('claude-builder', st.DEFAULT_CONFIG['agents'])
      ctrl = service.control(agy['session_id'], 'on')
      self.assertIn('antigravity-host-v27-0', ctrl['server_runtime']['capabilities'])
      self.assertIn('claude-subagents-v27-0', ctrl['server_runtime']['capabilities'])
      ac.begin(agy['session_id'])
      finished = ac.task_tool(agy['session_id'], 'finish', verdict='accepted', evidence='agy verified')
      self.assertEqual(finished['status'], 'closed')

    import result_delivery, completion, persistent_tasks
    job = {
      'id': 'jagy1', 'session_id': agy['session_id'], 'status': 'completed',
      'agent': 'claude-reviewer', 'provider_name': 'claude', 'model': 'sonnet',
      'effort': 'medium', 'result': 'ok',
    }
    st.write('job-jagy1.json', job)
    self.assertIn('delegate-agent wait antigravity', completion.describe(job)['wait_command'])
    self.assertIn('--host antigravity', result_delivery.receipt(job)['export_command'])
    loop_rec = {
      'id': 'lagy1', 'session_id': agy['session_id'], 'mode': 'persistent',
      'status': 'paused', 'requested': 'pause', 'note': '', 'task_id': finished['id'],
      'cycle': 1, 'attempt': 1, 'completed_cycles': 0, 'max_cycles': 0,
      'token_budget': 0, 'interval_seconds': 0, 'next_run_at': None,
      'current_job': None, 'previous_job': None, 'event_seq': 0, 'ack_seq': 0,
      'review': 'pending', 'goal_acceptance': 'done', 'usage': {},
    }
    self.assertIn('loop-wait antigravity', persistent_tasks.describe(loop_rec)['wait_command'])

    with tempfile.TemporaryDirectory() as home:
      home_path = Path(home)
      gemini_cfg = home_path / '.gemini' / 'config'
      gemini_cfg.mkdir(parents=True)
      (gemini_cfg / 'mcp_config.json').write_text('')  # empty existing file like default Antigravity installs
      installer = Path(__file__).resolve().parents[1] / 'scripts' / 'install-hosts.py'
      subprocess.run(
        [sys.executable, str(installer), 'antigravity'],
        env={**os.environ, 'HOME': home, 'GEMINI_CONFIG_DIR': str(home_path / 'ignored-config')},
        check=True, capture_output=True, text=True,
      )
      self.assertFalse((home_path / 'ignored-config').exists())
      mcp_data = json.loads((gemini_cfg / 'mcp_config.json').read_text())
      self.assertEqual(mcp_data['mcpServers']['delegate']['args'], ['host-mcp', 'antigravity'])
      self.assertTrue((gemini_cfg / 'skills' / 'delegate').is_symlink())
      launcher_text = (home_path / '.local' / 'bin' / 'delegate-agent').read_text()
      self.assertIn('.local/bin', launcher_text)
      self.assertIn('host-mcp:antigravity', launcher_text)

    script = Path(delegate.__file__)
    reqs = [
      {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
      {
        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
        'params': {'name': 'delegate_host', 'arguments': {'native_session_id': 'agy-rpc-1'}},
      },
      {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
    ]
    proc = subprocess.run(
      [sys.executable, str(script), 'host-mcp', 'antigravity'],
      input=''.join(json.dumps(r) + '\n' for r in reqs),
      capture_output=True, text=True, check=True, timeout=15,
    )
    rows = [json.loads(line) for line in proc.stdout.splitlines()]
    self.assertIn('delegate_host', rows[0]['result'].get('instructions', ''))
    val = json.loads(rows[1]['result']['content'][0]['text'])
    self.assertEqual(val['host'], 'antigravity')
    self.assertTrue(any(t['name'] == 'delegate_host' for t in rows[2]['result']['tools']))
    with patch.dict(os.environ, {'DELEGATE_CALLER_HOST': 'claude'}):
      self.assertNotIn('delegate_host', {t['name'] for t in delegate.tool_list()})

  def test_agy_roles_and_advisory_contracts_end_to_end(self) -> None:
    """Verify 0.28.0 antigravity provider/roles and workflow/blind_context/review_gate end-to-end."""
    from unittest.mock import Mock
    import worker
    service.validate_config(st.DEFAULT_CONFIG)
    agy_prov = st.DEFAULT_CONFIG['providers']['antigravity']
    self.assertEqual(agy_prov['kind'], 'antigravity')
    self.assertEqual(agy_prov['models'], [])
    self.assertEqual(agy_prov['efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])
    for role_name, expected_ws in (('agy-advisor', 'read-only'), ('agy-builder', 'worktree')):
      role_cfg = st.DEFAULT_CONFIG['agents'][role_name]
      self.assertEqual(role_cfg['provider'], 'antigravity')
      self.assertTrue(role_cfg['enabled'])
      self.assertFalse(role_cfg['automatic'])
      self.assertEqual(role_cfg['workspace'], expected_ws)

    sid = hosts.connect('native-v28')['session_id']
    ctrl = service.control(sid, 'on')
    caps = ctrl['server_runtime']['capabilities']
    self.assertIn('agy-worker-v28-0', caps)
    self.assertIn('advisory-dispatch-v28-0', caps)

    tools = {t['name']: t for t in delegate.tool_list()}
    self.assertLess(len(json.dumps(list(tools.values()), ensure_ascii=False).encode('utf-8')), 24000)
    for tool_name in ('delegate_plan', 'delegate_run'):
      props = tools[tool_name]['inputSchema']['properties']
      for field in ('workflow', 'blind_context', 'review_gate', 'estimated_lines'):
        self.assertIn(field, props)

    fake_agy_cat = {
      'provider_kind': 'antigravity',
      'models': ['gemini-3.1-pro', 'gemini-3-flash'],
      'efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
      'default_model': 'gemini-3.1-pro',
      'model_details': [
        {'id': 'gemini-3.1-pro', 'effort': {'supported_levels': ['low', 'medium', 'high', 'xhigh']}},
        {'id': 'gemini-3-flash', 'effort': {'supported_levels': ['low', 'medium', 'high']}},
      ],
      'source': 'agy models',
    }
    fake_codex_cat = {
      'provider': 'codex',
      'models': [{
        'model': 'gpt-6.2-sol',
        'supportedReasoningEfforts': [
          {'reasoningEffort': 'medium'},
          {'reasoningEffort': 'high'},
          {'reasoningEffort': 'xhigh'},
        ],
      }],
    }
    fake_agy_mod = Mock()
    fake_agy_mod.DEFAULT_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
    fake_agy_mod.catalog.return_value = fake_agy_cat

    with patch.dict(sys.modules, {'agy_worker': fake_agy_mod}):
      cat = service.catalog(agent_name='agy-advisor')
      self.assertEqual(cat['models'], ['gemini-3.1-pro', 'gemini-3-flash'])
      self.assertEqual(cat['default_model'], 'gemini-3.1-pro')
      service.choose(
        st.DEFAULT_CONFIG['agents']['agy-advisor'], agy_prov, 'antigravity', 'gemini-3.1-pro', 'high',
      )
      with self.assertRaisesRegex(ValueError, 'effort|allowlist'):
        service.choose(
          st.DEFAULT_CONFIG['agents']['agy-advisor'], agy_prov, 'antigravity', 'gemini-3-flash', 'xhigh',
        )

    with tempfile.TemporaryDirectory() as repo_dir:
      subprocess.run(['git', '-C', repo_dir, 'init', '-q'], check=True)
      Path(repo_dir, 'auth.py').write_text('def verify_token() -> bool:\n  return True\n')
      subprocess.run(['git', '-C', repo_dir, 'add', '.'], check=True)
      subprocess.run(
        ['git', '-C', repo_dir, '-c', 'user.name=T', '-c', 'user.email=t@e', 'commit', '-qm', 'init'],
        check=True,
      )

      with patch('service.quota', return_value={'status': 'unknown'}), \
           patch('service._catalog', side_effect=lambda p, r=False: fake_codex_cat if p == 'codex' else fake_agy_cat):
        inline_plan = delegate.dispatch('delegate_plan', {
          'session_id': sid,
          'work_item': 'tiny-edit',
          'workflow': 'backend-module',
          'estimated_lines': 20,
        })
        self.assertIn('advisory', inline_plan)
        self.assertEqual(inline_plan['advisory']['action'], 'keep_inline')

      fake_child = Mock(pid=43210)
      fake_img = {'path': str(Path(delegate.__file__).parent), 'digest': '0' * 64}
      with patch.dict(sys.modules, {'agy_worker': fake_agy_mod}), \
           patch('service.quota', return_value={'status': 'unknown'}), \
           patch('service.subprocess.Popen', return_value=fake_child), \
           patch('service.identity', return_value={'pid': 43210, 'start': '1', 'boot': 'b'}), \
           patch('runtime_image.freeze', return_value=fake_img):
        # 1. Normal call incurs zero extra prompt/context tokens.
        normal_rec = service.start(
          sid, 'agy-advisor', 'gemini-3.1-pro', 'medium', 'Inspect auth.py', 'Report facts',
          'Quick check', repo_dir, context='', one_shot=True,
        )
        normal_job = st.job(normal_rec['id'])
        self.assertEqual(normal_job['context'], '')
        self.assertEqual(normal_job['workflow'], 'auto')
        self.assertFalse(normal_job['blind_context'])
        self.assertFalse(normal_job['review_gate'])
        st.update_job(normal_rec['id'], status='completed', finished_at=1.0)

        # 2. Blind context blocks parent draft leakage in context/task and resume_job; dual-plan implies blind_context.
        with self.assertRaisesRegex(ValueError, 'blind_context=True'):
          service.start(
            sid, 'agy-advisor', 'gemini-3.1-pro', 'high', 'Design auth service', 'Independent plan',
            'Second opinion', repo_dir, context='Here is my draft plan: use JWT', one_shot=True,
            workflow='dual-plan',
          )
        with self.assertRaisesRegex(ValueError, 'blind_context=True'):
          service.start(
            sid, 'agy-advisor', 'gemini-3.1-pro', 'high', 'Review my draft plan: use JWT', 'Independent plan',
            'Second opinion', repo_dir, context='', one_shot=True,
            workflow='dual-plan',
          )
        with self.assertRaisesRegex(ValueError, 'resume_job'):
          service.start(
            sid, 'agy-advisor', 'gemini-3.1-pro', 'high', 'Design auth service', 'Independent plan',
            'Second opinion', repo_dir, context='', one_shot=True,
            resume_job=normal_rec['id'], workflow='dual-plan',
          )
        blind_rec = service.start(
          sid, 'agy-advisor', 'gemini-3.1-pro', 'high', 'Design auth service', 'Independent plan',
          'Second opinion', repo_dir, context='Repo uses Python 3.12.', one_shot=True,
          workflow='dual-plan',
        )
        blind_job = st.job(blind_rec['id'])
        self.assertTrue(blind_job['blind_context'])
        self.assertEqual(blind_job['workflow'], 'dual-plan')
        self.assertIn('[speculative]', blind_job['context'])
        self.assertIn('[speculative]', worker.packet(blind_job))
        st.update_job(blind_rec['id'], status='completed', finished_at=2.0)

      # 3. workflow='gate-review' automatically enables review_gate=True and injects 2-tier contract.
      with patch('service.quota', return_value={'status': 'unknown'}), \
           patch('service._catalog', return_value=fake_codex_cat), \
           patch('service.subprocess.Popen', return_value=fake_child), \
           patch('service.identity', return_value={'pid': 43210, 'start': '1', 'boot': 'b'}), \
           patch('runtime_image.freeze', return_value=fake_img):
        dual_plan_res = delegate.dispatch('delegate_plan', {
          'session_id': sid,
          'work_item': 'arch-plan',
          'workflow': 'dual-plan',
        })
        self.assertTrue(dual_plan_res['advisory']['blind_context'])
        gate_plan_res = delegate.dispatch('delegate_plan', {
          'session_id': sid,
          'work_item': 'gate-plan',
          'workflow': 'gate-review',
        })
        self.assertTrue(gate_plan_res['advisory']['review_gate'])
        gate_rec = service.start(
          sid, 'codex-reviewer', 'gpt-6.2-sol', 'xhigh', 'Review auth.py', 'Evidence',
          'Sensitive auth gate', repo_dir, one_shot=True,
          workflow='gate-review', required_files=['auth.py'],
        )
        gate_job = st.job(gate_rec['id'])
        self.assertTrue(gate_job['review_gate'])
        self.assertEqual(gate_job['workflow'], 'gate-review')
        self.assertIn('BLOCKING', gate_job['context'])
        self.assertIn('ADVISORY', gate_job['context'])

  def test_freeze_stamps_source_version_and_review_rounds_exclude_builders(self) -> None:
    """Verify freeze() stamps target source VERSION and _review_rounds ignores builder repairs."""
    import re
    import shutil
    import runtime_image

    with tempfile.TemporaryDirectory() as src_root:
      scripts_copy = Path(src_root) / 'scripts-0295'
      shutil.copytree(Path(delegate.__file__).parent, scripts_copy)
      ri_file = scripts_copy / 'runtime_image.py'
      ri_file.write_text(
        re.sub(r'^VERSION\s*=\s*"[^"]+"', 'VERSION = "0.29.5"', ri_file.read_text(), count=1, flags=re.M)
      )
      frozen = runtime_image.freeze(scripts_copy)
      manifest = json.loads((Path(frozen['path']) / 'runtime-image.json').read_text())
      self.assertEqual(manifest['version'], '0.29.5')
      self.assertEqual(runtime_image.job_version({'runtime_image': frozen}), '0.29.5')

    sid = hosts.connect('native-rounds')['session_id']
    service.control(sid, 'on')
    task = ac.begin(sid)
    st.write('job-jbld1.json', {
      'id': 'jbld1', 'session_id': sid, 'work_item': 'impl-mod', 'agent': 'codex-builder',
      'role': {'workspace': 'worktree'}, 'status': 'completed', 'review': 'needs-work',
    })
    st.write('job-jbld2.json', {
      'id': 'jbld2', 'session_id': sid, 'work_item': 'impl-mod', 'agent': 'codex-builder',
      'role': {'workspace': 'worktree'}, 'status': 'completed', 'review': 'needs-work',
    })
    task_rec = ac.load(task['id'])
    task_rec['jobs'] = ['jbld1', 'jbld2']
    ac.save(task_rec)
    self.assertEqual(service._review_rounds(sid, 'impl-mod'), 0)
    with patch('optimizer.plan', return_value={'action': 'main'}):
      fallback_builder = service.plan_task(sid, work_item='impl-mod', workflow='gate-review')
      self.assertNotEqual(fallback_builder['advisory']['action'], 'escalate_to_user')

    st.write('job-jrev1.json', {
      'id': 'jrev1', 'session_id': sid, 'work_item': 'impl-mod', 'agent': 'codex-reviewer',
      'role': {'workspace': 'read-only'}, 'workflow': 'gate-review', 'review_gate': True,
      'status': 'completed', 'review': 'needs-work',
    })
    st.write('job-jrev2.json', {
      'id': 'jrev2', 'session_id': sid, 'work_item': 'impl-mod', 'agent': 'codex-reviewer',
      'role': {'workspace': 'read-only'}, 'workflow': 'gate-review', 'review_gate': True,
      'status': 'completed', 'review': 'needs-work',
    })
    task_rec = ac.load(task['id'])
    task_rec['jobs'] = ['jbld1', 'jbld2', 'jrev1', 'jrev2']
    ac.save(task_rec)
    self.assertEqual(service._review_rounds(sid, 'impl-mod'), 2)
    with patch('optimizer.plan', return_value={'action': 'main'}):
      fallback_gate = service.plan_task(sid, work_item='impl-mod', workflow='gate-review')
      self.assertEqual(fallback_gate['advisory']['action'], 'escalate_to_user')

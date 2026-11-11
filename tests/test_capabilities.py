"""Capability requirements must fail before inference without probing real GPUs/SSH."""
from pathlib import Path
from unittest.mock import Mock, patch
import copy
import ctypes
import io
import contextlib
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import capabilities as cap
import codex_rpc
import service
import storage as st
import worker
import attempts
import optimizer
import accounting

TARGET = {"host": "127.0.0.1", "port": 12345}
NETWORK = {"network": True, "network_targets": [TARGET]}
GPU = {"gpu": True, "gpu_devices": ["0"]}


class CapabilityTests(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory(prefix="delegate-capabilities-")
    self.env = patch.dict(os.environ, {"DELEGATE_STATE_DIR": self.tmp.name, "CODEX_HOME": self.tmp.name + "/codex"})
    self.env.start()
    self.cfg = copy.deepcopy(st.DEFAULT_CONFIG)
    self.cfg["agents"]["codex-reviewer"]["execution_profiles"] = ["sandboxed", "network"]
    self.cfg["agents"]["codex-builder"]["execution_profiles"] = ["sandboxed", "network", "host"]
    self.cfg["agents"]["codex-builder"]["gpu_devices"] = ["0"]
    st.write("config.json", self.cfg)
    self.job = {"id": "j", "session_id": "s", "status": "running", "agent": "codex-reviewer", "provider_name": "codex",
      "provider": self.cfg["providers"]["codex"], "role": self.cfg["agents"]["codex-reviewer"], "cwd": self.tmp.name,
      "source_cwd": self.tmp.name, "model": "fixture", "effort": "low", "timeout_seconds": 300,
      "cancel_requested": False, "messages": [], "inference_state": "not_started"}
    self.args = dict(session_id="s", agent="codex-reviewer", model="fixture", effort="low", task="fixture",
      acceptance="fixture", rationale="fixture", cwd=self.tmp.name, one_shot=True)

  def tearDown(self) -> None:
    self.env.stop()
    self.tmp.cleanup()

  def test_legacy_defaults_remain_restricted_and_not_claimed_verified(self) -> None:
    spec = cap.select(st.DEFAULT_CONFIG["agents"]["codex-reviewer"], self.job["provider"], None)
    self.assertEqual(spec["profile"], "sandboxed")
    self.assertFalse(spec["declared"])
    self.assertEqual(spec["preflight"]["status"], "not_requested")
    self.assertFalse(cap.policy(self.job)["networkAccess"])

  def test_invalid_or_missing_allocation_and_target_declarations_fail_early(self) -> None:
    for data in ({"gpu":True}, {"gpu":True,"gpu_devices":["all"]}, {"gpu":True,"gpu_devices":["-1"]},
      {"ssh":True}, {"network":True}, {"network":True,"network_targets":[{"host":"https://user:pass@example.invalid","port":443}]},
      {"network":True,"network_targets":[{"host":"host","port":True}]}, {"gpu":"yes"}, {"env":{"OPENAI_API_KEY":"bad"}}):
      with self.subTest(data=list(data)):
        with self.assertRaises(ValueError):cap.normalize(data)

  def test_profile_grants_are_explicit_and_host_is_never_auto_selected(self) -> None:
    with self.assertRaisesRegex(ValueError,"not permitted"):
      cap.select(st.DEFAULT_CONFIG["agents"]["codex-builder"], self.job["provider"], GPU, "host")
    role = self.cfg["agents"]["codex-builder"]
    self.assertEqual(cap.select(role,self.job["provider"],GPU)["profile"],"sandboxed")
    host = cap.select(role,self.job["provider"],GPU,"host")
    self.assertFalse(host["filesystem_isolation"])
    self.assertTrue(host["network_access"])
    self.assertEqual(cap.policy({**self.job,"role":role,"execution_environment":host}),{"type":"dangerFullAccess"})
    with self.assertRaisesRegex(ValueError,"read-only"):
      cap.select({**self.job["role"],"execution_profiles":["host"]},self.job["provider"],GPU,"host")
    with self.assertRaisesRegex(ValueError,"allowlist"):
      cap.select(role,self.job["provider"],{"gpu":True,"gpu_devices":["1"]},"host")

  def test_incompatible_provider_or_role_is_rejected_before_catalog_quota_and_worker(self) -> None:
    with patch("service.catalog") as catalog,patch("service.quota") as quota,patch("service.subprocess.Popen") as spawn:
      with self.assertRaisesRegex(ValueError,"not permitted"):
        service.run(**self.args,requirements=NETWORK,execution_profile="host")
      with self.assertRaisesRegex(ValueError,"file-only"):
        service.run(**{**self.args,"agent":"claude-explorer"},requirements=GPU)
    catalog.assert_not_called();quota.assert_not_called();spawn.assert_not_called()
    self.assertIsNone(st.read("task-index.json"))

  def test_grants_can_be_configured_without_weakening_readonly_roles(self) -> None:
    cfg = copy.deepcopy(self.cfg)
    cfg["agents"]["codex-reviewer"]["execution_profiles"] = ["host"]
    with self.assertRaisesRegex(ValueError,"read-only"):service.validate_config(cfg)
    cfg = copy.deepcopy(self.cfg)
    cfg["agents"]["claude-explorer"]["execution_profiles"] = ["network"]
    with self.assertRaisesRegex(ValueError,"File-tool"):service.validate_config(cfg)

  def test_preflight_success_uses_the_model_policy_and_no_gpu_or_ssh_login(self) -> None:
    job = {**self.job,"execution_environment":cap.select(self.job["role"],self.job["provider"],NETWORK)}
    rpc = Mock()
    rpc.call.side_effect = [{"requirements":None},{"exitCode":0,"stdout":json.dumps({"checks":{"network":{"ok":True,"coverage":"TCP"}}})}]
    report = cap.preflight(rpc,job)
    self.assertEqual(report["status"],"passed")
    method,params = rpc.call.call_args.args
    self.assertEqual(method,"command/exec")
    self.assertEqual(params["sandboxPolicy"],cap.policy(job))
    self.assertEqual(params["command"][1:4],["-I","-S","-B"])
    self.assertNotIn("CUDA_VISIBLE_DEVICES",params["env"])
    self.assertEqual(report["inference_calls"],0)

  def test_gpu_blocked_preflight_never_starts_a_model_or_thread(self) -> None:
    job = {**self.job,"execution_environment":cap.select(self.job["role"],self.job["provider"],GPU)}
    st.write("job-j.json",job);calls=[]
    class RPC:
      def __init__(self,*args,**kwargs):pass
      def __enter__(self):return self
      def __exit__(self,*args):pass
      def call(self,method,params=None,timeout=30):
        calls.append(method)
        if method=="configRequirements/read":return {"requirements":None}
        if method=="command/exec":return {"exitCode":0,"stdout":json.dumps({"checks":{"gpu":{"ok":False,"driver_code":100}}})}
        raise AssertionError("No account/thread/inference operation expected")
    with patch("worker.CodexRPC",RPC):outcome=worker.codex_worker(job,"fixture")
    self.assertEqual(outcome["status"],"failed")
    self.assertEqual(calls,["configRequirements/read","command/exec"])
    self.assertFalse(attempts.model_attempted(st.job("j")))
    self.assertEqual(st.job("j")["execution_environment"]["preflight"]["blocked"],["gpu"])

  def test_managed_requirements_and_revoked_grants_block_preflight(self) -> None:
    role=copy.deepcopy(self.cfg["agents"]["codex-builder"])
    job={**self.job,"agent":"codex-builder","role":role,"execution_environment":cap.select(role,self.job["provider"],GPU,"host")}
    rpc=Mock();rpc.call.return_value={"requirements":{"allowedSandboxModes":["read-only","workspace-write"]}}
    report=cap.preflight(rpc,job)
    self.assertEqual(report["status"],"blocked");self.assertEqual(rpc.call.call_count,1)
    self.cfg["agents"]["codex-builder"]["execution_profiles"]=["sandboxed"]
    st.write("config.json",self.cfg);rpc.reset_mock()
    self.assertEqual(cap.preflight(rpc,job)["status"],"blocked")
    rpc.call.assert_not_called()

  def test_missing_device_visibility_never_initializes_cuda(self) -> None:
    needs=cap.normalize(GPU);out=io.StringIO()
    with patch.dict(os.environ,{},clear=True),patch.object(sys,"argv",["probe",json.dumps(needs)]), \
         patch.object(ctypes,"CDLL",side_effect=AssertionError("No real GPU probe in tests")),contextlib.redirect_stdout(out):
      exec(compile(cap.PROBE,"<fixed-preflight>","exec"),{})
    self.assertEqual(json.loads(out.getvalue())["checks"]["gpu"]["error"],"device_visibility_not_propagated")

  def test_fixed_gpu_probe_only_initializes_and_enumerates_mock_driver(self) -> None:
    needs=cap.normalize(GPU);out=io.StringIO()
    driver=SimpleNamespace(cuInit=Mock(return_value=0),cuDeviceGetCount=Mock(side_effect=lambda pointer:setattr(pointer._obj,'value',1) or 0))
    with patch.dict(os.environ,{"CUDA_VISIBLE_DEVICES":"0"}),patch.object(sys,"argv",["probe",json.dumps(needs)]), \
         patch.object(ctypes,"CDLL",return_value=driver),contextlib.redirect_stdout(out):
      exec(compile(cap.PROBE,"<fixed-preflight>","exec"),{})
    self.assertTrue(json.loads(out.getvalue())["checks"]["gpu"]["ok"])
    driver.cuInit.assert_called_once_with(0)
    self.assertEqual(driver.cuDeviceGetCount.call_count,1)

  def test_fixed_ssh_probe_checks_declared_tcp_without_starting_ssh(self) -> None:
    needs=cap.normalize({"ssh":True,"ssh_targets":[TARGET]});out=io.StringIO()
    connection=Mock();connection.__enter__=Mock(return_value=connection);connection.__exit__=Mock()
    with patch.object(sys,"argv",["probe",json.dumps(needs)]),patch('socket.create_connection',return_value=connection) as tcp, \
         patch('shutil.which',return_value='/fixture/ssh'),patch('subprocess.Popen',side_effect=AssertionError('No SSH login')), \
         contextlib.redirect_stdout(out):
      exec(compile(cap.PROBE,"<fixed-preflight>","exec"),{})
    checks=json.loads(out.getvalue())["checks"]
    self.assertTrue(checks["ssh"]["ok"])
    self.assertIn('not checked',checks['ssh']['coverage'])
    tcp.assert_called_once_with((TARGET['host'],TARGET['port']),timeout=1.5)

  def test_ssh_result_is_not_mislabeled_by_a_separate_network_failure(self) -> None:
    needs=cap.normalize({'network':True,'network_targets':[{'host':'127.0.0.1','port':5432}],
                         'ssh':True,'ssh_targets':[TARGET]});out=io.StringIO()
    connection=Mock();connection.__enter__=Mock(return_value=connection);connection.__exit__=Mock()
    def connect(address,timeout):
      if address[1]==5432:raise ConnectionRefusedError()
      return connection
    with patch.object(sys,'argv',['probe',json.dumps(needs)]),patch('socket.create_connection',side_effect=connect), \
         patch('shutil.which',return_value='/fixture/ssh'),contextlib.redirect_stdout(out):
      exec(compile(cap.PROBE,'<fixed-preflight>','exec'),{})
    checks=json.loads(out.getvalue())['checks']
    self.assertFalse(checks['network']['ok']);self.assertTrue(checks['ssh']['ok'])

  def test_environment_overrides_cannot_inject_credentials_or_recursion_flags(self) -> None:
    with patch("codex_rpc.subprocess.Popen") as spawn:
      with self.assertRaises(ValueError):codex_rpc.CodexRPC(env_overrides={"OPENAI_API_KEY":"bad"})
      with self.assertRaises(ValueError):codex_rpc.CodexRPC(env_overrides={"DELEGATE_WORKER":"0"})
    spawn.assert_not_called()

  def test_failed_or_invalid_probe_output_does_not_claim_capabilities(self) -> None:
    job={**self.job,"execution_environment":cap.select(self.job["role"],self.job["provider"],NETWORK)}
    for response in ({"exitCode":1,"stderr":"sandbox unavailable"},{"exitCode":0,"stdout":"not json"},
                     {"exitCode":0,"stdout":'{"checks":{}}'}):
      rpc=Mock();rpc.call.side_effect=[{"requirements":None},response]
      self.assertEqual(cap.preflight(rpc,job)["status"],"blocked")

  def test_probe_precedes_inference_and_reuses_identical_network_policy(self) -> None:
    job={**self.job,"execution_environment":cap.select(self.job["role"],self.job["provider"],NETWORK)}
    st.write("job-j.json",job);calls=[]
    class RPC:
      def __init__(self,*args,**kwargs):pass
      def __enter__(self):return self
      def __exit__(self,*args):pass
      def call(self,method,params=None,timeout=30):
        calls.append((method,params))
        if method=="configRequirements/read":return {"requirements":None}
        if method=="command/exec":return {"exitCode":0,"stdout":'{"checks":{"network":{"ok":true}}}'}
        return {"account":{"type":"chatgpt"},"thread":{"id":"thread"},"turn":{"id":"turn"}}
      def receive(self,timeout):return {"method":"turn/completed","params":{"turn":{"id":"turn","status":"completed"}}}
    with patch("worker.CodexRPC",RPC):outcome=worker.codex_worker(job,"fixture")
    self.assertEqual(outcome["status"],"completed")
    methods=[m for m,p in calls]
    self.assertLess(methods.index("command/exec"),methods.index("account/read"))
    by_method=dict(calls)
    self.assertEqual(by_method["command/exec"]["sandboxPolicy"],by_method["turn/start"]["sandboxPolicy"])

  def test_resource_planner_never_assumes_unverified_main_capabilities(self) -> None:
    service.control('s','on')
    accounting.begin('s',complexity='simple')
    sample={'status':'known','data':{'five_hour':{'used_percentage':1}}}
    catalog={'models':[{'model':'fixture-fast','supportedReasoningEfforts':[{'reasoningEffort':'low'}]}]}
    with patch('service.catalog',return_value=catalog),patch('service.quota',return_value={'status':'unknown'}), \
         patch('budget.read_claude',return_value=sample),patch('budget.recommendation',return_value={'mode':'normal'}):
      decision=optimizer.plan('s',requirements=NETWORK)
    self.assertEqual(decision['action'],'delegate')
    self.assertEqual(decision['execution_profile'],'network')
    with patch('service.catalog') as models,patch('service.quota') as quota:
      trivial=optimizer.plan('s',complexity='trivial',requirements=GPU)
      self.assertEqual(trivial['action'],'replan')
      with self.assertRaisesRegex(ValueError,'execution_profile'):
        optimizer.plan('s',requirements=NETWORK,execution_profile='not-a-profile')
    models.assert_not_called();quota.assert_not_called()

  def test_unavailable_capability_pool_fails_before_provider_discovery(self) -> None:
    service.control('s','on',agents=['claude-explorer'])
    accounting.begin('s',complexity='simple')
    with patch('service.catalog') as models,patch('service.quota') as quota:
      decision=optimizer.plan('s',requirements=NETWORK)
    self.assertEqual(decision['action'],'replan')
    self.assertIn('unverified',decision['reason'])
    models.assert_not_called();quota.assert_not_called()

  def test_unverified_runtime_does_not_cleanup_claimed_failed_workspace(self) -> None:
    job={**self.job,'execution_environment':{'preflight':{'status':'blocked'}},'thread_id':'previous-thread',
      'worktree':'/not-a-real-worktree','workspace_setup':{'created_by_job':True,'source_root':'/not-a-real-source'}}
    st.write('job-j.json',job)
    with patch('worker.workspaces.rollback') as cleanup,patch('worker.thread_cleanup.archive') as archive:
      worker.main('j')
    cleanup.assert_not_called();archive.assert_not_called()
    self.assertEqual(st.job('j')['status'],'failed')

  def test_expanded_profile_without_resources_still_checks_grants_and_managed_mode(self) -> None:
    role=copy.deepcopy(self.cfg['agents']['codex-builder'])
    job={**self.job,'agent':'codex-builder','role':role,'execution_environment':cap.select(role,self.job['provider'],None,'host')}
    rpc=Mock();rpc.call.return_value={'requirements':{'allowedSandboxModes':['workspace-write']}}
    self.assertEqual(cap.preflight(rpc,job)['status'],'blocked')
    self.assertEqual(rpc.call.call_count,1)
    self.cfg['agents']['codex-builder']['execution_profiles']=['sandboxed'];st.write('config.json',self.cfg)
    st.write('job-j.json',job)
    with patch('worker.CodexRPC') as backend:
      outcome=worker.codex_worker(job,'fixture')
    backend.assert_not_called()
    self.assertEqual(outcome['status'],'failed')

  def test_failed_prepare_does_not_cleanup_a_stale_blocked_receipt(self) -> None:
    job={**self.job,'role':{'workspace':'worktree'},'execution_environment':{'preflight':{'status':'blocked'}},
      'worktree':'/not-a-real-worktree','workspace_setup':{'created_by_job':True,'source_root':'/not-a-real-source'}}
    st.write('job-j.json',job)
    with patch('worker.runtime_image.require_worker'),patch('worker.prepare',side_effect=ValueError('existing target')), \
         patch('worker.workspaces.rollback') as cleanup:
      worker.main('j')
    cleanup.assert_not_called()


if __name__ == "__main__":unittest.main()

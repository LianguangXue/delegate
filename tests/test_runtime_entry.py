"""The stable CLI follows activated releases while preserving stdin, host args and in-flight calls."""
from pathlib import Path
from unittest.mock import patch
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS=Path(__file__).resolve().parents[1]/'plugins/delegate/scripts'
sys.path.insert(0,str(SCRIPTS))
import runtime_entry


class RuntimeEntryTests(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp=tempfile.TemporaryDirectory(prefix='delegate-entry-')
    self.root=Path(self.tmp.name);self.config=self.root/'profile'
    self.registry=self.config/'plugins/installed_plugins.json';self.registry.parent.mkdir(parents=True)
    self.env=patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(self.config)})
    self.env.start()
    self.fallback=self.package('fallback','0.26.0')/'scripts/delegate.py'

  def tearDown(self) -> None:
    self.env.stop();self.tmp.cleanup()

  def package(self, label: str, version: str) -> Path:
    package=self.root/label;(package/'scripts').mkdir(parents=True);(package/'.claude-plugin').mkdir()
    (package/'.claude-plugin/plugin.json').write_text(json.dumps({'name':'delegate','version':version}))
    (package/'scripts/delegate.py').write_text('import os,sys,json\nfrom pathlib import Path\n'
      'if os.environ.get("FIXTURE_READY"):Path(os.environ["FIXTURE_READY"]).write_text("ready")\n'
      'print(json.dumps({"engine":'+repr(label)+',"args":sys.argv[1:],"stdin":sys.stdin.read()}))\n')
    return package

  def activate(self, package: Path, version: str) -> None:
    self.registry.write_text(json.dumps({'plugins':{'delegate@fixture':[{'scope':'user','installPath':str(package),'version':version}]}}))

  def run_cli(self, *args: str, payload: str='') -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable,str(SCRIPTS/'runtime_entry.py'),'--fallback',str(self.fallback),*args],
      input=payload,text=True,capture_output=True,timeout=15)

  def test_same_conversation_uses_new_activation_without_reinstalling_launcher(self) -> None:
    first=self.package('first','0.26.1');second=self.package('second','0.27.0')
    payload=json.dumps({'name':'delegate_job','arguments':{'session_id':'same-parent','job_id':'old-job','action':'result'}})
    self.activate(first,'0.26.1');a=self.run_cli('call','codex',payload=payload)
    self.activate(second,'0.27.0');b=self.run_cli('call','codex',payload=payload)
    self.assertEqual((a.returncode,b.returncode),(0,0))
    self.assertEqual(json.loads(a.stdout)['engine'],'first')
    self.assertEqual(json.loads(b.stdout)['engine'],'second')
    self.assertEqual(json.loads(b.stdout)['args'],['cli','call','codex'])
    self.assertEqual(json.loads(b.stdout)['stdin'],payload)

  def test_native_install_without_user_plugin_keeps_explicit_fallback(self) -> None:
    self.assertEqual(runtime_entry.resolve(self.fallback),self.fallback)
    self.registry.write_text('{"plugins":{}}')
    self.assertEqual(runtime_entry.resolve(self.fallback),self.fallback)

  def test_invalid_active_install_never_silently_falls_back(self) -> None:
    package=self.package('active','0.26.1');self.activate(package,'0.26.1')
    (package/'.claude-plugin/plugin.json').write_text('{"name":"delegate","version":"0.25.1"}')
    value=self.run_cli('--version')
    self.assertEqual(value.returncode,2);self.assertEqual(value.stdout,'')
    self.assertNotIn('Traceback',value.stderr)
    self.registry.write_text('{broken')
    self.assertEqual(self.run_cli('--help').returncode,2)

  def test_old_ambiguous_and_malformed_registries_fail_closed(self) -> None:
    package=self.package('active','0.25.1');self.activate(package,'0.25.1')
    with self.assertRaises(ValueError):runtime_entry.resolve(self.fallback)
    data=json.loads(self.registry.read_text());data['plugins']['delegate@fixture']*=2
    self.registry.write_text(json.dumps(data))
    with self.assertRaises(ValueError):runtime_entry.resolve(self.fallback)
    for data in ([],{'plugins':None},{'plugins':{'delegate@fixture':[None]}}):
      self.registry.write_text(json.dumps(data))
      with self.assertRaises(ValueError):runtime_entry.resolve(self.fallback)

  def test_fifo_and_symlink_metadata_do_not_block_or_redirect(self) -> None:
    os.mkfifo(self.registry)
    with self.assertRaises(ValueError):runtime_entry.resolve(self.fallback)
    self.registry.unlink();target=self.root/'other.json';target.write_text('{"plugins":{}}')
    self.registry.symlink_to(target)
    with self.assertRaises(OSError):runtime_entry.resolve(self.fallback)

  def test_inflight_call_keeps_selected_engine_after_activation(self) -> None:
    first=self.package('first','0.26.1');second=self.package('second','0.27.0');self.activate(first,'0.26.1')
    ready=self.root/'ready'
    proc=subprocess.Popen([sys.executable,str(SCRIPTS/'runtime_entry.py'),'--fallback',str(self.fallback),'call'],
      stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
      env={**os.environ,'FIXTURE_READY':str(ready)})
    try:
      until=time.monotonic()+10
      while not ready.exists() and time.monotonic()<until:time.sleep(.01)
      self.assertTrue(ready.exists())
      self.activate(second,'0.27.0')
      out,err=proc.communicate('same-request',timeout=10)
      self.assertEqual(proc.returncode,0,err)
      self.assertEqual(json.loads(out)['engine'],'first')
      self.assertEqual(json.loads(self.run_cli('call',payload='next').stdout)['engine'],'second')
    finally:
      if proc.poll() is None:proc.kill();proc.communicate()


if __name__=='__main__':unittest.main()

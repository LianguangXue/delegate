"""Host installation preflight never partially mutates a conflicting setup."""
import argparse
from pathlib import Path
import os,runpy,tempfile,unittest,sys,contextlib,io
from unittest.mock import patch

API=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/install-hosts.py'))

class InstallTests(unittest.TestCase):
  def setUp(self):
    self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name);self.repo=Path(__file__).resolve().parents[1]
    self.env=patch.dict(os.environ,{'CODEX_HOME':str(self.home/'codex'),'DSH_HOME':str(self.home/'dsh')});self.env.start()
  def tearDown(self):self.env.stop();self.tmp.cleanup()
  def test_missing_dsh_profile_fails_before_any_shared_write(self):
    args=argparse.Namespace(host='all',dsh_profile='web');binary=self.home/'.local/bin/delegate-agent'
    with patch('shutil.which',return_value='/fixture/bin'):
      with self.assertRaises(ValueError):API['preflight'](self.repo,args,self.home,binary)
    self.assertFalse(binary.parent.exists())
    self.assertFalse((self.home/'codex/config.toml').exists())
  def test_existing_server_and_skill_are_not_replaced(self):
    config=self.home/'codex/config.toml';config.parent.mkdir();config.write_text('[mcp_servers.delegate]\ncommand="custom"\n')
    args=argparse.Namespace(host='codex',dsh_profile='web')
    with patch('shutil.which',return_value='/fixture/bin'):
      with self.assertRaises(ValueError):API['preflight'](self.repo,args,self.home,self.home/'launcher')
    self.assertIn('custom',config.read_text())
  def test_empty_dsh_profile_is_normalized_before_real_install_write(self):
    profile=self.home/'dsh/profiles/web/cordis.patch.yml';profile.parent.mkdir(parents=True)
    profile.write_text('# fresh profile\n[]\n')
    with patch('shutil.which',return_value='/fixture/bin'),patch('pathlib.Path.home',return_value=self.home), \
         patch.object(sys,'argv',['install-hosts.py','dsh']),contextlib.redirect_stdout(io.StringIO()):
      API['main']()
    text=profile.read_text()
    self.assertNotIn('[]',text)
    self.assertIn('- id: delegate-host-mcp',text)
    self.assertTrue((self.home/'.local/bin/delegate-agent').exists())
    self.assertTrue((self.home/'dsh/skills/delegate').is_symlink())

  def test_fresh_codex_preflight_is_read_only(self):
    args=argparse.Namespace(host='codex',dsh_profile='web')
    with patch('shutil.which',return_value='/fixture/bin'):
      API['preflight'](self.repo,args,self.home,self.home/'launcher')
    self.assertFalse((self.home/'launcher').exists())

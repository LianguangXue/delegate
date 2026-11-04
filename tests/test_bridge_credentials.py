"""Credential binding, account separation, and API routing tests with fake private snapshots."""
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/delegate/scripts'))
import credentials
import service
import storage as st
import worker


class BridgeTests(unittest.TestCase):
  def setUp(self) -> None:
    self.temp = tempfile.TemporaryDirectory(prefix='bridge-credentials-')
    self.root = Path(self.temp.name)
    self.env = patch.dict(os.environ, {'DELEGATE_STATE_DIR':str(self.root/'delegate')})
    self.env.start()
    profiles = {}
    for alias, preset, base, key_name in [
      ('official','deepseek-official','https://api.deepseek.com/anthropic','ANTHROPIC_API_KEY'),
      ('company','deepseek','http://gateway.invalid','ANTHROPIC_AUTH_TOKEN')]:
      directory = self.root/'api-accounts'/alias/'snapshot'
      directory.mkdir(parents=True)
      env = {'ANTHROPIC_BASE_URL':base,key_name:'dummy-'+alias}
      if alias == 'company':env['ANTHROPIC_CUSTOM_HEADERS']='X-Provider: deepseek'
      value = {'env':env,'model':'deepseek-flash'}
      self.write(directory/'settings.json',value)
      profiles[alias]={'auth_mode':'api','api_preset':preset,'base_url':base,'model':'deepseek-flash',
                       'config_dir':str(directory),'principal':'api:'+hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()}
    self.write(self.root/'state.json',{'profiles':profiles})

  def tearDown(self) -> None:
    self.env.stop()
    self.temp.cleanup()

  def write(self, path: Path, value: dict) -> None:
    path.write_text(json.dumps(value));path.chmod(0o600)

  def provider(self, alias: str) -> dict:
    return {'kind':'anthropic','enabled':True,'command':'claude','bridge_state_dir':str(self.root),
            'bridge_account':alias,'base_url':'https://api.deepseek.com/anthropic' if alias=='official' else 'http://gateway.invalid',
            'models':['deepseek-flash'],'efforts':['low','high','max']}

  def test_accounts_resolve_distinct_keys_and_headers_without_global_env_changes(self) -> None:
    original = dict(os.environ)
    official = credentials.environment(self.provider('official'))
    company = credentials.environment(self.provider('company'))
    self.assertEqual(official['ANTHROPIC_API_KEY'],'dummy-official')
    self.assertNotIn('ANTHROPIC_AUTH_TOKEN',official)
    self.assertEqual(company['ANTHROPIC_AUTH_TOKEN'],'dummy-company')
    self.assertEqual(company['ANTHROPIC_CUSTOM_HEADERS'],'X-Provider: deepseek')
    self.assertEqual(dict(os.environ),original)

  def test_tampered_snapshot_and_redirected_endpoint_fail_closed(self) -> None:
    p = self.provider('official')
    with self.assertRaisesRegex(ValueError,'binding'):
      credentials.environment(dict(p,base_url='https://wrong.invalid'))
    f=self.root/'api-accounts/official/snapshot/settings.json'
    d=json.loads(f.read_text());d['env']['ANTHROPIC_API_KEY']='changed';self.write(f,d)
    with self.assertRaisesRegex(ValueError,'fingerprint'):
      credentials.environment(p)

  def test_rotation_updates_lookup_without_copying_key(self) -> None:
    p=self.provider('official');state=json.loads((self.root/'state.json').read_text())
    folder=self.root/'api-accounts/official/rotated';folder.mkdir()
    d={'env':{'ANTHROPIC_BASE_URL':p['base_url'],'ANTHROPIC_API_KEY':'dummy-rotated'}}
    self.write(folder/'settings.json',d)
    state['profiles']['official'].update(config_dir=str(folder),principal='api:'+hashlib.sha256(json.dumps(d,sort_keys=True).encode()).hexdigest())
    self.write(self.root/'state.json',state)
    self.assertEqual(credentials.environment(p)['ANTHROPIC_API_KEY'],'dummy-rotated')

  def test_private_permissions_and_final_symlink_rejected(self) -> None:
    f=self.root/'api-accounts/official/snapshot/settings.json';f.chmod(0o644)
    with self.assertRaises(ValueError):credentials.environment(self.provider('official'))
    f.chmod(0o600);f.rename(f.with_suffix('.saved'));f.symlink_to(f.with_suffix('.saved'))
    with self.assertRaises(ValueError):credentials.environment(self.provider('official'))

  def test_company_key_cannot_be_sent_to_official_balance(self) -> None:
    with patch('credentials.build_opener') as open_request:
      with self.assertRaisesRegex(ValueError,'origin'):
        credentials.get_json(self.provider('company'),'https://api.deepseek.com/user/balance')
      open_request.assert_not_called()

  def test_company_http_requires_verified_bridge_binding(self) -> None:
    cfg=st.config();cfg['providers']['test']=self.provider('company');service.validate_config(cfg)
    cfg['providers']['test'].pop('bridge_account');cfg['providers']['test'].pop('bridge_state_dir')
    cfg['providers']['test']['api_key_env']='TEST_KEY'
    with self.assertRaisesRegex(ValueError,'HTTPS'):service.validate_config(cfg)

  def test_worker_keeps_selected_effort_and_does_not_use_parent_auth(self) -> None:
    value={'provider':self.provider('company'),'model':'deepseek-flash','effort':'low','role':{'workspace':'read-only'}}
    with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'parent-secret','CLAUDE_CODE_EFFORT_LEVEL':'max'}):
      command,env=worker.claude_command(value)
    self.assertNotIn('ANTHROPIC_API_KEY',env)
    self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'],'dummy-company')
    self.assertEqual(command[command.index('--effort')+1],'low')
    self.assertNotIn('CLAUDE_CODE_EFFORT_LEVEL',env)

  def test_redaction_and_config_never_contain_credentials(self) -> None:
    env=credentials.environment(self.provider('official'))
    self.assertEqual(credentials.redact('echo dummy-official',env),'echo [REDACTED]')
    self.assertEqual(credentials.redact_data({'nested':['dummy-official']},env),{'nested':['[REDACTED]']})
    service.configure('providers','official',self.provider('official'))
    self.assertNotIn('dummy-official',(st.root()/'config.json').read_text())

  def test_new_api_worker_history_is_isolated_by_provider(self) -> None:
    value={'provider':self.provider('official'),'provider_name':'deepseek-official','model':'deepseek-flash',
           'effort':'low','role':{'workspace':'read-only'}}
    with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':'/unrelated-parent-profile'}):
      _,env=worker.claude_command(value)
    self.assertEqual(env['CLAUDE_CONFIG_DIR'],str(st.root()/'backend-state/deepseek-official'))

  def test_dynamic_model_effort_is_checked(self) -> None:
    data={'models':['deepseek-flash'],'efforts':['low','high','max'],
          'model_details':[{'id':'deepseek-flash','effort':{'supported_levels':['high','max']}}]}
    with patch('service.catalog',return_value=data):
      with self.assertRaisesRegex(ValueError,'does not support'):
        service.choose({},self.provider('official'),'official','deepseek-flash','low')
      service.choose({},self.provider('official'),'official','deepseek-flash','high')

  def test_metadata_401_does_not_disable_configured_inference(self) -> None:
    p=dict(self.provider('official'),models_url='https://api.deepseek.com/models')
    service.configure('providers','official',p)
    with patch('credentials.get_json',side_effect=ValueError('Provider metadata request failed (HTTP 401)')):
      data=service.catalog('official',True)
      self.assertEqual(data['models'],['deepseek-flash'])
      self.assertIn('401',data['discovery_error'])
      service.choose({},p,'official','deepseek-flash','low')

  def test_metadata_echo_is_sanitized_before_return(self) -> None:
    with patch('credentials.build_opener') as opener:
      opener.return_value.open.return_value.__enter__.return_value.read.return_value=b'{"data":[{"note":"dummy-official"}]}'
      data=credentials.get_json(self.provider('official'),'https://api.deepseek.com/models')
      self.assertEqual(data,{'data':[{'note':'[REDACTED]'}]})

  def test_encoded_key_in_worker_output_is_not_persisted(self) -> None:
    fake=self.root/'fake-claude'
    fake.write_text('#!/usr/bin/env python3\nimport json\n'
                    'print(json.dumps({"result":"dummy-official","usage":{}}).replace("dummy",r"\\u0064ummy"))\n')
    fake.chmod(0o700)
    value={'id':'echo-test','provider':dict(self.provider('official'),command=str(fake)),
           'provider_name':'official','role':{'workspace':'read-only'},'model':'deepseek-flash','effort':'low',
           'cwd':str(self.root),'timeout_seconds':10,'cancel_requested':False}
    st.write('job-echo-test.json',value)
    result=worker.claude_worker(value,'fixture')
    self.assertEqual(result['result'],'[REDACTED]')
    output=(st.root()/'output-echo-test.json').read_text()
    self.assertNotIn('dummy-official',output)
    self.assertNotIn('dummy-official',json.loads(output)['result'])


if __name__=='__main__':unittest.main()

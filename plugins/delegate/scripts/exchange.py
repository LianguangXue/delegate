"""A no-inference adapter to am's quota-first work protocol."""
from typing import Any
import json
import subprocess


def exchange(session_id: str, action: str, payload: dict[str, Any], am_root: str = "") -> dict[str, Any]:
  """Read/share artifacts or explicitly queue bounded work; never launch a recipient."""
  import storage as st
  st.key(session_id)
  allowed = {'publish','search','get','configure','ask','inbox','reply','watch','poll','ack','stage','stage-check'}
  if action not in allowed:
    raise ValueError('Unsupported exchange action')
  data = {**payload, 'session_id':session_id}
  raw=json.dumps(data)
  if len(raw)>64000:raise ValueError('Exchange packet too large')
  if not am_root:
    am_root=subprocess.run(['am','where'],capture_output=True,text=True,check=True,timeout=5).stdout.strip()
  proc=subprocess.run(['am','--root',am_root,'work',action],input=raw,text=True,
                      capture_output=True,timeout=15)
  if proc.returncode:
    return {'ok':False,'error':proc.stderr[-2000:],'inference_started':False}
  return {'ok':True,'result':json.loads(proc.stdout),'inference_started':False,
          'note':'Queued questions are data; receiving/answering with a model consumes its own quota.'}

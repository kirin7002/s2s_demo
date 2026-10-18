"""Version-aware release checks; never open a model or device control session."""
import json


OFFLINE_CHECK = '''from gateway.server import create_app
from gateway.config import Config
from gateway.stackchan import QWEN_TOOLS
names={t['function']['name'] for t in QWEN_TOOLS}
assert names=={'stackchan_get_state','stackchan_perform','stackchan_perform_targets','stackchan_get_command','stackchan_stop'}
tools=Config().session_update(QWEN_TOOLS)['session']['tools']
assert {t['function']['name'] for t in tools}==names-{'stackchan_perform'}
target=next(t['function']['parameters'] for t in tools if t['function']['name']=='stackchan_perform_targets')
fields=target['properties']['steps']['items']['properties']
assert {'yaw_deg','pitch_deg','expression','hold_ms'}<=fields.keys()
assert not {'head','screen','light'} & fields.keys()
'''


def health_check_code(manifest):
    assets = {name.removeprefix('web/'): digest for name, digest in manifest['files'].items()
              if name.startswith('web/')}
    return '''import hashlib,http.cookies,json,time,urllib.request,urllib.error
from gateway.config import Config
c=Config.from_env()
assert c.stackchan_configured and c.stackchan_test_max_deg is None and not c.stackchan_read_only
assert c.max_seconds==600 and c.idle_seconds==60
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
base='http://127.0.0.1:8080'
for attempt in range(30):
 try:
  with opener.open(base+'/login',timeout=2) as page:assert page.status==200
  meta=json.load(opener.open(base+'/api/config',timeout=2))
  assert meta['ready'] and meta['password_required'] and meta['model']=='qwen-audio-3.0-realtime-flash'
  break
 except Exception:
  if attempt==29:raise SystemExit('Gateway health failed; details withheld') from None
  time.sleep(.2)
for name,digest in ASSETS.items():
 path={'index.html':'/login','login.html':'/login'}.get(name,'/'+name)
 if name=='index.html':continue
 assert hashlib.sha256(opener.open(base+path,timeout=2).read()).hexdigest()==digest
for path,headers,expected in [('/api/stackchan',{},401),('/device/v1/ws',{},401),('/ws',{'Origin':c.origin},401),('/ws',{'Origin':'https://invalid.example'},403)]:
 try:opener.open(urllib.request.Request(base+path,headers=headers),timeout=2);raise AssertionError()
 except urllib.error.HTTPError as e:
  try:assert e.code==expected
  finally:e.close()
def post(path,value,cookie=None):
 headers={'Origin':c.origin,'Content-Type':'application/json'}
 if cookie:headers['Cookie']=cookie
 return opener.open(urllib.request.Request(base+path,data=json.dumps(value).encode(),headers=headers),timeout=3)
cookie=None
try:
 response=post('/api/login',{'password':c.demo_password})
 jar=http.cookies.SimpleCookie();jar.load(response.headers['Set-Cookie'])
 response.read();response.close()
 cookie='s2s_session='+jar['s2s_session'].value
 assert jar['s2s_session']['secure'] and jar['s2s_session']['httponly']
 headers={'Cookie':cookie}
 chat=opener.open(urllib.request.Request(base+'/chat',headers=headers),timeout=2).read()
 assert hashlib.sha256(chat).hexdigest()==ASSETS['index.html']
 bound=json.load(post('/api/stackchan/bind',{'device_id':c.stackchan_device_id},cookie))
 assert bound['configured'] and bound['bound'] and bound['tools_registered_on_next_call']
finally:
 if cookie:post('/api/logout',{},cookie).close()
'''.replace('ASSETS', repr(assets))

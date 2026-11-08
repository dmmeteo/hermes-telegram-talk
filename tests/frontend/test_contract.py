import http.client
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest

BACKEND = Path(os.environ['TALK_BACKEND_SOURCE'])
sys.path.insert(0, str(BACKEND))
import server
from hermes_talk import fixtures as f
from hermes_talk.config import parse_config
from hermes_talk.history import MemoryHistory
from hermes_talk.talk import TalkRuntime


class Contract(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix='talk-contract-')
        home = f.write_home(self.home.name)
        self.upstream = f.FakeUpstream()
        self.history = MemoryHistory()
        self.runtime = TalkRuntime(parse_config(f.config_data(), home), f.BOT_TOKEN, self.history,
                                   soul=f.file_soul(home), skill=f.file_skill(home))
        self.app = server.VoiceApp(f.BOT_TOKEN, {f.USER_ID, f.OTHER_USER_ID}, 'https://talk.fixture',
                                  resolve=self.upstream.resolve, post=self.upstream.post, talk=self.runtime)
        self.httpd = server.BoundedServer(('127.0.0.1', 0), server.make_handler(self.app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.auth = f.sign_init_data(f.USER_ID, int(time.time()))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.home.cleanup()

    def request(self, route, fields=None, auth=None):
        conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        conn.request('POST', '/tg-voice/api/' + route, json.dumps({'initData': auth or self.auth, **(fields or {})}),
                     {'Origin': 'https://talk.fixture', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        status, data = response.status, json.loads(response.read())
        conn.close()
        return status, data

    def session(self):
        status, boot = self.request('bootstrap')
        self.assertEqual(status, 200)
        status, voice = self.request('session', {'conversation_id':boot['conversation_id'], 'sdp':f.OFFER})
        self.assertEqual(status, 200)
        return boot, voice

    def test_bootstrap_exact_public_fields_and_private_avatar(self):
        status, boot = self.request('bootstrap')
        self.assertEqual(status, 200)
        self.assertEqual(set(boot), {'conversation_id','profile','appearance','greeting_enabled','limits'})
        self.assertEqual(boot['profile']['avatar_url'], '/tg-voice/avatar')
        self.assertEqual(boot['appearance'], {'listen_color':'#4FC3F7','speak_color':'#FFB74D'})
        for marker in [f.SOUL_MARKER, f.SKILL_MARKER, f.SECRET_IN_SOUL, f.SYNTHETIC_TOKEN]:
            self.assertNotIn(marker, json.dumps(boot))
        self.assertEqual(self.upstream.resolves, 0)

    def test_session_contract_fixed_subscription_and_no_browser_instructions(self):
        boot, voice = self.session()
        self.assertEqual(set(voice), {'sdp','type','voice_session_id','conversation_id'})
        self.assertEqual(voice['conversation_id'], boot['conversation_id'])
        self.assertEqual(voice['type'], 'answer')
        self.assertEqual(self.upstream.posts[0][0], server.GPT_LIVE_URL)
        self.assertEqual(self.request('session', {'sdp':f.OFFER, 'conversation_id':boot['conversation_id'], 'instructions':'override'})[0], 400)

    def test_idempotent_ordered_events_after_end(self):
        boot, voice = self.session()
        ids = {'conversation_id':boot['conversation_id'], 'voice_session_id':voice['voice_session_id']}
        events = [{'id':'b','sequence':1,'role':'assistant','text':'Synthetic reply'},
                  {'id':'a','sequence':0,'role':'user','text':'Synthetic question'}]
        self.assertEqual(self.request('end', ids), (200, {'ok':True}))
        for _ in range(2):
            status, result = self.request('events', {**ids, 'events':events})
            self.assertEqual(status,200)
            self.assertEqual(set(result['accepted_ids']), {'a','b'})
        messages = [m for rows in self.history.messages.values() for m in rows]
        self.assertEqual([(m['role'],m['content']) for m in messages], [('user','Synthetic question'),('assistant','Synthetic reply')])

    def test_auth_before_any_upstream_and_owner_rejection(self):
        self.assertEqual(self.request('bootstrap', auth='forged')[0], 401)
        self.assertEqual(self.upstream.resolves,0)
        boot, voice = self.session()
        foreign = f.sign_init_data(f.OTHER_USER_ID,int(time.time()))
        self.assertEqual(self.request('events', {'conversation_id':boot['conversation_id'],
          'voice_session_id':voice['voice_session_id'], 'events':[{'id':'x','sequence':0,'role':'user','text':'Foreign'}]}, auth=foreign)[0],404)

    def test_real_server_serves_new_static_entry_and_csp(self):
        conn = http.client.HTTPConnection(*self.httpd.server_address,timeout=5)
        conn.request('GET','/tg-voice/')
        response=conn.getresponse()
        html=response.read().decode()
        self.assertEqual(response.status,200)
        self.assertIn('<title>Hermes Telegram Talk</title>',html)
        self.assertIn("connect-src 'self'",response.getheader('Content-Security-Policy'))
        self.assertIn('id="end"',html)
        self.assertIn('id="call-menu"',html)
        self.assertNotIn('<header>',html)
        self.assertIn('id="ring"',html)
        conn.close()
        assets = re.findall(r'"(app\.(?:js|css)\?v=[a-f0-9]+)"', html)
        self.assertEqual(len(assets), 2)
        for asset in assets:
            conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
            conn.request('GET', '/tg-voice/' + asset)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), (BACKEND / 'static/talk' / asset.split('?')[0]).read_bytes())
            conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)

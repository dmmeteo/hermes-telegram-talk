"""Offline compact UI backend boundaries, synthetic bots and image bytes only."""
import http.client
import json
from pathlib import Path
import sys
import threading
import time
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import server
from hermes_talk.avatar import BotAvatar, telegram_photo
from hermes_talk import fixtures as f
from hermes_talk.config import parse_config
from hermes_talk.history import MemoryHistory
from hermes_talk.talk import TalkRuntime

PNG = b'\x89PNG\r\n\x1a\nsynthetic'


class Compact(unittest.TestCase):
    def settle(self, cache):
        deadline = time.monotonic() + 2
        while cache._busy and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(cache._busy)

    def test_cache_refresh_change_failure_absence_and_profile_isolation(self):
        now = [0]
        calls = []
        value = [(PNG, 'image/png')]
        fallback = (b'neutral', 'image/svg+xml')
        def fetch(token, bot):
            calls.append((token, bot))
            if isinstance(value[0], Exception):
                raise value[0]
            return value[0]
        cache = BotAvatar('synthetic-a', '1', fallback, fetch, lambda: now[0])
        other = BotAvatar('synthetic-b', '2', fallback, fetch, lambda: now[0])
        self.assertEqual(cache.image(), fallback)
        self.settle(cache)
        self.assertEqual(cache.image(), (PNG, 'image/png'))
        self.assertEqual(calls, [('synthetic-a', '1')])
        value[0] = (PNG + b'new', 'image/png'); now[0] = 301
        cache.image(); self.settle(cache)
        self.assertEqual(cache.image(), value[0])
        self.assertIsNone(other._photo)
        value[0] = OSError('synthetic secret must never escape'); now[0] = 602
        old = cache.image(); self.settle(cache)
        self.assertEqual(cache.image(), old)
        value[0] = None; now[0] = 633
        cache.image(); self.settle(cache)
        self.assertEqual(cache.image(), fallback)
        other.image(); self.settle(other)
        self.assertIn(('synthetic-b', '2'), calls)

    def test_refresh_never_waits_for_telegram(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*_):
            entered.set(); release.wait(2); return PNG, 'image/png'
        cache = BotAvatar('synthetic', '1', (b'fallback', 'image/svg+xml'), slow)
        self.assertEqual(cache.image()[0], b'fallback')
        self.assertTrue(entered.wait(1))
        self.assertEqual(cache.image()[0], b'fallback')
        release.set(); self.settle(cache)
        self.assertEqual(cache.image()[0], PNG)

    def test_telegram_fixed_urls_no_redirects_and_bounded_image(self):
        requests = []
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, limit): return self.body[:limit]
        replies = [json.dumps({'ok': True, 'result': {'photos': [[{'file_id': 'safe', 'width': 100, 'height': 100}]]}}).encode(),
                   json.dumps({'ok': True, 'result': {'file_path': 'photos/safe.jpg'}}).encode(), b'\xff\xd8\xffsynthetic']
        class Opener:
            def open(self, url, timeout):
                requests.append(url); return Response(replies.pop(0))
        with patch('urllib.request.build_opener', return_value=Opener()):
            self.assertEqual(telegram_photo('synthetic-token', 1), (b'\xff\xd8\xffsynthetic', 'image/jpeg'))
        self.assertEqual(requests, ['https://api.telegram.org/botsynthetic-token/getUserProfilePhotos?user_id=1&limit=1',
                                   'https://api.telegram.org/botsynthetic-token/getFile?file_id=safe',
                                   'https://api.telegram.org/file/botsynthetic-token/photos/safe.jpg'])
        replies[:] = [b'{"ok":true,"result":{"photos":[[{"file_id":"safe"}]]}}',
                      b'{"ok":true,"result":{"file_path":"https://evil.invalid/a.jpg"}}']
        with patch('urllib.request.build_opener', return_value=Opener()):
            with self.assertRaises(ValueError): telegram_photo('synthetic-token', 1)
        self.assertEqual(len(replies), 0)

    def test_http_auth_before_avatar_access_and_no_secret_response(self):
        with tempfile.TemporaryDirectory() as temp:
            home = f.write_home(temp)
            runtime = TalkRuntime(parse_config(f.config_data(), home), f.BOT_TOKEN, MemoryHistory(),
                                  soul=f.file_soul(home), skill=f.file_skill(home))
            calls = []
            app = server.VoiceApp(f.BOT_TOKEN, {f.USER_ID}, 'http://fixture.invalid', talk=runtime,
                                 avatar_fetch=lambda *args: calls.append(args) or (PNG, 'image/png'))
            httpd = server.BoundedServer(('127.0.0.1', 0), server.make_handler(app))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                def post(auth, **extra):
                    conn = http.client.HTTPConnection(*httpd.server_address)
                    conn.request('POST', '/tg-voice/api/avatar', json.dumps({'initData': auth, **extra}),
                                 {'Origin': 'http://fixture.invalid', 'Content-Type': 'application/json'})
                    response = conn.getresponse(); body = response.read(); conn.close()
                    self.assertNotIn(f.BOT_TOKEN.encode(), body)
                    return response.status, body
                self.assertEqual(post('forged')[0], 401)
                self.assertEqual(calls, [])
                auth = f.sign_init_data(f.USER_ID, int(time.time()))
                self.assertEqual(post(auth, url='https://evil.invalid')[0], 400)
                self.assertEqual(calls, [])
                self.assertEqual(post(auth)[0], 200)
                self.settle(app.avatar)
                self.assertEqual(post(auth), (200, PNG))
                self.assertEqual(calls, [(f.BOT_TOKEN, runtime.bot_id)])
            finally:
                httpd.shutdown(); httpd.server_close()

    def test_catalogue_defaults_scoped_selection_and_fixed_subscription_route(self):
        with tempfile.TemporaryDirectory() as temp:
            home = f.write_home(temp); history = MemoryHistory()
            runtimes = []
            for profile, voice in [('pewds', 'cove'), ('shopy', 'sol')]:
                data = f.config_data(voice={'voice': voice, 'choices': ['cove', 'sol']})
                data['profile']['id'] = profile
                runtime = TalkRuntime(parse_config(data, home), f.BOT_TOKEN, history,
                                      soul=f.file_soul(home), skill=f.file_skill(home))
                runtimes.append(runtime)
                self.assertEqual(runtime.reply_voice(f.USER_ID), voice)
                self.assertEqual({v['id'] for v in runtime.set_voice(f.USER_ID)['voice']['choices']},
                                 {'arbor', 'breeze', 'cove', 'ember', 'juniper', 'maple', 'sol', 'spruce', 'vale'})
            runtimes[0].set_voice(f.USER_ID, 'vale')
            self.assertEqual(runtimes[1].reply_voice(f.USER_ID), 'sol')
            self.assertEqual(runtimes[0].reply_voice(f.OTHER_USER_ID), 'cove')
            self.assertEqual(runtimes[0].set_voice(f.USER_ID)['voice']['applies'], 'next_connection')


if __name__ == '__main__':
    result = unittest.main(exit=False)
    if result.result.wasSuccessful(): print('TALK_COMPACT_BACKEND_OK')
    sys.exit(0 if result.result.wasSuccessful() else 1)

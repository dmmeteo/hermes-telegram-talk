import io
import json
import logging
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = Path(os.environ['TALK_BACKEND_SOURCE'])
FRONTEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
import server
from hermes_talk import fixtures as f
from hermes_talk.config import parse_config
from hermes_talk.history import MemoryHistory
from hermes_talk.talk import TalkRuntime
from hermes_talk.thread_inbox import Thread

offers, answers = queue.Queue(), {}
provider_generation = 0
provider_lock = threading.Lock()
logs = io.StringIO()
stats = {'resolves': 0, 'posts': 0}
home = f.write_home(tempfile.mkdtemp(prefix='synthetic-talk-'))
history = MemoryHistory()
# Two reply voices, so the selector is part of every layout this inventory checks (synthetic config).
data = f.config_data(voice={'voice': 'cove', 'choices': ['cove', 'sol']})
if os.environ.get('TALK_COMPACT_PROFILE') == 'pewds':
    data['profile'].update(id='pewds', display_name='Synthetic Pewds')
if os.environ.get('TALK_COMPACT_PROFILE') == 'shopy':
    data['profile'].update(id='shopy', display_name='Synthetic Shopy')
    data['voice']['voice'] = 'sol'
    data['persona'] = {'self_reference': 'feminine'}
talk = TalkRuntime(parse_config(data, home), f.BOT_TOKEN, history,
                   soul=f.file_soul(home), skill=f.file_skill(home), home=home)
# What `/talk` in the synthetic user's private chat would hand the Mini App: one launch bound to that chat.
LAUNCH = talk.inbox.issue_launch(Thread(talk.config.profile_id, talk.bot_id, f.USER_ID, str(f.USER_ID)))


def resolve():
    stats['resolves'] += 1
    return f.SYNTHETIC_TOKEN, server._jwt_account(f.SYNTHETIC_TOKEN)


def relay(url, headers, body):
    assert url == server.GPT_LIVE_URL
    reply = queue.Queue(maxsize=1)
    with provider_lock:
        stats['posts'] += 1
        identity = str(stats['posts'])
        answers[identity] = reply
    offers.put({'id': identity, 'sdp': body['sdp']})
    try:
        answer = reply.get(timeout=20)
    except queue.Empty:
        return 504, '', ''
    finally:
        with provider_lock:
            answers.pop(identity, None)
    return 201, answer, f'/v1/realtime/calls/rtc_fixture{identity}'


app = server.VoiceApp(f.BOT_TOKEN, {f.USER_ID, f.OTHER_USER_ID}, 'http://127.0.0.1:8801',
                      resolve=resolve, post=relay, talk=talk, avatar_fetch=lambda *_: None)
app.limiter.limit = 100
for limiter in app.talk_limiters.values():
    limiter.limit = 500


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, data, content_type='application/json'):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path == '/provider.html':
            return self.send(200, (Path(__file__).parent / 'provider.html').read_bytes(), 'text/html')
        if self.path == '/offer':
            with provider_lock:
                generation = provider_generation
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                with provider_lock:
                    if generation != provider_generation:
                        return self.send(204, b'')
                    try:
                        sdp = offers.get_nowait()
                    except queue.Empty:
                        sdp = None
                if sdp is not None:
                    return self.send(200, sdp)
                time.sleep(0.05)
            return self.send(204, b'')
        if self.path == '/identity':
            return self.send(200, {'initData': f.sign_init_data(f.USER_ID, int(time.time())), 'launch': LAUNCH})
        if self.path == '/other-identity':
            return self.send(200, {'initData': f.sign_init_data(f.OTHER_USER_ID, int(time.time()))})
        if self.path == '/state':
            return self.send(200, {**stats, 'log': logs.getvalue(),
                                   'messages': history.messages})
        self.send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        if self.path == '/provider/reset':
            global provider_generation
            with provider_lock:
                provider_generation += 1
            return self.send(200, {})
        if self.path == '/answer':
            with provider_lock:
                reply = answers.get(body.get('id'))
                if reply is None:
                    return self.send(410, {})
                try:
                    reply.put_nowait(body['sdp'])
                except queue.Full:
                    pass
            return self.send(200, {})
        self.send(404, {})


if __name__ == '__main__':
    handler = logging.StreamHandler(logs)
    server.logger.addHandler(handler)
    server.logger.setLevel(logging.INFO)
    httpd = server.BoundedServer(('127.0.0.1', 8801), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print('TALK FIXTURE READY', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8802), Fixture).serve_forever()

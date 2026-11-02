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

offers, answers = queue.Queue(), queue.Queue()
logs = io.StringIO()
stats = {'resolves': 0, 'posts': 0}
home = f.write_home(tempfile.mkdtemp(prefix='synthetic-talk-'))
history = MemoryHistory()
talk = TalkRuntime(parse_config(f.config_data(), home), f.BOT_TOKEN, history,
                   soul=f.file_soul(home), skill=f.file_skill(home))


def resolve():
    stats['resolves'] += 1
    return f.SYNTHETIC_TOKEN, server._jwt_account(f.SYNTHETIC_TOKEN)


def relay(url, headers, body):
    assert url == server.GPT_LIVE_URL
    stats['posts'] += 1
    offers.put(body['sdp'])
    try:
        answer = answers.get(timeout=20)
    except queue.Empty:
        return 504, '', ''
    return 201, answer, f'/v1/realtime/calls/rtc_fixture{stats["posts"]}'


app = server.VoiceApp(f.BOT_TOKEN, {f.USER_ID, f.OTHER_USER_ID}, 'http://127.0.0.1:8801',
                      resolve=resolve, post=relay, talk=talk)
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
            try:
                return self.send(200, {'sdp': offers.get(timeout=20)})
            except queue.Empty:
                return self.send(204, b'')
        if self.path == '/identity':
            return self.send(200, {'initData': f.sign_init_data(f.USER_ID, int(time.time()))})
        if self.path == '/other-identity':
            return self.send(200, {'initData': f.sign_init_data(f.OTHER_USER_ID, int(time.time()))})
        if self.path == '/state':
            return self.send(200, {**stats, 'log': logs.getvalue(),
                                   'messages': history.messages})
        self.send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        if self.path == '/answer':
            answers.put(body['sdp'])
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

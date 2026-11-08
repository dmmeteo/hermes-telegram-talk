"""Synthetic fixtures for offline checks and frontend work. Nothing here is real.

  python3 -m hermes_talk.fixtures --port 8792

serves the real Talk server on 127.0.0.1 with synthetic history, a synthetic
credential, a fake upstream that answers any offer with a fixed SDP, and
prints a freshly signed synthetic initData for user 1001.
"""
import argparse
import hashlib
import hmac
import json
import tempfile
import time
import urllib.parse
from pathlib import Path

BOT_TOKEN = "123456:synthetic-offline-bot-token"
USER_ID = 1001
OTHER_USER_ID = 1002
ORIGIN = "https://her.dmmeteo.dev"
SOUL_MARKER = "SYNTHETIC-SOUL-MARKER"
SKILL_MARKER = "SYNTHETIC-SKILL-TALK-BRIEF"
OTHER_SKILL_MARKER = "SYNTHETIC-SKILL-NOT-ALLOWED"
# Assembled at import so plugin install scanners do not take this synthetic value for a leaked key.
SECRET_IN_SOUL = "sk-" + "synthetic" + "0" * 22
SYNTHETIC_TOKEN = "header.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoic3ludGgtYWNjdCJ9fQ.sig"
ANSWER = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=answer\r\n"
OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=offer-marker\r\n"


def sign_init_data(user_id, auth_date, token=BOT_TOKEN):
    fields = {"auth_date": str(auth_date), "query_id": "AAEsynthetic",
              "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def write_home(home):
    """A disposable HERMES_HOME with a synthetic SOUL and two synthetic skills."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "SOUL.md").write_text(f"# Synthetic persona\n{SOUL_MARKER}. Calm and brief. "
                                  f"Example key {SECRET_IN_SOUL} must never be spoken.\n")
    for name, marker in (("talk-brief", SKILL_MARKER), ("not-allowed", OTHER_SKILL_MARKER)):
        folder = home / "skills" / "voice" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Synthetic voice skill.\n---\n\n"
                                         f"{marker}: keep answers under three sentences.\n")
    return home


def file_soul(home):
    return lambda: (Path(home) / "SOUL.md").read_text()


def file_skill(home):
    def load(name):
        for path in (Path(home) / "skills").glob(f"*/{name}/SKILL.md"):
            return path.read_text()
        return ""
    return load


def config_data(**overrides):
    data = {"profile": {"id": "synthetic", "display_name": "Synthetic Talk"},
            "appearance": {"listen_color": "#4FC3F7", "speak_color": "#FFB74D"},
            "greeting": {"enabled": True, "text": "Say hello in one short sentence."},
            "context": {"skills": ["talk-brief"]},
            "activity": {"owner_user_id": USER_ID},
            "privacy": {"persist_audio": False}}
    data.update(overrides)
    return data


class FakeUpstream:
    def __init__(self):
        self.status, self.answer, self.location = 201, ANSWER, "/v1/realtime/calls/rtc_fixture1"
        self.posts, self.resolves, self.resolve_error = [], 0, None

    def resolve(self):
        self.resolves += 1
        if self.resolve_error:
            raise self.resolve_error
        import server
        return SYNTHETIC_TOKEN, server._jwt_account(SYNTHETIC_TOKEN)

    def post(self, url, headers, body):
        self.posts.append((url, headers, body))
        return self.status, self.answer, self.location


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import server
    from .config import parse_config
    from .history import MemoryHistory
    from .talk import TalkRuntime
    parser = argparse.ArgumentParser(description="Synthetic Hermes Telegram Talk fixture server")
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    home = write_home(tempfile.mkdtemp(prefix="talk-fixture-"))
    fake = FakeUpstream()
    talk = TalkRuntime(parse_config(config_data(), home), BOT_TOKEN, MemoryHistory(),
                       soul=file_soul(home), skill=file_skill(home))
    app = server.VoiceApp(BOT_TOKEN, {USER_ID, OTHER_USER_ID}, f"http://127.0.0.1:{args.port}", "/tg-voice/",
                          resolve=fake.resolve, post=fake.post, talk=talk)
    httpd = server.BoundedServer(("127.0.0.1", args.port), server.make_handler(app))
    print(json.dumps({"base": f"http://127.0.0.1:{args.port}/tg-voice/",
                      "initData": sign_init_data(USER_ID, int(time.time()))}), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

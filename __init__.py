"""Hermes Talk plugin: adds `hermes talk serve`, the Telegram Mini App voice server for the active profile.

The command runs server.py from this plugin directory under the current Hermes interpreter and
HERMES_HOME. It changes no Hermes core code and leaves the stock Telegram adapter alone.
"""
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent


def _setup(parser):
    commands = parser.add_subparsers(dest="talk_command", required=True)
    serve = commands.add_parser("serve", help="Serve /tg-voice/ with authenticated GPT-Live signaling")
    serve.add_argument("--config", required=True, help="Hermes Talk YAML (see talk.example.yaml)")
    serve.add_argument("--origin", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8791)
    serve.add_argument("--base-path", default="/tg-voice/")


def _run(args):
    os.execv(sys.executable, [sys.executable, str(PLUGIN_DIR / "server.py"), "--config", args.config,
                              "--origin", args.origin, "--host", args.host, "--port", str(args.port),
                              "--base-path", args.base_path])


def register(ctx):
    ctx.register_cli_command("talk", "Hermes Talk Telegram voice server", _setup, _run, description=__doc__)

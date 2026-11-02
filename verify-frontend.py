#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
DEFAULT_BACKEND = '/home/me/.hermes/run/coding/prime-tgv2-back-1102/accepted-backend-source'


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--offline', action='store_true')
    mode.add_argument('--browser', metavar='OUT_DIR')
    args = parser.parse_args()
    backend = Path(os.environ.get('TALK_BACKEND_SOURCE', DEFAULT_BACKEND))
    manifest = json.loads((ROOT / 'tests/frontend/backend-manifest.json').read_text())
    for name, digest in manifest['files'].items():
        path = backend / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            print('FAIL frozen backend missing or modified:', name)
            return 1
    env = {**os.environ, 'TALK_BACKEND_SOURCE': str(backend), 'PYTHONDONTWRITEBYTECODE': '1'}
    commands = [[sys.executable, 'verify.py', '--offline'],
                ['node', 'tests/frontend/check.mjs'],
                [sys.executable, 'tests/frontend/test_contract.py']]
    if args.browser:
        out = Path(args.browser).resolve()
        out.mkdir(parents=True, exist_ok=True)
        if os.environ.get('TALK_CAPPED_BROWSER') != '1':
            print('FAIL browser must run in the admitted offline container with TALK_CAPPED_BROWSER=1')
            return 1
        commands.append(['node', '/app/talk-job.mjs', str(out)])
    for cmd in commands:
        result = subprocess.run(cmd, cwd=ROOT, env=env)
        if result.returncode:
            return result.returncode
    print('TALK_FRONTEND_' + ('BROWSER' if args.browser else 'OFFLINE') + '_OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())

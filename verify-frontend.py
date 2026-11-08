#!/usr/bin/env python3
"""Talk frontend checks against the backend in this checkout. Synthetic identities and media only.

  python3 verify-frontend.py --offline            legacy offline suite, frontend unit checks, real-backend HTTP contract
  python3 verify-frontend.py --browser OUT_DIR    the same, then the Talk Chromium inventory in a capped offline container
  python3 verify-frontend.py --mobile OUT_DIR     the same, then cold/warm upgrade and audio unlock in Chromium and WebKit

Node runs from PATH, else from TALK_NODE_IMAGE in a network-less container. The browser job runs in
TALK_BROWSER_IMAGE (Playwright with Chromium) with no network. TALK_BACKEND_SOURCE may name another
backend checkout; its files must match tests/frontend/backend-manifest.json either way.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
NODE_IMAGE = os.environ.get('TALK_NODE_IMAGE', 'node:22.22.3-bookworm-slim')
BROWSER_IMAGE = os.environ.get('TALK_BROWSER_IMAGE', 'django-admin-repl-verify-browser:pw1.63.0')


def capped(image, command, *mounts, memory='256m', cpus='1'):
    cmd = ['docker', 'run', '--rm', '--init', '--network', 'none', '--memory', memory, '--memory-swap', memory,
           '--cpus', cpus, '--pids-limit', '384', '--shm-size', '256m', '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges:true', '--tmpfs', '/tmp:rw,size=256m', '--env', 'HOME=/tmp/home',
           '--env', 'TMPDIR=/tmp', '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'TALK_BACKEND_SOURCE=/src',
           '--mount', f'type=bind,src={ROOT},dst=/src,readonly', '--workdir', '/src']
    for mount in mounts:
        cmd += ['--mount', mount]
    return cmd + ['--entrypoint', command[0], image, *command[1:]]


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--offline', action='store_true')
    mode.add_argument('--browser', metavar='OUT_DIR')
    mode.add_argument('--mobile', metavar='OUT_DIR')
    args = parser.parse_args()
    backend = Path(os.environ.get('TALK_BACKEND_SOURCE', ROOT))
    manifest = json.loads((ROOT / 'tests/frontend/backend-manifest.json').read_text())
    for name, digest in manifest['files'].items():
        path = backend / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            print('FAIL backend missing or not the manifest revision:', name)
            return 1
    env = {**os.environ, 'TALK_BACKEND_SOURCE': str(backend), 'PYTHONDONTWRITEBYTECODE': '1'}
    check = ['node', 'tests/frontend/check.mjs']
    commands = [[sys.executable, 'verify.py', '--offline'],
                check if shutil.which('node') else capped(NODE_IMAGE, check),
                [sys.executable, 'tests/frontend/test_contract.py']]
    if args.browser:
        out = Path(args.browser).resolve()
        out.mkdir(parents=True, exist_ok=True)
        if backend.resolve() != ROOT:
            print('FAIL the browser job serves this checkout; unset TALK_BACKEND_SOURCE')
            return 1
        commands.append(capped(BROWSER_IMAGE, ['node', '/app/talk-job.mjs', '/output'],
                               f'type=bind,src={ROOT / "tests/browser/run.mjs"},dst=/app/talk-job.mjs,readonly',
                               f'type=bind,src={out},dst=/output', memory='2g', cpus='2'))
    if args.mobile:
        if backend.resolve() != ROOT:
            print('FAIL the mobile job serves this checkout; unset TALK_BACKEND_SOURCE')
            return 1
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
            revision += '-dirty'
        avatar = Path(os.environ['TALK_TEST_AVATAR']).resolve() if os.environ.get('TALK_TEST_AVATAR') else None
        extra_mounts = [f'type=bind,src={avatar},dst=/input/avatar{avatar.suffix},readonly'] if avatar else []
        extra_env = [f'TALK_TEST_AVATAR=/input/avatar{avatar.suffix}'] if avatar else []
        for engine in ('chromium', 'webkit'):
            out = Path(args.mobile).resolve() / engine
            out.mkdir(parents=True, exist_ok=True)
            commands.append(capped(BROWSER_IMAGE,
                ['env', f'TALK_SOURCE_REVISION={revision}', *extra_env, 'node', '/app/mobile.mjs', '/output', engine],
                f'type=bind,src={ROOT / "tests/browser/mobile.mjs"},dst=/app/mobile.mjs,readonly',
                f'type=bind,src={out},dst=/output', *extra_mounts, memory='2g', cpus='2'))
    for cmd in commands:
        result = subprocess.run(cmd, cwd=ROOT, env=env)
        if result.returncode:
            return result.returncode
    print('TALK_FRONTEND_' + ('MOBILE' if args.mobile else 'BROWSER' if args.browser else 'OFFLINE') + '_OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())

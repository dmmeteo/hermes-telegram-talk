"""Complete compact backend and real-media browser regression gate."""
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if __name__ == '__main__':
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / 'tests/compact/check.py')], check=True, cwd=ROOT)
    spec = importlib.util.spec_from_file_location('frontend_gate', ROOT / 'verify-frontend.py')
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT): revision += '-dirty'
    cmd = gate.capped(gate.BROWSER_IMAGE,
                      ['env', f'TALK_SOURCE_REVISION={revision}', 'node', '/app/compact.mjs', '/output'],
                      f'type=bind,src={ROOT / "tests/compact/browser.mjs"},dst=/app/compact.mjs,readonly',
                      f'type=bind,src={out},dst=/output', memory='1792m', cpus='2')
    subprocess.run(cmd, check=True, cwd=ROOT)
    print('TALK_COMPACT_UI_OK')

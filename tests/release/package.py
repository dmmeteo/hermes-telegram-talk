"""Build an exact installed-plugin artifact from committed main, independent of the caller's checkout."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

from assets import validate

FILES = ('plugin.yaml', '__init__.py', 'server.py', 'hermes_talk', 'static', 'LICENSE')


def build(repo, output):
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'refs/heads/main'], text=True).strip()
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', revision, '--', *FILES])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.talk-release-', dir=output.parent) as temporary:
        stage = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            if any(not (m.isfile() or m.isdir()) for m in bundle.getmembers()):
                raise ValueError('Release artifacts may contain only regular files and directories')
            bundle.extractall(stage, filter='data')
        validate(stage)
        for name in FILES:
            if not (stage / name).exists():
                raise ValueError(f'Missing release input: {name}')
        hashes = {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in stage.rglob('*') if p.is_file()}
        (stage / 'SOURCE.json').write_text(json.dumps({'revision': revision, 'branch': 'main', 'files': hashes},
                                                    indent=2) + '\n')
        stage.rename(output)
    return revision


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[2], args.output))

import argparse
from pathlib import Path
import subprocess
import tempfile
import unittest

from assets import validate
from package import build

ROOT = Path(__file__).resolve().parents[2]


class Release(unittest.TestCase):
    def test_current_assets_match_served_routes(self):
        self.assertEqual(validate(ROOT), ['talk/app.css', 'talk/app.js', 'talk/index.html'])

    def test_package_reads_only_committed_main_and_drops_untracked_files(self):
        with tempfile.TemporaryDirectory(prefix='talk-release-test-') as temporary:
            root = Path(temporary)
            repo = root / 'repo'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)
            git('init', '-b', 'main')
            for name in ('plugin.yaml', '__init__.py', 'LICENSE'):
                (repo / name).write_text('synthetic\n')
            (repo / 'server.py').write_text("STATIC_FILES = {'': ('index.html', 'html'), 'app.js': ('app.js', 'js'), 'app.css': ('app.css', 'css')}\n")
            (repo / 'hermes_talk').mkdir()
            (repo / 'hermes_talk/__init__.py').write_text('')
            (repo / 'static/talk').mkdir(parents=True)
            for name in ('index.html', 'app.js', 'app.css'):
                (repo / 'static/talk' / name).write_text('committed main')
            git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'synthetic main')
            git('checkout', '-b', 'old-worker')
            (repo / 'static/index.html').write_text('obsolete legacy UI')
            (repo / 'static/talk/app.js').write_text('uncommitted worker version')
            with self.assertRaisesRegex(ValueError, 'extra='):
                validate(repo)
            output = root / 'release'
            build(repo, output)
            self.assertEqual((output / 'static/talk/app.js').read_text(), 'committed main')
            self.assertFalse((output / 'static/index.html').exists())
            self.assertTrue((output / 'SOURCE.json').is_file())
            with self.assertRaises(FileExistsError):
                build(repo, output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    if args.root:
        print(validate(args.root))
    else:
        unittest.main(argv=['check'], verbosity=2)

import ast
from pathlib import Path


def validate(root):
    root = Path(root)
    module = ast.parse((root / 'server.py').read_text())
    routes = next(ast.literal_eval(node.value) for node in module.body
                  if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'STATIC_FILES'
                                                         for t in node.targets))
    expected = {Path('talk') / row[0] for row in routes.values()}
    actual = {p.relative_to(root / 'static') for p in (root / 'static').rglob('*') if p.is_file() or p.is_symlink()}
    missing, extra = expected - actual, actual - expected
    links = [p for p in actual if (root / 'static' / p).is_symlink()]
    if missing or extra or links:
        raise ValueError(f'static must contain only assets served by server.STATIC_FILES; '
                         f'missing={sorted(map(str, missing))}, extra={sorted(map(str, extra))}, '
                         f'symlinks={sorted(map(str, links))}. Move fixtures, legacy UI and scratch files outside static.')
    return sorted(map(str, actual))

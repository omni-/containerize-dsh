"""Compatibility fix for the directory browser in pinned DSH 0.1.5-rc.3.

Session creation already defaults to process.cwd(), but the directory browser
defaults to homedir() and has no configuration for its initial directory.
Keep HOME (credentials, history, caches) separate from the project directory.
Fail the image build if an upstream change invalidates this narrow patch.
"""
from pathlib import Path


def patch(root):
    # npm can hoist this dependency or nest it under dsh-web-app.
    paths = sorted(root.rglob('@deepseek-ai/dsh-host-directory-picker-browse/lib/index.js'))
    if not paths:
        raise RuntimeError(f'DSH directory browser not found under {root}; review the workspace default patch')
    original = 'const target = resolve(path ?? home);'
    replacements = []
    for path in paths:
        source = path.read_text(encoding='utf-8')
        if source.count(original) != 1:
            raise RuntimeError(f'DSH directory browser changed at {path}; review the workspace default patch')
        replacements.append((path, source.replace(original, 'const target = resolve(path ?? process.cwd());')))
    for path, source in replacements:
        path.write_text(source, encoding='utf-8')


if __name__ == '__main__':
    patch(Path('/usr/local/lib/node_modules/@deepseek-ai/dsh'))

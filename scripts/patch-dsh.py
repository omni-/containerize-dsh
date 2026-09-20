"""Compatibility fix for the directory browser in pinned DSH 0.1.5-rc.1.

Session creation already defaults to process.cwd(), but the directory browser
defaults to homedir() and has no configuration for its initial directory.
Keep HOME (credentials, history, caches) separate from the project directory.
Fail the image build if an upstream change invalidates this narrow patch.
"""
from pathlib import Path


def patch(root):
    path = root / 'dsh-host-directory-picker-browse/lib/index.js'
    source = path.read_text()
    original = 'const target = resolve(path ?? home);'
    if source.count(original) != 1:
        raise RuntimeError('DSH directory browser changed; review the workspace default patch')
    path.write_text(source.replace(original, 'const target = resolve(path ?? process.cwd());'))


if __name__ == '__main__':
    patch(Path('/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai'))

"""Install the launcher and explicit-only Codex and Claude Code skills at user scope."""
import os
from pathlib import Path
import shlex
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def validate_skill_paths(source, destination):
    """Check an installation before changing either skill or the launcher."""
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if (destination.is_symlink()
            or getattr(destination, 'is_junction', lambda: False)()
            or destination.resolve().is_relative_to(source)
            or source.is_relative_to(destination.resolve())):
        raise ValueError('Skill source and installation must be separate ordinary directories')
    if destination.exists() and not (destination / '.containerize-dsh-install').is_file():
        raise ValueError(f'An unmanaged dsh-work skill already exists at {destination}; refusing to overwrite')
    if not (source / 'SKILL.md').is_file():
        raise ValueError('Canonical skill source is missing SKILL.md')
    return source, destination


def install_skill(source, destination, launcher, *, claude_code=False):
    """Replace the generated installation, including files removed from source."""
    source, destination = validate_skill_paths(source, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Stage on the same filesystem. A failed copy leaves the current install intact.
    # Use normal inherited permissions: Python's private temporary directories on
    # Windows would make the renamed installation unreadable to sandboxed agents.
    staging = destination.parent / ('.dsh-work-update-' + uuid.uuid4().hex)
    staging.mkdir()
    staging = staging.resolve()
    if not staging.is_relative_to(destination.parent.resolve()):
        raise ValueError('Staging directory escaped the installation parent')
    try:
        fresh, previous = staging / 'fresh', staging / 'previous'
        # Claude Code uses frontmatter instead of Codex's agents/ metadata.
        ignore = shutil.ignore_patterns('agents') if claude_code else None
        shutil.copytree(source, fresh, ignore=ignore)
        if claude_code:
            skill_file = fresh / 'SKILL.md'
            content = skill_file.read_text(encoding='utf-8')
            if not content.startswith('---\n') or '\n---\n' not in content:
                raise ValueError('Canonical skill source is missing YAML frontmatter')
            content = content.replace('---\n', '---\ndisable-model-invocation: true\n', 1)
            skill_file.write_text(content, encoding='utf-8')
        (fresh / '.containerize-dsh-install').write_text(str(ROOT), encoding='utf-8')
        (fresh / 'references' / 'launcher.txt').write_text(str(launcher) + '\n', encoding='utf-8')
        if destination.exists():
            destination.rename(previous)
        try:
            fresh.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    finally:
        # Only this unique, checked staging directory (and the replaced copy).
        shutil.rmtree(staging)


def main():
    codex = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
    codex_skill = codex / 'skills' / 'dsh-work'
    claude_skill = claude / 'skills' / 'dsh-work'
    source = ROOT / 'skills' / 'dsh-work'
    for skill in (codex_skill, claude_skill):
        validate_skill_paths(source, skill)
    if codex_skill.resolve() == claude_skill.resolve():
        raise ValueError('Codex and Claude Code skills must use separate installation directories')
    if sys.platform == 'win32':
        import winreg
        bindir = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'containerize-dsh' / 'bin'
        launcher = bindir / 'dsh-task.cmd'
        content = '@rem containerize-dsh launcher\n@"' + sys.executable.replace('%', '%%') + '" "' + str(ROOT / 'dsh_task.py').replace('%', '%%') + '" %*\n'
    else:
        bindir = Path.home() / '.local' / 'bin'
        launcher = bindir / 'dsh-task'
        content = '#!/bin/sh\n# containerize-dsh launcher\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(ROOT / 'dsh_task.py')) + ' "$@"\n'
    if launcher.exists() and 'containerize-dsh launcher' not in launcher.read_text():
        raise ValueError('An unrelated launcher already exists; refusing to overwrite')
    bindir.mkdir(parents=True, exist_ok=True)
    launcher.write_text(content, encoding='utf-8')
    if sys.platform != 'win32':
        launcher.chmod(0o755)
    install_skill(source, codex_skill, launcher)
    install_skill(source, claude_skill, launcher, claude_code=True)
    if sys.platform == 'win32':
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            try:
                current, kind = winreg.QueryValueEx(key, 'Path')
            except FileNotFoundError:
                current, kind = '', winreg.REG_EXPAND_SZ
            if str(bindir).casefold() not in [p.rstrip('\\/').casefold() for p in current.split(';')]:
                winreg.SetValueEx(key, 'Path', 0, kind, current.rstrip(';') + ';' + str(bindir))
        # Notify Explorer so newly launched apps inherit the new user PATH.
        import ctypes
        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(65535, 26, 0, 'Environment', 2, 5000, ctypes.byref(result))
    print('Launcher:', launcher)
    print('Codex skill:', codex_skill)
    print('Claude Code skill:', claude_skill)
    print('Restart your terminal/app to refresh PATH and skill discovery.' if sys.platform == 'win32'
          else 'Ensure ~/.local/bin is on PATH; restart Codex/Claude Code for skill discovery.')


if __name__ == '__main__':
    main()

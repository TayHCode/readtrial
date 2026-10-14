"""Check committed HEAD in a local clone and fresh offline venv; keep evidence in .scratch."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    scratch = root / '.scratch'
    scratch.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix='offline-install-', dir=scratch))
    source, venv, home, temp = (run / name for name in ('source', 'venv', 'home', 'tmp'))
    home.mkdir()
    temp.mkdir()
    env = {**os.environ, 'HOME': str(home), 'TMPDIR': str(temp),
           'PIP_CONFIG_FILE': os.devnull, 'PIP_NO_INDEX': '1', 'PIP_NO_DEPS': '1',
           'PIP_NO_CACHE_DIR': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    report = {'status': 'fail', 'commands': []}
    print(f'Evidence: {run}', flush=True)

    def check(argv, role, cwd=source):
        index = len(report['commands']) + 1
        log = run / f'{index:02d}-{role}.log'
        with log.open('w', encoding='utf-8') as output:
            result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT)
        report['commands'].append({'argv': argv, 'role': role, 'exit_code': result.returncode,
                                   'output_path': str(log)})
        print(f'{role}: {shlex.join(argv)} -> {result.returncode}', flush=True)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, argv)

    try:
        check(['git', 'clone', '--quiet', '--no-hardlinks', str(root), str(source)], 'clone', root)
        report['commit'] = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=source, env=env, text=True).strip()
        check([sys.executable, '-m', 'venv', str(venv)], 'venv', root)
        env['VIRTUAL_ENV'] = str(venv)
        env['PATH'] = str(venv / ('Scripts' if os.name == 'nt' else 'bin')) + os.pathsep + env['PATH']
        check(['python', '--version'], 'interpreter')
        check(['python', '-m', 'pip', 'list', '--format=json'], 'seed-packages')
        manifest = json.loads((source / 'release-check.json').read_text(encoding='utf-8'))
        for command in manifest['quickstart'] + manifest['checks']:
            if 'file' in command:
                line = (source / command['file']).read_text(encoding='utf-8').splitlines()[command['line'] - 1]
                if shlex.split(line) != command['argv']:
                    raise ValueError(f"README/manifest mismatch: {command['file']}:{command['line']}")
            check(command['argv'], command.get('role', 'quickstart'))
        # Run outside the checkout so source imports cannot conceal a broken installation.
        check(['python', '-I', '-c',
               'import importlib.metadata, pathlib, readtrial, sys; '
               'assert pathlib.Path(readtrial.__file__).is_relative_to(pathlib.Path(sys.prefix)); '
               'assert importlib.metadata.version("readtrial") == readtrial.__version__'], 'installed-import', home)
        check(['readtrial', '--help'], 'installed-entrypoint', home)
        report['status'] = 'pass'
    finally:
        (run / 'results.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

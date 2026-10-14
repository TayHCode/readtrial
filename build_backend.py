"""Standard-library PEP 517 backend for readtrial's single pure-Python package."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
from pathlib import Path
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent


def _project():
    return tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']


def _metadata(project):
    headers = [
        'Metadata-Version: 2.4',
        f"Name: {project['name']}",
        f"Version: {project['version']}",
        f"Summary: {project['description']}",
        f"Requires-Python: {project['requires-python']}",
        f"License-Expression: {project['license']}",
        'License-File: LICENSE',
        'Author: ' + ', '.join(author['name'] for author in project['authors']),
        'Description-Content-Type: text/markdown',
        *[f'Requires-Dist: {item}' for item in project['dependencies']],
    ]
    return ('\n'.join(headers) + '\n\n'
            + (ROOT / project['readme']).read_text(encoding='utf-8')).encode('utf-8')


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    project = _project()
    stem = f"{project['name']}-{project['version']}"
    info = f'{stem}.dist-info'
    files = {path.relative_to(ROOT).as_posix(): path.read_bytes()
             for path in sorted((ROOT / 'readtrial').rglob('*.py'))}
    files.update({
        f'{info}/METADATA': _metadata(project),
        f'{info}/WHEEL': (b'Wheel-Version: 1.0\nGenerator: readtrial\n'
                         b'Root-Is-Purelib: true\nTag: py3-none-any\n'),
        f'{info}/entry_points.txt': ('[console_scripts]\n' + ''.join(
            f'{name} = {target}\n' for name, target in project['scripts'].items()
        )).encode('utf-8'),
        f'{info}/licenses/LICENSE': (ROOT / 'LICENSE').read_bytes(),
    })
    record = io.StringIO(newline='')
    writer = csv.writer(record)
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=')
        writer.writerow((name, 'sha256=' + digest.decode('ascii'), len(data)))
    writer.writerow((f'{info}/RECORD', '', ''))
    files[f'{info}/RECORD'] = record.getvalue().encode('utf-8')
    filename = f'{stem}-py3-none-any.whl'
    with zipfile.ZipFile(Path(wheel_directory) / filename, 'w', zipfile.ZIP_DEFLATED) as wheel:
        for name, data in files.items():
            wheel.writestr(name, data)
    return filename


def build_sdist(sdist_directory, config_settings=None):
    project = _project()
    stem = f"{project['name']}-{project['version']}"
    filename = f'{stem}.tar.gz'
    files = [ROOT / name for name in ('pyproject.toml', 'build_backend.py', 'README.md', 'LICENSE')]
    files += sorted((ROOT / 'readtrial').rglob('*.py'))
    with tarfile.open(Path(sdist_directory) / filename, 'w:gz') as archive:
        for path in files:
            archive.add(path, arcname=f'{stem}/{path.relative_to(ROOT).as_posix()}')
        metadata = _metadata(project)
        entry = tarfile.TarInfo(f'{stem}/PKG-INFO')
        entry.size = len(metadata)
        archive.addfile(entry, io.BytesIO(metadata))
    return filename

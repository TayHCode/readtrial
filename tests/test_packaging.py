"""The offline backend must produce complete wheels and rebuildable source archives."""
import base64
import csv
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_backend

ROOT = Path(__file__).parents[1]


class PackagingTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_wheel_contains_package_entrypoint_license_and_valid_record(self):
        filename = build_backend.build_wheel(self.directory)
        with zipfile.ZipFile(self.directory / filename) as wheel:
            info = next(name.rsplit('/', 1)[0] for name in wheel.namelist() if name.endswith('/RECORD'))
            self.assertEqual(wheel.read(f'{info}/licenses/LICENSE'), (ROOT / 'LICENSE').read_bytes())
            self.assertIn(b'readtrial = readtrial.cli:main', wheel.read(f'{info}/entry_points.txt'))
            for source in (ROOT / 'readtrial').rglob('*.py'):
                self.assertEqual(wheel.read(source.relative_to(ROOT).as_posix()), source.read_bytes())
            rows = list(csv.reader(io.StringIO(wheel.read(f'{info}/RECORD').decode('utf-8'))))
            self.assertEqual({row[0] for row in rows}, set(wheel.namelist()))
            for name, digest, size in rows:
                if name == f'{info}/RECORD':
                    self.assertEqual((digest, size), ('', ''))
                    continue
                data = wheel.read(name)
                self.assertEqual(int(size), len(data))
                expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode('ascii')
                self.assertEqual(digest, 'sha256=' + expected)

    def test_sdist_rebuilds_same_wheel_without_the_checkout(self):
        original = build_backend.build_wheel(self.directory)
        filename = build_backend.build_sdist(self.directory)
        extracted = self.directory / 'extracted'
        extracted.mkdir()
        with tarfile.open(self.directory / filename) as archive:
            archive.extractall(extracted, filter='data')
        source = next(extracted.iterdir())
        with patch.object(build_backend, 'ROOT', source):
            rebuilt = build_backend.build_wheel(extracted)
        with zipfile.ZipFile(self.directory / original) as first, zipfile.ZipFile(extracted / rebuilt) as second:
            self.assertEqual(first.namelist(), second.namelist())
            for name in first.namelist():
                self.assertEqual(first.read(name), second.read(name))

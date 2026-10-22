"""Nested controllers must explain unavailable loop storage without creating it."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from readtrial import setup
from readtrial.process import Result


def result(value):
    return Result(0, json.dumps(value), 0)


class NestedSetupTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('readtrial.setup.platform.system', return_value='Linux')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_loop_in_container_explains_doctor_dead_end(self):
        with patch('readtrial.setup.nested_loop_unavailable', return_value=True), patch('readtrial.setup.shutil.which', return_value='/snap/bin/lxc'), patch('readtrial.setup.in_lxd_group', return_value=True), patch('readtrial.setup.query', side_effect=[{'api_version': '1.0'}, [{'driver': 'dir', 'status': 'Created'}]]):
            report = setup.doctor('Linux')
        self.assertFalse(report['ready'])
        fix = report['checks'][-1]['fix']
        self.assertIn('nested container', fix)
        self.assertIn('real Linux host or VM', fix)
        self.assertNotIn('setup-linux --yes', fix)

    def test_existing_quota_pool_is_usable_without_loop_control(self):
        with patch('readtrial.setup.nested_loop_unavailable', return_value=True), patch('readtrial.setup.shutil.which', return_value='/snap/bin/lxc'), patch('readtrial.setup.in_lxd_group', return_value=True), patch('readtrial.setup.query', side_effect=[{'api_version': '1.0'}, [{'driver': 'btrfs', 'status': 'Created'}]]):
            self.assertTrue(setup.doctor('Linux')['ready'])

    def test_setup_refuses_nested_loop_creation_before_mutation(self):
        with contextlib.redirect_stdout(io.StringIO()), patch('readtrial.setup.nested_loop_unavailable', return_value=True), patch('readtrial.setup.query', side_effect=[[], [], []]), patch('readtrial.setup.execute') as run:
            with self.assertRaisesRegex(ValueError, 'real Linux host or VM'):
                setup.setup_linux(yes=True)
        run.assert_not_called()

    def test_matching_owned_pool_remains_idempotent_in_container(self):
        pool = {'name': 'readtrial', 'driver': 'btrfs', 'config': {'size': '30GiB', 'user.readtrial.setup': '1'}}
        with contextlib.redirect_stdout(io.StringIO()), patch('readtrial.setup.nested_loop_unavailable', return_value=True), patch('readtrial.setup.query', side_effect=[[pool], [], []]), patch('readtrial.setup.execute') as run:
            self.assertEqual(setup.setup_linux(yes=True), 0)
        run.assert_not_called()

    def test_detection_distinguishes_host_container_and_available_device(self):
        for container, device, expected in [('lxc', False, True), ('lxc', True, False), ('', False, False)]:
            with patch('readtrial.setup.Path.read_text', return_value=container), patch('readtrial.setup.Path.exists', return_value=device):
                self.assertEqual(setup.nested_loop_unavailable(), expected)
        with patch('readtrial.setup.Path.read_text', side_effect=FileNotFoundError):
            self.assertFalse(setup.nested_loop_unavailable())

    def test_pool_creation_error_gives_storage_advice_and_preserves_exact_undo(self):
        with contextlib.redirect_stdout(io.StringIO()), patch('readtrial.setup.nested_loop_unavailable', return_value=False), patch('readtrial.setup.query', side_effect=[[], [], []]), patch('readtrial.setup.execute', return_value=Result(1, 'private machine detail', 0)):
            with self.assertRaisesRegex(ValueError, 'quota-capable') as raised:
                setup.setup_linux(yes=True)
        self.assertIn('Inspect this exact pool', str(raised.exception))
        self.assertIn('real Linux host or VM', str(raised.exception))
        self.assertNotIn('private machine detail', str(raised.exception))

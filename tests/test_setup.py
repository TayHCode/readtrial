"""Read-only prerequisite checks and explicit, bounded pool provisioning."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import resolved_tmp
from readtrial import cli, setup
from readtrial.process import Result
from platforms import posix_only


class SetupTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('readtrial.setup.platform.system', return_value='Linux')
        patcher.start()
        self.addCleanup(patcher.stop)

    def result(self, value):
        return Result(0, json.dumps(value), 0)

    def test_doctor_missing_cli_is_read_only_and_plain(self):
        with patch('readtrial.setup.shutil.which', return_value=None), patch('readtrial.setup.execute') as run:
            report = setup.doctor('Linux')
        self.assertFalse(report['ready'])
        self.assertIn('sudo snap install lxd', report['checks'][0]['fix'])
        run.assert_not_called()
        self.assertNotIn(str(Path.home()), json.dumps(report))

    def test_doctor_checks_server_group_and_quota(self):
        with patch('readtrial.setup.shutil.which', return_value='/bin/lxc'), patch('readtrial.setup.in_lxd_group', return_value=True), patch('readtrial.setup.execute', side_effect=[self.result({'api_version': '1.0'}), self.result([{'name': 'readtrial', 'driver': 'btrfs', 'status': 'Created', 'config': {}}])]) as run:
            self.assertTrue(setup.doctor('Linux')['ready'])
        self.assertTrue(all('create' not in call.args[0] for call in run.call_args_list))

    def test_doctor_stock_ubuntu_bootstrap_launcher_is_not_installed_or_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / 'lxc'
            launcher.write_text('#!/bin/sh\nSNAP_BIN="/snap/bin/lxc"\n'
                                '# bootstrap via /run/lxd-installer.socket\n')
            with patch('readtrial.setup.shutil.which', return_value=str(launcher)), \
                 patch('readtrial.setup.os.access', return_value=False), \
                 patch('readtrial.setup.execute') as run:
                report = setup.doctor('Linux')
            self.assertFalse(report['checks'][0]['ready'])
            self.assertEqual(report['checks'][0]['fix'], 'sudo snap install lxd')
            run.assert_not_called()

    def test_doctor_bootstrap_launcher_with_installed_snap_can_check_server(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / 'lxc'
            launcher.write_text('#!/bin/sh\nSNAP_BIN="/snap/bin/lxc"\n'
                                '# bootstrap via /run/lxd-installer.socket\n')
            with patch('readtrial.setup.shutil.which', return_value=str(launcher)), \
                 patch('readtrial.setup.os.access', return_value=True) as access, \
                 patch('readtrial.setup.in_lxd_group', return_value=True), \
                 patch('readtrial.setup.execute', side_effect=[
                     self.result({'api_version': '1.0'}),
                     self.result([{'driver': 'btrfs', 'status': 'Created'}])]):
                self.assertTrue(setup.doctor('Linux')['ready'])
            access.assert_called_once_with('/snap/bin/lxc', __import__('os').X_OK)

    def test_doctor_native_cli_does_not_require_a_snap_install(self):
        with tempfile.TemporaryDirectory() as temp:
            native = Path(temp) / 'lxc'
            native.write_bytes(b'\x7fELF\x00native CLI fixture')
            with patch('readtrial.setup.shutil.which', return_value=str(native)), \
                 patch('readtrial.setup.os.access') as access, \
                 patch('readtrial.setup.in_lxd_group', return_value=True), \
                 patch('readtrial.setup.execute', side_effect=[
                     self.result({'api_version': '1.0'}),
                     self.result([{'driver': 'btrfs', 'status': 'Created'}])]):
                self.assertTrue(setup.doctor('Linux')['ready'])
            access.assert_not_called()

    @posix_only
    def test_doctor_other_platforms_need_real_prerequisites(self):
        with patch('readtrial.setup.execute', return_value=Result(0, 'Disabled', 0)):
            self.assertFalse(setup.doctor('Windows')['ready'])
        with patch('readtrial.setup.execute') as run:
            self.assertFalse(setup.doctor('Darwin')['ready'])
            run.assert_not_called()

    def test_setup_without_yes_prints_plan_and_never_calls_lxc(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('readtrial.setup.execute') as run:
            self.assertEqual(cli.main(['setup-linux']), 2)
        run.assert_not_called()
        self.assertIn('30GiB', output.getvalue())
        self.assertIn('--yes', output.getvalue())

    def test_setup_new_pool_capped_and_undo_exact(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1:3] == ['storage', 'list']: return self.result([])
            if argv[1] == 'list': return self.result([])
            if argv[1:3] == ['profile', 'list']: return self.result([])
            if argv[1:3] == ['storage', 'create']: return Result(0, '', 0)
            return self.result({'driver': 'btrfs', 'config': {'size': '7GiB', 'user.readtrial.setup': '1'}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('readtrial.setup.execute', side_effect=run):
            self.assertEqual(setup.setup_linux(pool='sample-test', size_gib=7, yes=True), 0)
        self.assertIn(['lxc', 'storage', 'create', 'sample-test', 'btrfs', 'size=7GiB', 'user.readtrial.setup=1'], calls)
        self.assertIn('lxc storage delete sample-test', output.getvalue())

    def test_existing_foreign_pool_refused_and_owned_pool_idempotent(self):
        for owned in (False, True):
            pool = {'name': 'readtrial', 'driver': 'btrfs', 'config': {'size': '30GiB'}}
            if owned: pool['config']['user.readtrial.setup'] = '1'
            with contextlib.redirect_stdout(io.StringIO()), patch('readtrial.setup.execute', side_effect=[self.result([pool]), self.result([]), self.result([])]) as run:
                if owned: self.assertEqual(setup.setup_linux(yes=True), 0)
                else:
                    with self.assertRaisesRegex(ValueError, 'existing'): setup.setup_linux(yes=True)
            self.assertFalse(any('create' in call.args[0] for call in run.call_args_list))

    def test_same_named_profile_or_instance_and_bad_inputs_never_mutate(self):
        for profiles, instances in [([{'name': 'readtrial'}], []), ([], [{'name': 'readtrial'}])]:
            with contextlib.redirect_stdout(io.StringIO()), patch('readtrial.setup.execute', side_effect=[self.result([]), self.result(instances), self.result(profiles)]) as run:
                with self.assertRaises(ValueError): setup.setup_linux(yes=True)
            self.assertEqual(run.call_count, 3)
        for options in ({'pool': '-bad'}, {'size_gib': 0}, {'location': '/relative/../bad'}):
            with patch('readtrial.setup.execute') as run:
                with self.assertRaises(ValueError): setup.setup_linux(yes=True, **options)
            run.assert_not_called()

    def test_failed_or_truncated_queries_refuse_creation(self):
        for result in (Result(1, 'private machine error', 0), Result(0, '[]', 0, truncated=True)):
            with contextlib.redirect_stdout(io.StringIO()), patch('readtrial.setup.execute', return_value=result) as run:
                with self.assertRaises(ValueError): setup.setup_linux(yes=True)
            self.assertEqual(run.call_count, 1)

    def test_owned_custom_loop_location_is_idempotent_and_foreign_file_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            location=Path(temp)/'loop.img';location.write_bytes(b'existing file')
            owned={'name':'readtrial','driver':'btrfs','config':{'size':'30GiB','source':str(location),'user.readtrial.setup':'1'}}
            with contextlib.redirect_stdout(io.StringIO()),patch('readtrial.setup.execute',side_effect=[self.result([owned]),self.result([]),self.result([])]) as run:
                self.assertEqual(setup.setup_linux(location=str(location),yes=True),0)
            self.assertFalse(any('create' in c.args[0] for c in run.call_args_list))
            with contextlib.redirect_stdout(io.StringIO()),patch('readtrial.setup.execute',side_effect=[self.result([]),self.result([]),self.result([])]) as run:
                with self.assertRaises(ValueError):setup.setup_linux(location=str(location),yes=True)
            self.assertEqual(location.read_bytes(),b'existing file')
            self.assertFalse(any('create' in c.args[0] for c in run.call_args_list))

    @posix_only
    def test_fresh_shell_readme_install_makes_uv_available_without_new_login(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);home=root/'home';home.mkdir();bin=root/'bin';bin.mkdir()
            # Synthetic downloader emits a fixture installer; no network/install occurs.
            installer='test "$UV_NO_MODIFY_PATH" = 1; mkdir -p "$UV_INSTALL_DIR"; printf \'#!/bin/sh\\nexit 0\\n\' > "$UV_INSTALL_DIR/uv"; chmod +x "$UV_INSTALL_DIR/uv"'
            import shlex
            curl=bin/'curl';curl.write_text('#!/bin/sh\nprintf %s '+shlex.quote(installer)+'\n');curl.chmod(0o700)
            readme=(Path(__file__).parents[1]/'README.md').read_text(encoding='utf-8').split('## Isolated trials\n',1)[1].split('```sh\n',1)[1].split('```',1)[0]
            commands=readme.splitlines();last=commands.index('"$TOOLS/bin/uv" venv --python 3.12 .venv')
            result=subprocess.run(['sh','-e','-c','\n'.join(commands[:last+1])],cwd=root,env={**os.environ,'HOME':str(home),'PATH':str(bin)+':/usr/bin:/bin'},capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((root/'.scratch/controller-tools/bin/uv').is_file())
            self.assertFalse((home/'.local').exists())

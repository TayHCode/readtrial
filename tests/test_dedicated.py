"""Dedicated macOS account boundary: reject ambiguous privilege and ownership."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from readtrial.cli import parser
from readtrial.targets import Limits, Target, TargetError
from readtrial.process import Result


class DedicatedTests(unittest.TestCase):
    def account(self):
        return dict(user='trialuser', uid=502, gid=510, groups=['trialuser'],
                    home='/Users/' + 'trialuser', home_owned=True, private_group=True,
                    owner_homes=['/Users/' + 'example-user'], owner_reads_refused=True,
                    sudo='denied', ssh=True)

    def test_explicit_mode_persists_in_cli(self):
        args = parser().parse_args(['run', '.', '--target', 'ssh:trialuser' + '@' + 'example.invalid',
                                  '--dedicated-account', '--trusted-target',
                                  '--agent-command', 'fake', '--out', 'unused'])
        self.assertTrue(args.dedicated_account)

    def test_standalone_sudo_audit_help_imports_no_controller(self):
        script=Path(__file__).parents[1]/'readtrial/account.py'
        result=subprocess.run([sys.executable,'-B',str(script),'--help'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--test-user',result.stdout)

    def test_reject_unsafe_accounts_and_ambiguous_sudo(self):
        from readtrial.account import validate_account
        validate_account(self.account(), 'trialuser')
        for key, value in [('uid', 0), ('user', 'other'), ('gid', 20),
                           ('groups', ['trialuser', 'admin']), ('groups', ['wheel']),
                           ('private_group', False), ('owner_reads_refused', False),
                           ('sudo', 'password-required'), ('sudo', 'allowed'),
                           ('home_owned', False), ('ssh', False), ('owner_homes', [])]:
            account = self.account(); account[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_account(account, 'trialuser')

    def test_preflight_failure_prevents_bundle_upload_and_creation(self):
        target = Target('ssh:trialuser' + '@' + 'example.invalid', Limits(), lambda *a: None,
                        trusted_target=True, dedicated_account=True)
        with patch.object(target, 'probe_platform', return_value='Darwin'), \
             patch('readtrial.account.AccountSSH.before', side_effect=TargetError('sudo unknown')), \
             patch('readtrial.trusted.TrustedSSH.prepare') as upload:
            with self.assertRaisesRegex(TargetError, 'sudo unknown'):
                target.prepare(Path('unused'))
        upload.assert_not_called(); self.assertFalse(target.created)

    def test_dedicated_mode_rejects_other_platforms(self):
        target = Target('ssh:trialuser' + '@' + 'example.invalid', Limits(), lambda *a: None,
                        trusted_target=True, dedicated_account=True)
        with patch.object(target, 'probe_platform', return_value='Linux'):
            with self.assertRaisesRegex(TargetError, 'macOS'):
                target.prepare(Path('unused'))

    def test_cleanup_requires_journal_and_exact_owned_path(self):
        from readtrial.account import AccountSSH
        target = Target('ssh:trialuser' + '@' + 'example.invalid', Limits(), lambda *a: None,
                        trusted_target=True, dedicated_account=True)
        target.base = '/Users/' + 'trialuser/' + target.id
        account = AccountSSH(target)
        account.observed = self.account()
        with self.assertRaisesRegex(TargetError, 'journal'):
            account.authorize_cleanup()

    def test_pid_reuse_never_killed(self):
        from readtrial.account import verified_process
        saved = {'pid': 123, 'uid': 502, 'pgid': 123, 'start': 'Mon Oct 12 01:02:03 2026'}
        self.assertTrue(verified_process(saved, dict(saved), 502))
        for key, value in [('start', 'Mon Oct 12 01:02:04 2026'), ('uid', 501), ('pgid', 5)]:
            current = dict(saved); current[key] = value
            self.assertFalse(verified_process(saved, current, 502))

    def test_doctor_prints_exact_provisioning_steps_even_when_ready(self):
        from readtrial import setup
        with patch('readtrial.account.local_probe', return_value=self.account()):
            report = setup.doctor('Darwin', test_user='trialuser')
        self.assertTrue(report['ready'])
        instructions = report['instructions']
        for word in ['sysadminctl', 'dscl', 'dseditgroup', 'authorized_keys', 'attest-sudo', 'ssh']:
            self.assertIn(word, instructions)

    def test_ambiguous_sudo_fails_doctor(self):
        from readtrial import setup
        account = self.account(); account['sudo'] = 'password-required'
        with patch('readtrial.account.local_probe', return_value=account):
            report = setup.doctor('Darwin', test_user='trialuser')
        self.assertFalse(report['ready'])
        missing = [row['check'] for row in report['checks'] if not row['ready']]
        self.assertEqual(missing, ['Sudo policy denies this account'])
        self.assertTrue(next(row['ready'] for row in report['checks'] if row['check'] == 'SSH session connected'))

    def test_home_manifest_detects_native_home_writes_without_removing_them(self):
        from readtrial.account import home_manifest
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); fixture = root / 'fixture'; fixture.write_text('before')
            before = home_manifest(str(root))
            fixture.write_text('after')
            self.assertNotEqual(before, home_manifest(str(root)))
            self.assertEqual(fixture.read_text(), 'after')

    def test_manifest_never_follows_links_into_owner_home(self):
        from readtrial.account import home_manifest
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); outside = root / 'outside'; outside.mkdir()
            home = root / 'home'; home.mkdir(); (home / 'link').symlink_to(outside)
            before = home_manifest(str(home)); (outside / 'private').write_text('private')
            self.assertEqual(before, home_manifest(str(home)))

    def fixture_root(self, folder):
        root = Path(folder) / ('readtrial-' + 'f' * 32)
        root.mkdir(); (root / 'control').mkdir()
        info = root.stat()
        return root, dict(base=str(root), id=root.name,
                          root_identity=dict(device=info.st_dev, inode=info.st_ino), processes=[])

    def test_cleanup_refuses_replaced_root_and_preserves_unfamiliar_file(self):
        from readtrial.account import remote_action
        with tempfile.TemporaryDirectory() as folder:
            root, data = self.fixture_root(folder)
            fixture = root / 'unfamiliar'; fixture.write_text('preserve')
            data['root_identity']['inode'] += 1
            with patch('readtrial.account.pwd.getpwuid', return_value=SimpleNamespace(pw_dir=folder)):
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    remote_action('cleanup', data)
            self.assertEqual(fixture.read_text(), 'preserve')

    def test_cleanup_never_signals_unjournaled_process_record(self):
        from readtrial.account import remote_action
        with tempfile.TemporaryDirectory() as folder:
            root, data = self.fixture_root(folder)
            (root / 'control/1.process.json').write_text(json.dumps({'pid': 123}))
            with patch('readtrial.account.pwd.getpwuid', return_value=SimpleNamespace(pw_dir=folder)), \
                 patch('readtrial.account.kill_verified') as kill:
                with self.assertRaisesRegex(ValueError, 'controller ownership'):
                    remote_action('cleanup', data)
            kill.assert_not_called(); self.assertTrue(root.exists())

    def test_cleanup_handles_more_than_nine_journaled_commands(self):
        from readtrial.account import remote_action
        with tempfile.TemporaryDirectory() as folder:
            root,data=self.fixture_root(folder)
            for number in range(1,12):
                saved={'pid':number,'uid':os.getuid(),'pgid':number,'start':'saved'}
                data['processes'].append(saved)
                (root / ('control/'+str(number)+'.process.json')).write_text(json.dumps(saved))
            with patch('readtrial.account.pwd.getpwuid',return_value=SimpleNamespace(pw_dir=folder)), \
                 patch('readtrial.account.kill_verified'), patch('readtrial.account.time.sleep'):
                remote_action('cleanup',data)
            self.assertFalse(root.exists())

    @unittest.skipUnless(hasattr(os, 'setxattr'), 'host Python extended metadata API unavailable')
    def test_manifest_detects_extended_metadata_changes(self):
        from readtrial.account import home_manifest
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); fixture=root/'fixture'; fixture.write_text('same content')
            before=home_manifest(str(root))
            os.setxattr(fixture,'user.readtrial_test',b'synthetic')
            self.assertNotEqual(before,home_manifest(str(root)))

    def test_reused_pid_refuses_signal(self):
        from readtrial.account import kill_verified
        saved = dict(pid=123, pgid=123, uid=os.getuid(), start='before')
        with patch('readtrial.account.process_identity', return_value={**saved, 'start': 'after'}), \
             patch('readtrial.account.os.killpg') as kill:
            with self.assertRaisesRegex(ValueError, 'reused PID'):
                kill_verified(saved)
        kill.assert_not_called()

    def test_transport_journals_identity_before_releasing_command(self):
        from readtrial.account import AccountSSH
        with tempfile.TemporaryDirectory() as folder:
            target = Target('ssh:trialuser' + '@' + 'example.invalid', Limits(), lambda *a: None,
                            trusted_target=True, dedicated_account=True)
            target.base='/Users/' + 'trialuser/'+target.id
            target.guard_directory=Path(folder); target.journal=SimpleNamespace(value={
                'resources': [{'kind':'target-path','identity':target.base}]})
            account=AccountSSH(target); account.observed=self.account(); account.root_identity={'inode':1,'device':2}
            saved={'pid':123,'pgid':123,'uid':502,'start':'recorded'}
            def request(action, data, **kwargs):
                if action == 'launch': return saved
                self.assertEqual(json.loads((Path(folder)/'account-processes.json').read_text())['processes'],[saved])
                self.assertEqual(data['process'],saved)
                return {'exit_code':0,'output':'ok','seconds':0}
            with patch.object(account,'request',side_effect=request):
                account.transport('echo ok', 1, guard=False)

    def test_attestation_refuses_allowed_policy_without_writing(self):
        from readtrial.account import attest_sudo
        with patch('readtrial.account.os.getuid',return_value=0), \
             patch('readtrial.account.pwd.getpwnam',return_value=SimpleNamespace(pw_uid=502)), \
             patch('readtrial.account.output',side_effect=[(0,'trialuser'),(0,'(ALL) ALL')]), \
             patch('readtrial.account.atomic_record') as write:
            with self.assertRaisesRegex(ValueError,'denial not established'):
                attest_sudo('trialuser',yes=True)
        write.assert_not_called()

    def test_macos_administrator_list_denial_can_exit_zero(self):
        from readtrial.account import attest_sudo
        account=SimpleNamespace(pw_uid=502,pw_gid=510,pw_dir='/Users/' + 'trialuser')
        with patch('readtrial.account.os.getuid',return_value=0), \
             patch('readtrial.account.pwd.getpwnam',return_value=account), \
             patch('readtrial.account.output',side_effect=[(0,'trialuser'),(0,'User trialuser is not allowed to run sudo on example.\n')]), \
             patch('readtrial.account.atomic_record') as write, patch('readtrial.account.os.chmod'):
            self.assertEqual(attest_sudo('trialuser',yes=True),0)
        self.assertEqual(write.call_args.args[1]['sudo'],'denied')

"""Synthetic lifecycle contracts; no repository code or shared resources."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from readtrial import isolation
from readtrial.installers import environment
from readtrial.process import Result
from readtrial.targets import Limits, Target, TargetError
from readtrial.worker import initialise_workspace, policy, sandbox
from readtrial.trial import run_target
from test_trial import FakeAgent, FakeTarget


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_owned_environment_and_second_trial_empty(self):
        for number in range(2):
            root = self.root / str(number)
            root.mkdir(mode=0o700)
            env = environment(str(root))
            initialise_workspace(root)
            for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                        'XDG_STATE_HOME', 'TMPDIR', 'TMP', 'TEMP', 'UV_INSTALL_DIR',
                        'UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR', 'UV_TOOL_DIR'):
                path = Path(env[key])
                self.assertTrue(path.is_relative_to(root), key)
                self.assertEqual(sorted(child.name for child in path.iterdir()),
                                 ['cache', 'config', 'data', 'state'] if key == 'HOME' else [], key)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            Path(env['XDG_STATE_HOME'], 'sentinel').write_text('state')
        self.assertNotEqual(environment(str(self.root / '0'))['HOME'], environment(str(self.root / '1'))['HOME'])

    def test_environment_refuses_linked_root_without_writes(self):
        other = self.root / 'other'
        other.mkdir()
        link = self.root / 'linked'
        link.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            isolation.make_environment(link)
        self.assertEqual(list(other.iterdir()), [])

    def test_sandbox_redirects_every_environment_root(self):
        argv = sandbox('/control-source', '/bundle-source', 64)
        for key, value in environment('/workspace').items():
            index = argv.index(key)
            self.assertEqual(argv[index - 1], '--setenv')
            self.assertEqual(argv[index + 1], value)

    def test_uploaded_worker_import_and_environment_initialization(self):
        control = self.root / 'control'
        control.mkdir()
        workspace = self.root / 'workspace'
        workspace.mkdir(mode=0o700)
        source = Path(__file__).parents[1] / 'readtrial'
        for name in ('worker.py', 'process.py', 'installers.py', 'shell.py', 'isolation.py'):
            (control / name).write_bytes((source / name).read_bytes())
        script = 'import sys;sys.path.insert(0,sys.argv[1]);import worker;from pathlib import Path;worker.initialise_workspace(Path(sys.argv[2]));print(worker.installers.environment(sys.argv[2])["XDG_STATE_HOME"])'
        result = subprocess.run([sys.executable, '-I', '-c', script, str(control), str(workspace)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(workspace / 'home/state'))
        self.assertEqual(list((workspace / 'home/state').iterdir()), [])

    def test_manifest_exact_content_mode_absence_and_symlinks(self):
        protected = self.root / 'protected'
        protected.mkdir()
        sentinel = protected / 'sentinel'
        sentinel.write_bytes(b'alpha')
        missing = self.root / 'missing'
        before = isolation.manifest([protected, missing])
        self.assertEqual(before, isolation.manifest([protected, missing]))
        sentinel.write_bytes(b'omega')
        self.assertNotEqual(before, isolation.manifest([protected, missing]))
        sentinel.write_bytes(b'alpha')
        sentinel.chmod(0o600)
        self.assertNotEqual(before, isolation.manifest([protected, missing]))
        missing.mkdir()
        self.assertNotEqual(before, isolation.manifest([protected, missing]))
        (protected / 'escape').symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            isolation.manifest([protected])

    def test_manifest_rejects_special_unreadable_and_parent_link(self):
        pipe = self.root / 'pipe'
        os.mkfifo(pipe)
        with self.assertRaises(ValueError):
            isolation.manifest([pipe])
        sentinel = self.root / 'sentinel'
        sentinel.write_text('data')
        with patch('readtrial.isolation.os.open', side_effect=PermissionError('unreadable')):
            with self.assertRaises(PermissionError):
                isolation.manifest([sentinel])
        (self.root / 'alias').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            isolation.manifest([self.root / 'alias' / 'sentinel'])

    def test_manifest_bound_and_internal_link_identity(self):
        root = self.root / 'protected'
        root.mkdir()
        (root / 'one').write_text('1')
        (root / 'two').symlink_to('one')
        before = isolation.manifest([root])
        self.assertEqual(before['0:two']['target'], 'one')
        with patch('readtrial.isolation.MAX_ENTRIES', 2), self.assertRaises(ValueError):
            isolation.manifest([root])
        with patch('readtrial.isolation.MAX_BYTES', 0), self.assertRaises(ValueError):
            isolation.manifest([root])

    def test_protected_extended_metadata_changes_are_detected(self):
        sentinel = self.root / 'sentinel'
        sentinel.write_text('same content')
        before = isolation.manifest([sentinel])
        self.assertIn('xattrs', before['0:.'])
        os.setxattr(sentinel, 'user.readtrial-test', b'metadata')
        after = isolation.manifest([sentinel])
        self.assertNotEqual(before['0:.']['xattrs'], after['0:.']['xattrs'])


    def test_journal_durable_write_ahead_and_readonly_exact_discovery(self):
        directory = self.root / 'journals'
        resource = self.root / ('readtrial-' + 'a' * 32)
        unrelated = self.root / ('readtrial-' + 'b' * 32)
        journal = isolation.Journal(directory, 'readtrial-' + 'a' * 32, 'local-container')
        with patch('readtrial.isolation.os.fsync', wraps=os.fsync) as synced:
            journal.intent('local-path', str(resource))
            self.assertGreaterEqual(synced.call_count, 2)
        self.assertFalse(resource.exists())
        resource.mkdir()
        unrelated.mkdir()
        before = journal.path.read_bytes()
        found = isolation.leftovers(directory)
        self.assertEqual(found[0]['identity'], str(resource))
        self.assertEqual(found[0]['status'], 'present')
        self.assertEqual(journal.path.read_bytes(), before)
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(journal.path.stat().st_mode), 0o600)

    def test_malformed_journal_and_links_rejected(self):
        directory = self.root / 'journals'
        journal = isolation.Journal(directory, 'readtrial-' + 'c' * 32, 'lxc')
        journal.intent('lxc', journal.id)
        value = json.loads(journal.path.read_text())
        value['resources'][0]['identity'] = 'unrelated'
        journal.path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            isolation.leftovers(directory)
        journal.path.unlink()
        journal.path.symlink_to(self.root / 'outside')
        with self.assertRaises(ValueError):
            isolation.leftovers(directory)

    def test_lxc_discovery_uses_exact_name_and_checks_tag(self):
        directory = self.root / 'journals'
        journal = isolation.Journal(directory, 'readtrial-' + 'd' * 32, 'lxc')
        journal.intent('lxc', journal.id)
        output = [{'name': journal.id + '-unrelated', 'config': {}},
                  {'name': journal.id, 'config': {'user.readtrial.id': journal.id}}]
        with patch('readtrial.isolation.execute', return_value=Result(0, json.dumps(output), 0)) as command:
            self.assertEqual(isolation.leftovers(directory)[0]['status'], 'present')
        self.assertEqual(command.call_args.args[0], ['lxc', 'list', journal.id, '--format', 'json'])
        output[1]['config'] = {}
        with patch('readtrial.isolation.execute', return_value=Result(0, json.dumps(output), 0)):
            self.assertEqual(isolation.leftovers(directory)[0]['status'], 'ownership-unverified')

    def target(self):
        target = Target('lxc:cached', Limits(), lambda *args: None, pool='test-pool')
        bundle = self.root / 'source.bundle'
        bundle.write_bytes(b'synthetic')
        target.configure_isolation(self.root / 'evidence', {}, bundle)
        return target, bundle

    def test_quota_unenforced_fails_before_creation(self):
        target, bundle = self.target()
        outputs = [Result(0, json.dumps({'driver': 'dir', 'config': {'source': str(self.root)}}), 0),
                   Result(0, json.dumps({'filesystems': [{'fstype': 'ext4', 'options': 'rw,relatime'}]}), 0)]
        with patch('readtrial.targets.execute', side_effect=outputs) as command:
            with self.assertRaisesRegex(TargetError, 'root quota'):
                target.prepare(bundle)
        self.assertFalse(target.created)
        self.assertFalse(any('init' in call.args[0] for call in command.call_args_list))
        self.assertEqual(target.capabilities['effective']['root_disk']['status'], 'unsupported')

    def test_unknown_or_unreadable_quota_rejects_without_mutation(self):
        for result in (Result(0, '{}', 0), Result(0, '{"driver":"unknown"}', 0),
                       Result(0, '{"driver":"zfs"}', 0, truncated=True)):
            target, bundle = self.target()
            with patch('readtrial.targets.execute', return_value=result):
                with self.assertRaises(TargetError):
                    target.prepare(bundle)
            self.assertFalse(target.created)
            target.close()
            self.root.joinpath('evidence').rename(self.root / target.id)

    def test_tag_mismatch_preserves_instance_and_still_checks_host(self):
        target, bundle = self.target()
        target.guard_before()
        target.journal.intent('lxc', target.id)
        target.created = True
        inventory = [{'name': target.id, 'config': {'user.readtrial.id': 'other'}}]
        with patch('readtrial.targets.execute', return_value=Result(0, json.dumps(inventory), 0)) as command:
            with self.assertRaisesRegex(TargetError, 'tag mismatch'):
                target.close()
        self.assertFalse(any('delete' in call.args[0] for call in command.call_args_list))
        self.assertEqual(target.capabilities['guards']['status'], 'unchanged')
        self.assertEqual(json.loads(target.journal.path.read_text())['phase'], 'incomplete')

    def test_absence_proves_guest_paths_absent_without_prefix_deletion(self):
        journal = isolation.Journal(self.root / 'journals', 'readtrial-' + 'e' * 32, 'lxc')
        journal.intent('lxc', journal.id)
        journal.intent('target-path', '/tmp/' + journal.id + '/home')
        with patch('readtrial.isolation.execute', return_value=Result(0, '[]', 0)) as command:
            found = isolation.leftovers(journal.path.parent)
        self.assertEqual([item['status'] for item in found], ['absent', 'absent'])
        self.assertEqual(command.call_count, 1)

    def test_interrupted_trial_cleanup_and_capability_report(self):
        class Interrupted(FakeTarget):
            prepare_error = ''
            close_error = ''
            capabilities = {'guards': {'status': 'unchanged'}, 'cleanup': {'status': 'confirmed'}}
            def prepare(self, bundle):
                raise KeyboardInterrupt
        config = {'repo': '.', 'agent_command': 'fake', 'checks': [{'command': 'verify', 'match': None}],
                  'limits': asdict(Limits())}
        before = Interrupted.closed
        result = run_target(config, 'fake', self.root / 'run', Path('unused'), 'fake',
                            target_factory=Interrupted, agent_factory=FakeAgent)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['error'], 'interrupted')
        self.assertEqual(Interrupted.closed, before + 1)

    def test_failed_setup_journal_precedes_creation(self):
        target, bundle = self.target()
        def manage(argv, **kwargs):
            if 'init' in argv:
                value = json.loads(target.journal.path.read_text())
                self.assertEqual(value['resources'][0]['identity'], target.id)
                self.assertEqual(value['phase'], 'creating')
                raise TargetError('synthetic init failure')
            return Result(0, '[]', 0)
        with patch.object(target, 'root_quota_preflight'), patch.object(target, 'management', side_effect=manage):
            with self.assertRaisesRegex(TargetError, 'init failure'):
                target.prepare(bundle)

    def test_guard_mismatch_and_unreadable_postcheck_fail_without_restore(self):
        for failure in ('mismatch', 'unreadable'):
            with self.subTest(failure=failure):
                sentinel = self.root / failure
                sentinel.write_text('before')
                target = Target('lxc:cached', Limits(), lambda *args: None)
                bundle = self.root / (failure + '.bundle')
                bundle.write_bytes(b'bundle')
                target.configure_isolation(self.root / (failure + '-evidence'), {'protected_paths': [str(sentinel)]}, bundle)
                target.guard_before()
                if failure == 'mismatch':
                    sentinel.write_text('after')
                    with self.assertRaisesRegex(TargetError, 'preservation'):
                        target.close()
                    self.assertEqual(sentinel.read_text(), 'after')
                else:
                    with patch('readtrial.isolation.manifest', side_effect=PermissionError('unreadable')):
                        with self.assertRaisesRegex(TargetError, 'preservation'):
                            target.close()
                self.assertEqual(target.capabilities['guards']['status'], 'failed')

    def test_cleanup_truncation_cannot_certify_absence(self):
        target, bundle = self.target()
        target.guard_before()
        target.created = True
        target.journal.intent('lxc', target.id)
        with patch('readtrial.targets.execute', return_value=Result(0, '[]', 0, truncated=True)):
            with self.assertRaises(TargetError):
                target.close()
        self.assertNotEqual(target.capabilities['cleanup']['status'], 'confirmed')

    def test_portable_policy_and_operator_denied_endpoint(self):
        for command in ('chromium --remote-debugging-port=45678', 'cat .config/browser/state', 'cat /dev/video0'):
            self.assertTrue(policy(command), command)
        self.assertEqual(policy('echo 45678'), '')
        self.assertTrue(policy('curl https://example.invalid:45678/path', denied_endpoints={'example.invalid:45678'}))
        self.assertTrue(policy('curl example.invalid:45678/path', denied_endpoints={'example.invalid:45678'}))
        self.assertEqual(policy('curl https://example.invalid:45678/path'), '')

    def test_guard_failure_overrides_successful_checks_and_is_reported(self):
        class GuardFailure(FakeTarget):
            capabilities = {'requested': {'filesystem': 'confined'}, 'effective': {'filesystem': 'synthetic'},
                            'guards': {'status': 'failed'}, 'cleanup': {'status': 'confirmed'}}
            def close(self):
                raise TargetError('host preservation mismatch')
        FakeAgent.actions = [{'done': True, 'findings': []}]
        FakeTarget.commands = {}
        FakeTarget.metadata = '{}'
        FakeTarget.prepare_error = ''
        config = {'repo': '.', 'agent_command': 'fake', 'checks': [{'command': 'verify', 'match': None}],
                  'limits': asdict(Limits())}
        result = run_target(config, 'fake', self.root / 'run', Path('unused'), 'fake',
                            target_factory=GuardFailure, agent_factory=FakeAgent)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['capabilities']['guards']['status'], 'failed')

    def test_old_saved_run_cannot_bypass_new_isolation_contract(self):
        from readtrial.cli import main
        prior = self.root / 'prior'
        prior.mkdir()
        (prior / 'run.json').write_text(json.dumps({'version': 1, 'config': {}}))
        with self.assertRaises(SystemExit) as outcome:
            main(['rerun', str(prior), '--out', str(self.root / 'new'), '--dry-run'])
        self.assertEqual(outcome.exception.code, 2)

    def test_new_rerun_preserves_guard_paths_and_endpoint_configuration(self):
        import contextlib
        import io
        from readtrial.cli import main
        prior = self.root / 'prior'
        prior.mkdir()
        config = {'isolation_version': 1, 'protected_paths': ['synthetic'],
                  'readonly_inputs': ['immutable'], 'denied_endpoints': ['example.invalid:45678']}
        (prior / 'run.json').write_text(json.dumps({'version': 2, 'config': config}))
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.assertEqual(main(['rerun', str(prior), '--out', str(self.root / 'new'), '--dry-run']), 0)
        plan = json.loads(captured.getvalue().split('\n', 1)[1])
        self.assertEqual(plan['config'], config)
        self.assertFalse((self.root / 'new').exists())


if __name__ == '__main__':
    unittest.main()

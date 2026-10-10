from __future__ import annotations

import base64
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from test_trial import FakeAgent, FakeTarget

from readtrial.cli import parser
from readtrial.process import Result
from readtrial.targets import Limits, Target, TargetError
from readtrial.trial import run_target
from readtrial.trusted import NOTICE, TrustedSSH, inspect, powershell


class TrustedTests(unittest.TestCase):
    def test_opt_in_is_not_default(self):
        args = parser().parse_args(['run', '.', '--target', 'ssh:example', '--agent-command', 'fake', '--out', 'unused'])
        self.assertFalse(args.trusted_target)

    def test_mac_requires_opt_in(self):
        target = Target('ssh:example', Limits(), lambda *a: None)
        with patch('readtrial.targets.execute', return_value=Result(0, 'Darwin', 0)), self.assertRaisesRegex(TargetError, '--trusted-target'):
            target.prepare(Path('unused'))
        self.assertFalse(target.created)

    def test_mac_routes_without_target_python(self):
        target = Target('ssh:example', Limits(), lambda *a: None, trusted_target=True)
        with patch('readtrial.targets.execute', return_value=Result(0, 'Darwin', 0)), patch.object(TrustedSSH, 'prepare') as prepare:
            target.prepare(Path('unused'))
        prepare.assert_called_once()
        self.assertIn('no OS confinement', target.isolation)
        self.assertIsNotNone(target.trusted)

    def test_windows_routes_after_fixed_probe(self):
        target = Target('ssh:example', Limits(), lambda *a: None, trusted_target=True)
        with patch('readtrial.targets.execute', side_effect=[Result(1, 'unknown', 0), Result(0, 'Win32NT', 0)]), patch.object(TrustedSSH, 'prepare'):
            target.prepare(Path('unused'))
        self.assertEqual(target.platform, 'Windows')
        self.assertTrue(target.trusted.windows)

    def test_obvious_breakouts_are_denied_and_logged(self):
        records = []
        target = Target('ssh:example', Limits(), lambda *a: records.append(a), trusted_target=True)
        backend = TrustedSSH(target)
        commands = ['cat ~/.ssh/config', 'echo x > ../outside', 'sudo -n true',
                    'Get-Content "$HOME\\.ssh\\config"', 'Set-Content ..\\outside x',
                    'Start-Process powershell -Verb RunAs', 'Set-Content C:\\outside x',
                    'brew install python', 'winget install Python', 'git config --global x y',
                    'Get-Content "$env:APPDATA\\Google\\Chrome\\Default\\Login Data"',
                    'curl https://unrelated.invalid/', 'mkdir -p /workspace/private', 'touch /opt/outside', 'Stop-Process -Name unrelated']
        with patch('readtrial.trusted.execute') as runner:
            for command in commands:
                result = backend.command(command)
                self.assertEqual(result.exit_code, 126, command)
                self.assertTrue(result.denied)
        runner.assert_not_called()
        self.assertEqual([r[1] for r in records], commands)

    def test_registry_and_repo_urls_allowed(self):
        for command in ['curl https://pypi.org/simple/', 'git clone https://github.com/example/repo', 'python3 -m venv .venv', 'py -3 script.py --help']:
            self.assertEqual(inspect(command, {'github.com'}, repository='https://github.com/example/repo'), '')

    def test_computed_paths_not_claimed_confined(self):
        # This is deliberately allowed: source code can construct arbitrary paths.
        self.assertEqual(inspect("python3 -c 'print(1)'", set()), '')
        self.assertIn('less isolated', NOTICE)

    def test_windows_encoding_does_not_interpolate_ssh_shell(self):
        script = "Write-Output '$() & dangerous'"
        encoded = powershell(script).split()[-1]
        self.assertTrue(base64.b64decode(encoded).decode('utf-16le').endswith(script))

    def test_process_tree_cleanup_scripts(self):
        target = Target('ssh:example', Limits(), lambda *a: None)
        backend = TrustedSSH(target)
        mac = backend.mac_script('(sleep 10 & wait)', 1)
        self.assertIn('set -m', mac)
        self.assertIn('kill -KILL -- -', mac)
        self.assertIn('ps -axo pgid=', mac)
        windows = backend.windows_script('Start-Sleep 10', 1)
        self.assertIn('taskkill.exe /PID $p.Id /T /F', windows)
        self.assertIn('owned process cleanup not confirmed', windows)

    def test_total_budget_expiration_executes_nothing(self):
        target = Target('ssh:example', Limits(), lambda *a: None)
        target.deadline = 0
        with patch('readtrial.trusted.execute') as runner:
            result = TrustedSSH(target).command('echo ok')
        self.assertTrue(result.timed_out)
        runner.assert_not_called()

    def test_transport_failures_keep_evidence(self):
        records = []
        target = Target('ssh:example', Limits(), lambda *a: records.append(a))
        with patch('readtrial.trusted.execute', return_value=Result(3, 'not JSON', 0)), self.assertRaises(TargetError):
            TrustedSSH(target).command('echo ok')
        self.assertEqual(records[0][0], 'transport')

    def test_cleanup_failure_is_error(self):
        target = Target('ssh:example', Limits(), lambda *a: None)
        target.created = True
        with patch('readtrial.trusted.execute', return_value=Result(1, 'failed', 0)), self.assertRaises(TargetError):
            TrustedSSH(target).close()

    def test_report_records_trusted_mode(self):
        class TrustedFake(FakeTarget):
            trusted = True
            platform = 'Windows'
            isolation = 'TRUSTED-TARGET: no OS confinement'
        FakeAgent.actions = [{'done': True, 'findings': []}]
        FakeTarget.prepare_error = FakeTarget.close_error = ''
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / 'result'
            config = {'repo': '.', 'agent_command': 'fake', 'limits': asdict(Limits()), 'checks': [{'command': 'echo ok', 'match': None}], 'trusted_target': True}
            summary = run_target(config, 'ssh:example', dest, Path('unused'), 'fake', target_factory=TrustedFake, agent_factory=FakeAgent)
            self.assertTrue(summary['trusted_mode'])
            self.assertIn(NOTICE, (dest / 'report.md').read_text())
            self.assertTrue(json.loads((dest / 'result.json').read_text())['trusted_mode'])


if __name__ == '__main__':
    unittest.main()

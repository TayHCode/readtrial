from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from test_trial import FakeAgent, FakeTarget, finding

from readtrial.process import Result
from readtrial.protocol import normalize
from readtrial.targets import Limits
from readtrial.trial import Agent, Evidence, run, run_target


class WindowsTarget(FakeTarget):
    platform = 'Windows'
    trusted = True
    isolation = 'TRUSTED-TARGET'

    def command(self, command, **kwargs):
        if 'README.md' in command:
            result = Result(0, '1 ## Quickstart\n2 ```powershell\n3 cd examples && py -3 ../hello.py --name Ada\n4 py -3 hello.py --name Check\n5 ```\n', 0)
            self.log('command', command, result)
            return result
        return super().command(command, **kwargs)


class RoundFourTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        FakeTarget.commands = {}
        FakeTarget.metadata = '{}'
        FakeTarget.prepare_error = FakeTarget.close_error = ''
        FakeTarget.seen = []
        FakeAgent.calls = []
        self.config = {'repo': '.', 'targets': ['ssh:example'], 'agent_command': 'fake',
                       'ssh_linux_ack': True, 'limits': asdict(Limits()), 'checks': [{'command': 'check'}]}

    def tearDown(self):
        self.temp.cleanup()

    def trial(self, target=FakeTarget, **kwargs):
        return run_target(self.config, 'ssh:example', self.root / 'result', Path('unused'), 'fake',
                          target_factory=target, agent_factory=FakeAgent, **kwargs)

    def test_exact_serialized_action_unwraps_one_level(self):
        action = {'command': '{"command":"python3 release_check.py --help","done":false,"findings":[]}'}
        self.assertEqual(normalize(action), {'command': 'python3 release_check.py --help'})
        envelope = json.dumps({'structured_output': action})
        with patch('readtrial.trial.execute', return_value=Result(0, envelope, 0)) as runner:
            result, _ = Agent('claude -p --model claude-haiku-5-5', Limits()).ask({'history': []}, 10, self.root)
        self.assertEqual(result, {'command': 'python3 release_check.py --help'})
        self.assertEqual(runner.call_count, 1)

    def test_double_nested_action_is_refused(self):
        action = {'command': json.dumps({'command': json.dumps({'command': 'echo x'})})}
        with self.assertRaisesRegex(ValueError, 'serialized JSON action'):
            normalize(action)

    def test_runner_corrects_one_malformed_response_and_logs_both(self):
        FakeAgent.actions = [{'command': '{"wrong":"echo x"}'}, {'command': 'echo x'}, {'done': True, 'findings': []}]
        self.assertTrue(self.trial()['verdict'].startswith('PASS'))
        self.assertIn('protocol_correction', FakeAgent.calls[1])
        rows = [json.loads(line) for line in (self.root / 'result/commands.jsonl').read_text().splitlines()]
        self.assertEqual(len([r for r in rows if r['kind'].startswith('agent')]), 3)

    def test_adapter_retry_has_separate_evidence_row(self):
        envelopes = [json.dumps({'structured_output': {'command': '{"wrong":"echo x"}'}}),
                     json.dumps({'structured_output': {'command': 'echo x'}}),
                     json.dumps({'structured_output': {'command': None, 'done': True, 'findings': []}})]
        self.config['agent_command'] = 'claude -p --model claude-haiku-5-5'
        with patch('readtrial.trial.execute', side_effect=[Result(0, value, 0) for value in envelopes]):
            summary = run_target(self.config, 'ssh:example', self.root / 'result', Path('unused'), 'fake', target_factory=FakeTarget)
        self.assertTrue(summary['verdict'].startswith('PASS'))
        rows = [json.loads(line) for line in (self.root / 'result/commands.jsonl').read_text().splitlines()]
        self.assertEqual(len([r for r in rows if r['kind'].startswith('agent')]), 3)
        self.assertIn('PROTOCOL CORRECTION', rows[1]['output'])

    def test_exact_windows_added_cd_is_ignored_with_evidence(self):
        proposed = 'cd examples; if ($?) { py -3 hello.py --name Check }'
        FakeTarget.commands = {proposed: Result(2, 'cannot open hello.py', 0)}
        FakeAgent.actions = [{'command': proposed}, {'done': True, 'findings': []}]
        summary = self.trial(WindowsTarget)
        self.assertEqual(summary['verdict'], 'PASS (partial)')
        self.assertEqual(summary['readme_coverage']['ran'], 1)
        rows = [json.loads(line) for line in (self.root / 'result/commands.jsonl').read_text().splitlines()]
        note = next(r for r in rows if r['kind'] == 'agent-deviation')
        self.assertEqual(note['command'], proposed)
        self.assertIn('ignored added cd', note['output'])
        self.assertNotIn(proposed, FakeTarget.seen)

    def test_arbitrary_agent_failure_is_labelled_agent(self):
        FakeTarget.commands = {'echo invented': Result(2, 'unrecognized arguments: --version', 0)}
        FakeAgent.actions = [{'command': 'echo invented'}, {'done': True, 'findings': []}]
        summary = self.trial(WindowsTarget)
        self.assertIn('agent command 1', summary['verdict_detail'])
        self.assertNotIn('[failed:', summary['verdict_detail'])
        self.assertIn('unrecognized arguments: --version', summary['verdict_detail'])
        self.assertIn('Failed agent command', (self.root / 'result/report.md').read_text())

    def test_missing_checks_never_calls_agent(self):
        self.config['checks'] = []
        FakeAgent.actions = [{'done': True, 'findings': []}]
        self.assertEqual(self.trial()['verdict'], 'INCOMPLETE')
        self.assertEqual(FakeAgent.calls, [])

    def test_missing_local_checks_no_output_or_agent(self):
        self.config['repo'] = str(self.root)
        self.config['checks'] = []
        with patch('readtrial.trial.execute', return_value=Result(1, '', 0)), patch('readtrial.trial.snapshot') as fetch, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(run(self.config, self.root / 'output'), 2)
        self.assertFalse((self.root / 'output').exists())
        fetch.assert_not_called()
        self.assertIn('No success checks declared', output.getvalue())

    def test_linux_notice_consistent(self):
        class LinuxTarget(FakeTarget):
            platform = 'Linux'
            trusted = True
            isolation = 'SSH-LINUX-UNCONFINED'
        FakeAgent.actions = [{'done': True, 'findings': []}]
        summary = self.trial(LinuxTarget)
        self.assertFalse(summary['trusted_mode'])
        self.assertEqual(summary['notice_label'], 'SSH-LINUX-UNCONFINED')
        report = (self.root / 'result/report.md').read_text()
        self.assertIn('SSH-LINUX-UNCONFINED', report)
        self.assertNotIn('TRUSTED-TARGET', report)

    def test_advisory_churn_excluded_from_diff(self):
        previous = [finding('advisory-old')]
        FakeAgent.actions = [{'done': True, 'findings': []}]
        self.trial(previous=previous, previous_records=[])
        changes = json.loads((self.root / 'result/diff.json').read_text())
        self.assertEqual(changes['no_longer_raised'], [])
        self.assertEqual(changes['added'], [])
        self.assertEqual(len(changes['unassessed']), 1)

    def test_evidence_abbreviates_target_temp_and_account(self):
        evidence = Evidence(self.root / 'commands.jsonl')
        windows_path = chr(92).join(['C:', 'Users', 'sample-user', 'AppData', 'Local', 'Temp', 'readtrial-abcdef', 'source.bundle'])
        paths = ['/tmp/readtrial-abcdef.Xyz123/source.bundle', windows_path]
        for path in paths:
            evidence.log('setup', path, Result(0, path, 0))
        evidence.close()
        text = evidence.path.read_text()
        self.assertNotIn('sample-user', text)
        self.assertNotIn('/tmp/', text)
        self.assertIn('<WORKDIR>', text)

    def test_evidence_abbreviates_escaped_windows_output(self):
        evidence = Evidence(self.root / 'commands.jsonl')
        path = (chr(92) * 2).join(['C:', 'Users', 'sample-user', 'AppData', 'Local', 'Temp', 'readtrial-abcdef', 'source.bundle'])
        evidence.log('setup', 'prepare target', Result(0, path, 0))
        evidence.close()
        text = evidence.path.read_text()
        self.assertNotIn('sample-user', text)
        self.assertIn('<WORKDIR>', text)

    def test_missing_flags_name_the_probed_platform(self):
        self.config['ssh_linux_ack'] = False
        for platform, flag in [('Linux', '--i-understand-ssh-linux-is-not-confined'), ('Windows', '--trusted-target'), ('Darwin', '--trusted-target')]:
            with self.subTest(platform=platform), patch('readtrial.targets.Target.probe_platform', return_value=platform), patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(run(self.config, self.root / 'output'), 2)
            self.assertIn('ssh:example (' + platform + ') needs ' + flag, output.getvalue())
            self.assertFalse((self.root / 'output').exists())


if __name__ == '__main__':
    unittest.main()

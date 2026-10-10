from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from test_trial import FakeAgent, FakeTarget, finding

from readtrial.coverage import coverage, declared_steps
from readtrial.identity import canonical_findings, normalized_command
from readtrial.process import Result
from readtrial.targets import Limits, Target
from readtrial.trial import Agent, AgentError, run, run_target
from readtrial.trusted import TrustedSSH

COMMAND = 'py -3 hello.py --version'


class QuickstartTarget(FakeTarget):
    platform = 'Windows'

    def command(self, command, **kwargs):
        if 'README.md' in command:
            result = Result(0, '1 ## Quickstart\n2 ```powershell\n3 py -3 hello.py --help\n4 ' + COMMAND + '\n5 ```\n', 0)
            self.log('command', command, result)
            return result
        return super().command(command, **kwargs)


class RoundThreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        FakeTarget.commands = {COMMAND: Result(2, 'unsupported option', 0)}
        FakeTarget.prepare_error = FakeTarget.close_error = ''
        FakeTarget.seen = []
        FakeAgent.calls = []
        self.config = {'repo': '.', 'targets': ['ssh:example'], 'agent_command': 'fake',
                       'trusted_target': True, 'limits': asdict(Limits()), 'checks': [{'command': 'check'}]}

    def tearDown(self):
        self.tmp.cleanup()

    def trial(self, name, identity=None, **kwargs):
        items = [finding(identity) | {'command': COMMAND}] if identity else []
        FakeAgent.actions = [{'command': COMMAND}, {'done': True, 'findings': items}]
        return run_target(self.config, 'ssh:example', self.root / name, Path('unused'), 'fake',
                          target_factory=QuickstartTarget, agent_factory=FakeAgent, **kwargs)

    def saved(self, name):
        folder = self.root / name
        return {'previous': json.loads((folder / 'findings.json').read_text()),
                'previous_records': [json.loads(line) for line in (folder / 'commands.jsonl').read_text().splitlines()]}

    def changes(self, name):
        return json.loads((self.root / name / 'diff.json').read_text())

    def test_two_unchanged_reruns_ignore_model_chosen_ids(self):
        self.trial('first', 'model-first')
        self.trial('second', 'model-second', **self.saved('first'))
        self.trial('third', 'model-third', **self.saved('second'))
        for name in ('second', 'third'):
            changes = self.changes(name)
            self.assertEqual(changes['added'], [])
            self.assertEqual(changes['no_longer_raised'], [])
            self.assertEqual(changes['resolved'], [])
            self.assertEqual(len(changes['unchanged']), 1)

    def test_legacy_model_id_is_matched_using_previous_evidence(self):
        self.trial('first', 'old-model-name')
        prior = self.saved('first')
        prior['previous'][0]['id'] = 'legacy-model-name'
        self.trial('second', 'different-name', **prior)
        self.assertEqual(self.changes('second')['added'], [])
        self.assertEqual(self.changes('second')['no_longer_raised'], [])

    def test_identity_key_separates_exit_segment_and_machine_class(self):
        item = finding('model') | {'command': COMMAND}
        entry = {'phase': 'trial', 'kind': 'command', 'command': COMMAND, **Result(2, '', 0).json()}
        base = canonical_findings([item], [entry], 'ssh:Windows', True)[0]['id']
        for changed, machine in ((entry | {'exit_code': 3}, 'ssh:Windows'),
                                 (entry, 'ssh:Linux'), (entry, 'lxc:Windows'),
                                 (entry | {'steps': [{'segment': 2, 'command': COMMAND, 'exit_code': 2}]}, 'ssh:Windows')):
            self.assertNotEqual(canonical_findings([item], [changed], machine, True)[0]['id'], base)
        self.assertEqual(canonical_findings([item | {'id': 'other', 'actual': 'different wording'}],
                                           [entry], 'ssh:Windows', True)[0]['id'], base)

    def test_separator_normalization_preserves_quoted_argument_meaning(self):
        self.assertEqual(normalized_command('  echo "a  b";echo x  '), 'echo "a  b" ; echo x')
        self.assertNotEqual(normalized_command('echo "a  b"'), normalized_command('echo "a b"'))

    def test_unverified_advisory_id_also_ignores_model_text(self):
        first = canonical_findings([finding('first')], [], 'ssh:Windows', True)[0]
        second = canonical_findings([finding('second') | {'actual': 'another opinion'}], [], 'ssh:Windows', True)[0]
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(first['id'].startswith('advisory-'))

    def test_segment_finding_uses_full_proposal_to_preserve_cwd(self):
        item = finding('model') | {'command': COMMAND}
        def entry(folder):
            return {'phase': 'trial', 'kind': 'command', 'command': f'cd {folder}; {COMMAND}',
                    **Result(2, '', 0, steps=[{'segment': 2, 'command': COMMAND, 'exit_code': 2}]).json()}
        first = canonical_findings([item], [entry('one')], 'ssh:Windows', True)[0]
        second = canonical_findings([item], [entry('two')], 'ssh:Windows', True)[0]
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['command'], entry('one')['command'])
        self.assertTrue(canonical_findings([item], [entry('one'), entry('two')], 'ssh:Windows', True)[0]['id'].startswith('advisory-'))

    def test_failed_command_omitted_by_agent_is_only_no_longer_raised(self):
        self.trial('first', 'model-first')
        self.trial('second', **self.saved('first'))
        self.assertEqual(self.changes('second')['resolved'], [])
        self.assertEqual(len(self.changes('second')['no_longer_raised']), 1)

    def test_real_fix_resolves_with_successful_command_evidence(self):
        self.trial('first', 'model-first')
        FakeTarget.commands[COMMAND] = Result(0, 'version 1', 0)
        self.trial('second', **self.saved('first'))
        self.assertEqual(len(self.changes('second')['resolved']), 1)
        self.assertEqual(self.changes('second')['no_longer_raised'], [])

    def test_incomplete_rerun_never_claims_progress(self):
        self.trial('first', 'model-first')
        prior = self.saved('first')
        for name, actions in (('before', [AgentError('agent-protocol-error: malformed', Result(2, '', 0))]),
                              ('after', [{'command': COMMAND}, AgentError('agent-protocol-error: malformed', Result(2, '', 0))])):
            with self.subTest(name=name):
                FakeAgent.actions = actions
                result = run_target(self.config, 'ssh:example', self.root / name, Path('unused'), 'fake',
                                    target_factory=QuickstartTarget, agent_factory=FakeAgent, **prior)
                self.assertEqual(result['verdict'], 'INCOMPLETE')
                changes = self.changes(name)
                self.assertEqual(changes['no_longer_raised'], [])
                self.assertEqual(changes['resolved'], [])
                self.assertEqual(changes['status'], 'incomplete')
                self.assertEqual(len(changes['unassessed']), 1)

    def test_fail_stdout_names_readme_step_command_and_exit(self):
        result = self.trial('first')
        with patch('readtrial.trial.snapshot', return_value=(Path('unused'), 'fake')), \
                patch('readtrial.trial.run_target', return_value=result), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(run(self.config, self.root / 'out'), 1)
        self.assertIn('README step 2', output.getvalue())
        self.assertIn(COMMAND, output.getvalue())
        self.assertIn('exit 2', output.getvalue())

    def test_compound_failure_stdout_uses_segment_exit_even_if_tail_succeeded(self):
        compound = COMMAND + '; echo done'
        FakeTarget.commands[compound] = Result(0, 'done', 0, steps=[
            {'segment': 1, 'command': COMMAND, 'exit_code': 2},
            {'segment': 2, 'command': 'echo done', 'exit_code': 0}])
        FakeAgent.actions = [{'command': compound}, {'done': True, 'findings': []}]
        result = run_target(self.config, 'ssh:example', self.root / 'first', Path('unused'), 'fake',
                            target_factory=QuickstartTarget, agent_factory=FakeAgent)
        self.assertIn('segment 1', result['verdict_detail'])
        self.assertIn('exit 2', result['verdict_detail'])
        self.assertIn(COMMAND, result['verdict_detail'])

    def test_failed_independent_check_stdout_names_command_and_exit(self):
        FakeTarget.commands['check'] = Result(9, 'failed', 0)
        FakeAgent.actions = [{'done': True, 'findings': []}]
        result = run_target(self.config, 'ssh:example', self.root / 'first', Path('unused'), 'fake',
                            target_factory=QuickstartTarget, agent_factory=FakeAgent)
        self.assertIn('declared check 1', result['verdict_detail'])
        self.assertIn('check', result['verdict_detail'])
        self.assertIn('exit 9', result['verdict_detail'])

    def test_quickstart_own_posix_block_survives_windows_subsection(self):
        doc = '## Quickstart\n```sh\npython3 hello.py --help\n```\n### Windows\n```powershell\npy -3 hello.py --help\n```\n'
        for platform in ('Linux', 'Darwin'):
            self.assertEqual([s['command'] for s in declared_steps(doc, platform)], ['python3 hello.py --help'])
        self.assertEqual([s['command'] for s in declared_steps(doc, 'Windows')], ['py -3 hello.py --help'])

    def test_other_platform_subsection_is_never_used_as_fallback(self):
        doc = '## Quickstart\n### Windows\n```sh\necho windows-only\n```\n'
        self.assertEqual(declared_steps(doc, 'Linux'), [])

    def test_windows_alternative_is_successful_but_not_exact_declared_coverage(self):
        doc = '## Quickstart\n```powershell\npy -3 hello.py --help\ncd examples && py -3 ../hello.py --name Ada\n```\n'
        entries = [{'command': 'py -3 hello.py --help', **Result(0, '', 0).json()},
                   {'command': 'cd examples; py -3 ../hello.py --name Ada', **Result(0, '', 0, steps=[
                       {'segment': 1, 'command': 'cd examples', 'exit_code': 0},
                       {'segment': 2, 'command': 'py -3 ../hello.py --name Ada', 'exit_code': 0}]).json()}]
        counts = coverage(declared_steps(doc, 'Windows'), entries, True)
        self.assertEqual((counts['declared'], counts['ran'], counts['succeeded']), (2, 1, 1))
        self.assertFalse(counts['complete'])

    def test_protocol_error_excludes_raw_cli_envelope(self):
        envelope = json.dumps({'structured_output': {'command': '{"wrong":"echo x"}'},
                               'session_id': 'session-detail', 'usage': {'input_tokens': 123},
                               'total_cost_usd': 1.25})
        with patch('readtrial.trial.execute', return_value=Result(0, envelope, 0)), self.assertRaises(AgentError) as caught:
            Agent('claude -p', Limits()).ask({'history': [{'command': 'prior'}]}, 10, self.root)
        for detail in ('session_id', 'session-detail', 'input_tokens', 'total_cost_usd'):
            self.assertNotIn(detail, str(caught.exception))
            self.assertNotIn(detail, caught.exception.result.output)
        self.assertLess(len(str(caught.exception)), 1300)
        self.assertIn('serialized JSON action', str(caught.exception))

    def test_spacing_only_rewrite_is_metadata_for_one_execution(self):
        records = []
        target = Target('ssh:example', Limits(), lambda *args: records.append(args))
        target.platform = 'Windows'
        target.base = str(self.root)
        result = Result(0, 'hello', 0)
        with patch('readtrial.trusted.execute', return_value=Result(0, json.dumps(result.json()), 0)) as call:
            TrustedSSH(target).command('cd examples; py -3 hello.py')
        self.assertEqual(call.call_count, 1)
        commands = [r for r in records if r[0] == 'command']
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][1], 'cd examples; py -3 hello.py')
        self.assertEqual(len([r for r in records if r[0] == 'installation-rewrite']), 1)


if __name__ == '__main__':
    unittest.main()

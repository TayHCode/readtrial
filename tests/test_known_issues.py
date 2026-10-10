from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from test_round4 import WindowsTarget
from test_trial import FakeAgent, FakeTarget

from readtrial.process import Result
from readtrial.targets import Limits
from readtrial.trial import Agent, run_target


class LinuxReadmeTarget(FakeTarget):
    def command(self, command, **kwargs):
        if 'README.md' in command:
            result = Result(0, '1 ## Quickstart\n2 ```sh\n3 python3 -m venv .venv\n4 echo x\n5 ```\n', 0)
            self.log('command', command, result)
            return result
        return super().command(command, **kwargs)


class KnownIssueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        FakeTarget.commands = {}
        FakeTarget.metadata = '{}'
        FakeTarget.prepare_error = FakeTarget.close_error = ''
        FakeTarget.seen = []
        FakeAgent.calls = []
        self.config = {'repo': '.', 'targets': ['ssh:example'], 'agent_command': 'fake',
                       'ssh_linux_ack': True, 'limits': asdict(Limits()),
                       'checks': [{'command': 'check'}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_deviation_visible_after_completion_on_both_shells(self):
        for target, proposed, exact in [
            (LinuxReadmeTarget, 'cd examples && python3 -m venv .venv', 'python3 -m venv .venv'),
            (WindowsTarget, 'cd examples; if ($?) { py -3 hello.py --name Check }',
             'py -3 hello.py --name Check'),
        ]:
            with self.subTest(target=target.__name__):
                FakeAgent.actions = [{'command': proposed}, {'done': True, 'findings': []}]
                destination = self.root / target.__name__
                with patch('sys.stdout', new_callable=io.StringIO) as output:
                    summary = run_target(self.config, 'ssh:example', destination, Path('unused'),
                                         'fake', target_factory=target, agent_factory=FakeAgent)
                self.assertEqual(summary['verdict'], 'PASS (partial)')
                self.assertEqual(summary['readme_coverage']['ran'], 1)
                self.assertNotIn(proposed, target.seen)
                self.assertIn(exact, target.seen)
                notes = json.loads((destination / 'findings.json').read_text())
                self.assertEqual(len(notes), 1)
                self.assertEqual(notes[0]['command'], proposed)
                self.assertIn('ignored added cd', notes[0]['actual'])
                self.assertIn(exact, notes[0]['actual'])
                self.assertIn('agent-deviation', notes[0]['id'])
                report = (destination / 'report.md').read_text()
                self.assertIn('ignored added cd', report)
                self.assertIn(proposed, report)
                self.assertIn(exact, report)
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertIn('AGENT deviation', lines[0])
                self.assertIn('ignored added cd', lines[0])

    def test_deviation_survives_later_agent_error_without_passing_trial(self):
        FakeAgent.actions = [{'command': 'cd examples && python3 -m venv .venv'},
                             {'command': '{"wrong":"echo x"}'}, {'command': '{"wrong":"echo x"}'}]
        destination = self.root / 'failed'
        with patch('sys.stdout', new_callable=io.StringIO):
            summary = run_target(self.config, 'ssh:example', destination, Path('unused'),
                                 'fake', target_factory=LinuxReadmeTarget, agent_factory=FakeAgent)
        self.assertEqual(summary['verdict'], 'INCOMPLETE')
        notes = json.loads((destination / 'findings.json').read_text())
        self.assertEqual(len(notes), 1)
        self.assertIn('ignored added cd', notes[0]['actual'])

    def test_annotations_only_for_unwrapped_actions_in_every_adapter(self):
        plain = {'command': 'echo x'}
        done = {'command': None, 'done': True, 'findings': []}
        nested = {'command': json.dumps(plain)}
        for template in ['fake', 'claude -p', 'codex exec -m fake-model']:
            for action, unwrapped in [(plain, False), (done, False), (nested, True)]:
                with self.subTest(template=template, action=action):
                    if template.startswith('claude'):
                        envelope = json.dumps({'structured_output': action})
                    elif template.startswith('codex'):
                        envelope = json.dumps({'type': 'item.completed', 'item': {
                            'type': 'agent_message', 'text': json.dumps(action)}})
                    else:
                        envelope = json.dumps(action)
                    with patch('readtrial.trial.execute', return_value=Result(0, envelope, 0)), \
                         patch('readtrial.codex_adapter.catalog', return_value=self.root / 'catalog.json'):
                        value, result = Agent(template, Limits()).ask({'history': [{}]}, 10, self.root)
                    self.assertEqual(value, plain if action != done else {'done': True, 'findings': []})
                    self.assertEqual('one serialized command level accepted' in result.output, unwrapped)

    def test_claude_text_result_records_unwrap(self):
        action = {'command': json.dumps({'command': 'echo x'})}
        envelope = json.dumps({'result': json.dumps(action)})
        with patch('readtrial.trial.execute', return_value=Result(0, envelope, 0)):
            _, result = Agent('claude -p', Limits()).ask({'history': [{}]}, 10, self.root)
        self.assertIn('one serialized command level accepted', result.output)

    def test_retry_annotation_matches_final_action(self):
        for unwrapped in [False, True]:
            with self.subTest(unwrapped=unwrapped):
                action = {'command': json.dumps({'command': 'echo x'}) if unwrapped else 'echo x'}
                results = [Result(0, json.dumps({'command': '{"wrong":"echo x"}'}), 0),
                           Result(0, json.dumps(action), 0)]
                with patch('readtrial.trial.execute', side_effect=results):
                    agent = Agent('fake', Limits())
                    _, result = agent.ask({'history': [{}]}, 10, self.root)
                self.assertIn('Protocol correction retry', result.output)
                self.assertEqual('one serialized command level accepted' in result.output, unwrapped)
                self.assertEqual(len(agent.retry_records), 1)
                self.assertNotIn('one serialized command level accepted', agent.retry_records[0].output)


if __name__ == '__main__':
    unittest.main()

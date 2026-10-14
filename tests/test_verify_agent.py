"""Hermetic native-provider contract and exact shell-text regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from readtrial.cli import main
from readtrial.process import Result
from readtrial.protocol import cli_error_text
from readtrial.targets import Limits
from readtrial.trial import Agent, AgentError
from readtrial.verify import BRIEF, RECIPE_SCHEMA, SCHEMA, action
from test_verify import PLAN


ROOT = Path(__file__).parents[1]


class NativeVerifyAgentTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def agent(self, template='claude -p --model independent'):
        return Agent(template, Limits(), brief=BRIEF, schema=SCHEMA,
                     normalizer=action, require_command=False, first_schema=RECIPE_SCHEMA)

    def strict_cli(self, replies):
        replies = iter(replies)

        def run(argv, **kwargs):
            schema = json.loads(argv[argv.index('--json-schema') + 1])
            if schema.get('type') != 'object':
                return Result(1, 'input_schema.type: Field required', 0)
            if any(key in schema for key in ('anyOf', 'oneOf', 'allOf')):
                return Result(1, 'input_schema does not support top-level unions', 0)
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(schema['required'], ['action'])
            self.assertEqual(set(schema['properties']), {'action'})
            nested = schema['properties']['action']
            context = kwargs['data'].decode()
            if '"recipe": null' in context:
                self.assertEqual(nested, RECIPE_SCHEMA)
            else:
                self.assertEqual(nested, SCHEMA)
                for branch in nested['anyOf']:
                    self.assertFalse(branch['additionalProperties'])
                    self.assertTrue(branch['required'])
            return Result(0, json.dumps({'structured_output': {'action': next(replies)}}), 0)
        return run

    def test_strict_cli_accepts_recipe_only_first_then_union_and_unwraps(self):
        agent = self.agent()
        with patch('readtrial.trial.execute', side_effect=self.strict_cli(PLAN[:2])) as run:
            first, _ = agent.ask({'recipe': None, 'history': []}, 10, self.directory)
            self.assertEqual(first, PLAN[0])
            next_action, _ = agent.ask({'recipe': first['recipe'], 'history': []}, 10, self.directory)
            self.assertEqual(next_action, PLAN[1])
        self.assertEqual(run.call_count, 2)

    def test_saved_recipe_uses_union_immediately(self):
        with patch('readtrial.trial.execute', side_effect=self.strict_cli([PLAN[1]])):
            value, _ = self.agent().ask({'recipe': PLAN[0]['recipe'], 'history': []}, 10, self.directory)
        self.assertEqual(value, PLAN[1])

    def test_wrapped_schema_preserves_expected_exit_and_replacement_actions(self):
        command = {**PLAN[1], 'expected_exit_code': 2}
        observation = {**PLAN[2], 'proof_steps': [2],
                       'superseded': [{'step': 1, 'reason': 'Corrected command.'}]}
        with patch('readtrial.trial.execute', side_effect=self.strict_cli([command, observation])):
            for expected in (command, observation):
                value, _ = self.agent().ask({'recipe': PLAN[0]['recipe']}, 10, self.directory)
                self.assertEqual(value, expected)
        command_schema = SCHEMA['anyOf'][1]['properties']['expected_exit_code']
        self.assertEqual(command_schema['default'], 0)
        self.assertEqual(command_schema['type'], 'integer')
        self.assertNotIn('expected_exit_code', SCHEMA['anyOf'][2]['properties'])

    def test_next_action_is_outside_untrusted_history_and_reaches_native_agent(self):
        hint = 'Report the observation from recorded output.'
        with patch('readtrial.trial.execute', side_effect=self.strict_cli([PLAN[2]])) as run:
            self.agent().ask({'recipe': PLAN[0]['recipe'], 'next_action': hint}, 10, self.directory)
        prompt = run.call_args.kwargs['data'].decode()
        trusted = prompt.split('UNTRUSTED TRIAL DATA:', 1)[0]
        self.assertIn('NEXT ACTION: ' + hint, trusted)
        self.assertIn('Do not return another recipe', trusted)

    def test_command_before_recipe_is_corrected_and_never_returned(self):
        with patch('readtrial.trial.execute', side_effect=self.strict_cli([PLAN[1], PLAN[0]])) as run:
            value, _ = self.agent().ask({'recipe': None, 'history': []}, 10, self.directory)
        self.assertEqual(value, PLAN[0])
        self.assertIn('commands cannot run before recipe selection', run.call_args.kwargs['data'].decode())
        self.assertEqual(run.call_count, 2)

    def test_repeated_phase_error_stops_with_clear_message(self):
        with patch('readtrial.trial.execute', side_effect=self.strict_cli([PLAN[1], PLAN[1]])) as run:
            with self.assertRaisesRegex(AgentError, 'commands cannot run before recipe selection'):
                self.agent().ask({'recipe': None}, 10, self.directory)
        self.assertEqual(run.call_count, 2)

    def test_wrapper_and_inner_unknown_fields_are_strict(self):
        for wrapped in ({'action': PLAN[1], 'extra': True},
                        {'action': {**PLAN[1], 'recipe': PLAN[0]['recipe']}},
                        PLAN[1]):
            with self.subTest(wrapped=wrapped):
                with patch('readtrial.trial.execute', return_value=Result(
                        0, json.dumps({'structured_output': wrapped}), 0)):
                    with self.assertRaises(AgentError):
                        self.agent().ask({'recipe': PLAN[0]['recipe']}, 10, self.directory)

    def test_custom_agent_keeps_bare_actions_and_receives_recipe_correction(self):
        outputs = [Result(0, json.dumps(value), 0) for value in (PLAN[1], PLAN[0])]
        with patch('readtrial.trial.execute', side_effect=outputs) as run:
            value, _ = self.agent('offline-agent').ask({'recipe': None}, 10, self.directory)
        self.assertEqual(value, PLAN[0])
        self.assertIn('first action must supply only a recipe', run.call_args.kwargs['data'].decode())

    def test_command_decodes_once_for_structured_and_text_envelopes(self):
        # Backslashes may be intentional shell text. Never heuristically strip them.
        for command in ('printf "hello\\n"', r'printf \"hello\\n\"',
                        "python3 -c 'print(\"quoted value\")'"):
            value = {**PLAN[1], 'command': command}
            for envelope in ({'structured_output': {'action': value}, 'result': 'ignored'},
                             {'result': json.dumps({'action': value})}):
                with self.subTest(command=command, envelope=envelope):
                    with patch('readtrial.trial.execute', return_value=Result(0, json.dumps(envelope), 0)):
                        decoded, _ = self.agent().ask({'recipe': PLAN[0]['recipe']}, 10, self.directory)
                    self.assertEqual(decoded['command'], command)

    def test_cli_error_extracts_actual_text_not_session_metadata(self):
        raw = json.dumps({'is_error': True, 'result': 'API Error: 400 input_schema.type: Field required',
                          'session_id': 'session-metadata', 'usage': {'input_tokens': 0}})
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                with patch('readtrial.trial.execute', return_value=Result(exit_code, raw, 0)) as run:
                    with self.assertRaises(AgentError) as caught:
                        self.agent().ask({'recipe': None}, 10, self.directory)
                self.assertIn('API Error: 400 input_schema.type: Field required', str(caught.exception))
                self.assertEqual(caught.exception.result.output, str(caught.exception))
                self.assertNotIn('session-metadata', str(caught.exception))
                self.assertNotIn('[response omitted]', str(caught.exception))
                self.assertEqual(run.call_count, 1)

    def test_plain_errors_redact_environment_and_secret_shapes_before_truncating(self):
        known, unknown = secrets.token_hex(18), secrets.token_hex(18)
        raw = 'provider rejected input; TOKEN=' + unknown + '; Bearer ' + unknown + '; sk-' + unknown
        raw += '; ' + known + '; ' + 'x' * 2000
        with patch.dict(os.environ, {'FIXTURE_TOKEN': known}):
            diagnostic = cli_error_text(raw)
        self.assertIn('provider rejected input', diagnostic)
        self.assertNotIn(known, diagnostic)
        self.assertNotIn(unknown, diagnostic)
        self.assertIn('[truncated]', diagnostic)
        self.assertLess(len(diagnostic), 500)
        self.assertNotIn('\x1b', cli_error_text('failed \x1b[31m'))
        self.assertNotIn(unknown, cli_error_text('unexpected credential ' + unknown))

    def test_error_envelope_after_progress_keeps_error_without_metadata(self):
        raw = 'CLI progress\n' + json.dumps({'result': 'API Error: schema rejected',
                                            'session_id': 'private-session'})
        self.assertEqual(cli_error_text(raw), 'API Error: schema rejected')

    def test_verify_cli_prints_the_saved_error(self):
        diagnostic = 'agent-protocol-error: CLI error: provider rejected schema'
        report = {'checks': [], 'tool_problems': [{'tool': 'readtrial verify runner', 'problem': diagnostic}]}
        with patch('readtrial.verify.verify', return_value=(report, 2)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                code = main(['verify', '.', '--requirements', 'examples/verify-requirements.json',
                             '--model', 'independent', '--builder-model', 'builder',
                             '--source-hash', 'a' * 64, '--agent-command', 'offline-agent',
                             '--out', str(self.directory / 'out')])
        self.assertEqual(code, 2)
        self.assertIn(diagnostic, errors.getvalue())




if __name__ == '__main__':
    unittest.main()

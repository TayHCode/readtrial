from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from test_trial import FakeAgent, FakeTarget

from readtrial import codex_adapter
from readtrial.cli import main
from readtrial.demo import run_demo
from readtrial.installers import prepare
from readtrial.process import Result, decode_output, execute
from readtrial.protocol import claude_response, normalize
from readtrial.shell import posix_script, powershell_script, split, statuses
from readtrial.targets import Limits, Target, TargetError
from readtrial.trial import Agent, AgentError, run_target
from readtrial.trusted import TrustedSSH, inspect

DIAGNOSTIC = 'Code Mode is unavailable because code-mode host is disabled'


def events(action):
    return '\n'.join(map(json.dumps, [
        {'type': 'item.completed', 'item': {'type': 'error', 'message': DIAGNOSTIC}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(action)}}]))


class ProtocolTests(unittest.TestCase):
    def test_aliases_normalize_with_exact_old_diagnostic(self):
        for field in ('command', 'proposed_command', 'cmd'):
            self.assertEqual(codex_adapter.response(events({field: 'python3 --version'})), {'command': 'python3 --version'})

    def test_strict_alias_and_field_validation(self):
        for value in ({'command': 'x', 'cmd': 'y'}, {'proposed_command': 2}, {'unknown': 'x'},
                      {'command': 'x', 'done': True}, {'command': 'x', 'extra': False}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize(value)

    def test_structured_shape(self):
        self.assertEqual(normalize({'command': 'x', 'done': False, 'findings': []}), {'command': 'x'})
        self.assertEqual(normalize({'command': None, 'done': True, 'findings': []}), {'done': True, 'findings': []})

    def test_claude_alias_envelope_and_diagnostics(self):
        raw = 'Harmless diagnostic\n' + json.dumps({'is_error': False, 'result': '{"cmd":"py -3 --version"}'})
        self.assertEqual(claude_response(raw), {'command': 'py -3 --version'})

    def test_claude_structured_result(self):
        self.assertEqual(claude_response(json.dumps({'structured_output': {'command': 'x', 'done': False, 'findings': []}})), {'command': 'x'})

    def test_codex_retry_done_without_command(self):
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.codex_adapter.catalog', return_value=Path(folder) / 'catalog.json'), patch('readtrial.trial.execute', side_effect=[Result(0, events({'done': True, 'findings': []}), 0), Result(0, events({'proposed_command': 'python3 --version'}), 0)]) as runner:
            action, result = Agent('codex exec -m gpt-6-luna', Limits()).ask({'history': []}, 5, Path(folder))
            schema = json.loads((Path(folder) / 'action-schema.json').read_text())
            self.assertEqual(set(schema['properties']), {'command'})
            self.assertFalse(schema['additionalProperties'])
        self.assertEqual(action, {'command': 'python3 --version'})
        self.assertEqual(runner.call_count, 2)
        self.assertIn('PROTOCOL CORRECTION', runner.call_args.kwargs['data'].decode())
        self.assertIn('--output-schema', runner.call_args.args[0])
        self.assertIn('done without a README command proposal', result.output)

    def test_claude_retry_then_explicit_failure(self):
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.trial.execute', return_value=Result(0, '{"result":"unsupported"}', 0)) as runner, self.assertRaisesRegex(AgentError, 'agent-protocol-error:.*returned:'):
            Agent('claude -p --model claude-haiku-5-5', Limits()).ask({'history': []}, 5, Path(folder))
        self.assertEqual(runner.call_count, 2)

    def test_exact_claude_serialized_action_unwraps_before_execution(self):
        broken = {'structured_output': {'command': '{"command":"python3 release_check.py --help","done":false,"findings":[]}'}}
        fixed = {'structured_output': {'command': 'python3 release_check.py --help'}}
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.trial.execute', side_effect=[Result(0, json.dumps(broken), 0), Result(0, json.dumps(fixed), 0)]) as runner:
            action, _ = Agent('claude -p --model claude-haiku-5-5', Limits()).ask({'history': []}, 10, Path(folder))
        self.assertEqual(action, {'command': 'python3 release_check.py --help'})
        self.assertEqual(runner.call_count, 1)

    def test_claude_command_with_early_findings_gets_specific_correction(self):
        from test_trial import finding
        broken = {'structured_output': {'command': 'python3 release_check.py --help', 'done': False, 'findings': [finding()]}}
        fixed = {'structured_output': {'command': 'python3 release_check.py --help', 'done': False, 'findings': []}}
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.trial.execute', side_effect=[Result(0, json.dumps(broken), 0), Result(0, json.dumps(fixed), 0)]) as runner:
            action, _ = Agent('claude -p --model claude-haiku-5-5', Limits()).ask({'history': [{'command':'previous'}]}, 10, Path(folder))
        self.assertEqual(action, {'command': 'python3 release_check.py --help'})
        self.assertIn('Findings MUST be []', runner.call_args.kwargs['data'].decode())

    def test_error_scrubs_runtime_only_values(self):
        import secrets
        token = secrets.token_hex(18)
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'FAKE_TOKEN': token}), patch('readtrial.trial.execute', return_value=Result(2, token + 'z' * 2000, 0)), self.assertRaises(AgentError) as caught:
            Agent('fake', Limits()).ask({}, 5, Path(folder))
        self.assertNotIn(token, str(caught.exception))
        self.assertIn('[redacted]', str(caught.exception))
        self.assertIn('[truncated]', str(caught.exception))
        self.assertLess(len(str(caught.exception)), 1300)

    def test_native_tool_event_no_retry(self):
        raw = json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution'}})
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.codex_adapter.catalog', return_value=Path(folder) / 'catalog.json'), patch('readtrial.trial.execute', return_value=Result(0, raw, 0)) as runner, self.assertRaises(AgentError):
            Agent('codex exec -m gpt-6-luna', Limits()).ask({}, 5, Path(folder))
        self.assertEqual(runner.call_count, 1)

    def test_catalog_overrides_only_requested_model_tools(self):
        fixture = {'models': [{'slug': 'gpt-6-luna', 'tool_mode': 'code_mode_only', 'experimental_supported_tools': ['clock'], 'base_instructions': 'original'}, {'slug': 'other'}]}
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.codex_adapter.execute', return_value=Result(0, 'diagnostic\n' + json.dumps(fixture), 0)) as runner:
            path = codex_adapter.catalog('codex', 'gpt-6-luna', Path(folder), 5)
            actual = json.loads(path.read_text())['models']
        self.assertEqual(actual, [{'slug': 'gpt-6-luna', 'tool_mode': 'direct', 'experimental_supported_tools': [], 'base_instructions': 'original', 'apply_patch_tool_type': None, 'supports_search_tool': False}])
        self.assertIn('--bundled', runner.call_args.args[0])

    def test_exact_plain_native_router_failure_is_not_harmless(self):
        diagnostic = "2026-10-10T04:33:25.506624Z ERROR codex_core::tools::router: error=apply_patch verification failed: invalid hunk at line 2, Update file hunk for path '/dev/null' is empty"
        with self.assertRaisesRegex(ValueError, 'configuration diagnostic'):
            codex_adapter.response(diagnostic + '\n' + events({'command': 'python3 --version'}))

    def test_exact_rejected_function_arguments_are_not_execution(self):
        diagnostic = '2026-10-10T04:38:45.734729Z ERROR codex_core::tools::router: error=failed to parse function arguments: unknown field `command`, expected `timeout_ms` at line 1 column 10'
        self.assertEqual(codex_adapter.response(diagnostic + '\n' + events({'command': 'python3 --version'})), {'command': 'python3 --version'})
        with self.assertRaises(TypeError):
            codex_adapter.response(diagnostic)

    def test_windows_shell_brief(self):
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.trial.execute', return_value=Result(0, '{"command":"py -3 --version"}', 0)) as runner:
            Agent('fake', Limits()).ask({'platform': 'Windows', 'history': []}, 5, Path(folder))
        prompt = runner.call_args.kwargs['data'].decode()
        self.assertIn('Windows PowerShell 5.1: no &&', prompt)
        self.assertIn('use py -3, not python3', prompt)


class SegmentTests(unittest.TestCase):
    def measured(self, command):
        with tempfile.TemporaryFile() as stream:
            result = execute(['sh', '-c', posix_script(command, f'/dev/fd/{stream.fileno()}')], pass_fds=(stream.fileno(),))
            stream.seek(0)
            steps = statuses(stream.read().decode(), command)
        return result, steps

    def test_exact_failure_cannot_be_masked_by_echo(self):
        result, steps = self.measured('false && echo skipped; echo EXIT=$?')
        self.assertEqual(result.exit_code, 0)  # Original shell aggregate is preserved.
        self.assertEqual([s['exit_code'] for s in steps], [1, 0])
        self.assertEqual(steps[0]['command'], 'false')
        self.assertIn('EXIT=1', result.output)

    def test_pipeline_and_or_failures_recorded(self):
        _, steps = self.measured('false | cat; false || echo recovered')
        self.assertEqual([s['exit_code'] for s in steps], [1, 0, 1, 0])

    def test_cd_state_preserved(self):
        result, steps = self.measured('cd . && printf ok; echo done')
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(all(s['exit_code'] == 0 for s in steps))

    def test_quotes_and_substitutions_not_split(self):
        segments, operators = split('printf "%s" "a; b && c" && echo "$(printf x)"')
        self.assertEqual(len(segments), 2)
        self.assertEqual(operators, ['&&'])

    def test_backtick_substitution_and_invalid_trailing_operator(self):
        segments, operators = split('echo `printf "a; b"` && echo done')
        self.assertEqual(len(segments), 2)
        self.assertEqual(operators, ['&&'])
        for command in ('true; false &&', 'true |', 'false ||'):
            with self.assertRaisesRegex(ValueError, 'trailing shell operator'):
                split(command)

    def test_install_allowance_with_activation_in_compound(self):
        command = 'python3 -m venv .venv && . .venv/bin/activate && python -m pip install . ; echo pip_exit=$? && release-check --help'
        run, _, denial = prepare(command, '/workspace')
        self.assertFalse(denial)
        self.assertIn('test -x ./.venv/bin/python', run)
        self.assertIn('./.venv/bin/python -m pip install .', run)
        self.assertFalse(inspect(command, set(), '/workspace'))

    def test_official_installer_inside_compound(self):
        command = 'python3 --version && curl -LsSf https://astral.sh/uv/install.sh | sh && uv --version'
        run, _, denial = prepare(command, '/workspace')
        self.assertFalse(denial)
        self.assertIn('UV_NO_MODIFY_PATH=1 sh && uv --version', run)
        self.assertIn('segment 4', inspect(command + '; sudo -n true', set(), '/workspace'))

    def test_powershell_preserves_guard_and_measures_pipeline_stages(self):
        script = powershell_script('py -3 -c "exit(7)" | Out-String; if ($?) { Write-Output success }', "'steps'")
        self.assertIn("'1:'", script)
        self.assertIn("'2:'", script)
        self.assertIn("'3:'", script)
        self.assertIn('$input | Out-String', script)
        self.assertIn('if ($script:rt_previous_ok)', script)

    def test_only_offending_segment_named(self):
        reason = inspect('python3 --version && sudo -n true; echo done', set())
        self.assertEqual(reason, 'segment 2 `sudo -n true` is denied: sudo/admin elevation')

    def test_pipeline_cannot_allow_downloaded_code(self):
        for command in ('curl https://pypi.org/x | env sh', 'curl https://pypi.org/x -o x; sh x'):
            self.assertTrue(prepare(command, '/workspace')[2])

    def test_unicode_and_oem_decoding(self):
        text = 'README – author’s route'
        self.assertEqual(decode_output(text.encode('utf-8')), text)
        self.assertEqual(decode_output(text.encode('utf-16')), text)
        self.assertEqual(decode_output('café'.encode('cp437')), 'café')
        self.assertNotIn('\ufffd', decode_output(bytes([0xff])))
        self.assertNotIn('\ufffd', decode_output('README \U0001f600'.encode('utf-16')[:-2]))

    def test_powershell_utf8_transport_and_step_status(self):
        import base64

        from readtrial.trusted import powershell
        decoded = base64.b64decode(powershell('Get-Content README.md').split()[-1]).decode('utf-16le')
        self.assertIn('[Console]::OutputEncoding', decoded)
        self.assertIn("Get-Content:Encoding", decoded)
        t = Target('ssh:example', Limits(), lambda *args: None, trusted_target=True)
        t.platform = 'Windows'
        script = TrustedSSH(t).windows_script('py -3 --version; Write-Output done', 3)
        self.assertIn('steps_b64', script)
        self.assertTrue(script.startswith('$OutputEncoding='))

    def test_windows_raw_utf16_response_is_preserved(self):
        import base64
        target = Target('ssh:example', Limits(), lambda *args: None, trusted_target=True)
        target.platform, target.base = 'Windows', 'C:\\Temp\\readtrial-owned'
        text = 'README – author’s route'
        frame = {'exit_code': 0, 'seconds': 0, 'output_b64': base64.b64encode(text.encode('utf-16')).decode(),
                 'steps_b64': base64.b64encode(b'1:0\n').decode()}
        with patch('readtrial.trusted.execute', return_value=Result(0, json.dumps(frame), 0)) as runner:
            result = TrustedSSH(target).command('py -3 --version')
        self.assertEqual(result.output, text)
        self.assertTrue(runner.call_args.kwargs['data'].startswith(b'$OutputEncoding='))


class ExperienceTests(unittest.TestCase):
    def test_linux_ssh_refuses_before_folder_creation(self):
        target = Target('ssh:example', Limits(), lambda *args: None)
        with patch('readtrial.targets.execute', return_value=Result(0, 'Linux', 0)), self.assertRaisesRegex(TargetError, '--i-understand-ssh-linux-is-not-confined'):
            target.prepare(Path('unused'))
        self.assertFalse(target.created)

    def test_linux_explicit_ack_selects_honest_backend(self):
        target = Target('ssh:example', Limits(), lambda *args: None, ssh_linux_ack=True)
        with patch('readtrial.targets.execute', return_value=Result(0, 'Linux', 0)), patch('readtrial.trusted.TrustedSSH.prepare'):
            target.prepare(Path('unused'))
        self.assertIsNotNone(target.trusted)
        self.assertIn('no network/filesystem confinement', target.isolation)

    def test_lxc_instance_prefix(self):
        self.assertTrue(Target('lxc:ubuntu:24.04', Limits(), lambda *args: None, instance_prefix='trial-owner-').id.startswith('trial-owner-'))
        self.assertEqual(len(Target('lxc:ubuntu:24.04', Limits(), lambda *args: None, instance_prefix='a' * 31).id), 63)
        for prefix in ('../bad', 'Bad', '', 'a' * 32):
            with self.assertRaises(TargetError):
                Target('lxc:ubuntu:24.04', Limits(), lambda *args: None, instance_prefix=prefix)

    def test_dry_run_creates_nothing_and_calls_nothing(self):
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.cli.run') as runner, patch('sys.stdout', new_callable=io.StringIO) as output:
            destination = Path(folder) / 'unused'
            self.assertEqual(main(['run', '.', '--target', 'ssh:example', '--agent-command', 'fake', '--out', str(destination), '--dry-run']), 0)
            self.assertFalse(destination.exists())
        runner.assert_not_called()
        self.assertIn('"dry_run": true', output.getvalue())

    def test_demo_real_sample_fail_pass(self):
        with tempfile.TemporaryDirectory() as folder, patch('readtrial.trial.execute') as runner, patch('sys.stdout', new_callable=io.StringIO):
            destination = Path(folder) / 'demo'
            self.assertEqual(run_demo(destination), 0)
            values = json.loads((destination / 'run.json').read_text())['results']
            self.assertEqual([v['verdict'] for v in values], ['FAIL', 'PASS'])
            self.assertEqual(len(json.loads((destination / 'diff.json').read_text())['resolved']), 1)
        runner.assert_not_called()

    def test_failed_step_heads_report_and_checks_are_separate(self):
        FakeTarget.commands = {'bad; echo done': Result(7, 'done', 0, steps=[{'segment': 1, 'command': 'bad', 'exit_code': 7}])}
        FakeTarget.prepare_error = FakeTarget.close_error = ''
        FakeAgent.actions = [{'command': 'bad; echo done'}, {'done': True, 'findings': []}]
        config = {'repo': '.', 'agent_command': 'fake', 'checks': [{'command': 'check'}], 'limits': asdict(Limits())}
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'result'
            result = run_target(config, 'fake', destination, Path('unused'), 'fake', target_factory=FakeTarget, agent_factory=FakeAgent)
            report = (destination / 'report.md').read_text()
        self.assertEqual(result['verdict'], 'FAIL')
        self.assertTrue(result['declared_success_checks_passed'])
        self.assertFalse(result['every_readme_step_succeeded'])
        self.assertIn('Failed segment 1 (exit 7): `bad`', report)
        FakeTarget.commands = {}

    def test_output_exists_is_friendly(self):
        with tempfile.TemporaryDirectory() as folder, patch('sys.stderr', new_callable=io.StringIO) as output, self.assertRaises(SystemExit):
            main(['demo', '--out', folder])
        self.assertIn('output folder already exists', output.getvalue())
        self.assertNotIn('Errno', output.getvalue())

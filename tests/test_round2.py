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
from readtrial.process import Result, execute
from readtrial.targets import Limits, Target
from readtrial.trial import Agent, diff, run, run_target
from readtrial.trusted import TrustedSSH, inspect
from readtrial.worker import policy

README = '# Example\n\n## Quickstart\n```sh\npython3 hello.py --help\npython3 hello.py --name Ada\n```\n'


class ReadmeTarget(FakeTarget):
    def command(self, command, **kwargs):
        if 'README.md' in command:
            result = Result(0, ''.join(f'{i} {line}\n' for i, line in enumerate(README.splitlines(), 1)), 0)
            self.log('command', command, result)
            return result
        return super().command(command, **kwargs)


class RoundTwoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        FakeTarget.commands = {}
        FakeTarget.prepare_error = FakeTarget.close_error = ''
        FakeTarget.seen = []
        FakeAgent.calls = []
        self.config = {'repo': '.', 'targets': ['fake'], 'agent_command': 'fake',
                       'limits': asdict(Limits()), 'checks': [{'command': 'check'}]}

    def tearDown(self):
        self.tmp.cleanup()

    def trial(self, actions):
        FakeAgent.actions = actions + [{'done': True, 'findings': []}]
        return run_target(self.config, 'fake', self.root / 'result', Path('unused'), 'fake',
                          target_factory=ReadmeTarget, agent_factory=FakeAgent)

    def test_help_only_has_partial_verdict_and_stdout_coverage(self):
        result = self.trial([{'command': 'python3 hello.py --help'}])
        self.assertEqual(result['verdict'], 'PASS (partial)')
        self.assertEqual(result['readme_coverage']['declared'], 2)
        self.assertEqual(result['readme_coverage']['ran'], 1)
        with patch('readtrial.trial.snapshot', return_value=(Path('unused'), 'fake')), \
                patch('readtrial.trial.run_target', return_value=result), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(run(self.config, self.root / 'out'), 0)
        self.assertIn('PASS (partial): agent ran 1 of 2 README steps', output.getvalue())

    def test_full_quickstart_has_qualified_pass(self):
        result = self.trial([{'command': 'python3 hello.py --help'}, {'command': 'python3 hello.py --name Ada'}])
        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(result['verdict_detail'], 'PASS' + ': all 2 declared README steps ran and succeeded')

    def test_duplicate_success_is_logged_without_running_again(self):
        result = self.trial([{'command': 'python3 hello.py --help'}] * 2)
        self.assertEqual(FakeTarget.seen.count('python3 hello.py --help'), 1)
        self.assertEqual(result['readme_coverage']['ran'], 1)
        records = [json.loads(line) for line in (self.root / 'result/commands.jsonl').read_text().splitlines()]
        self.assertTrue(any(r['kind'] == 'duplicate-command' and r['exit_code'] is None for r in records))

    def test_denied_command_is_incomplete_and_finding_is_tool(self):
        proposal = 'python3 ../outside.py'
        FakeTarget.commands[proposal] = Result(126, '', 0, denied='path outside workspace')
        FakeAgent.actions = [{'command': proposal}, {'done': True, 'findings': [finding() | {'type': 'DOCS', 'command': proposal}]}]
        result = run_target(self.config, 'fake', self.root / 'result', Path('unused'), 'fake',
                            target_factory=ReadmeTarget, agent_factory=FakeAgent)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        items = json.loads((self.root / 'result/findings.json').read_text())
        self.assertTrue(items)
        self.assertTrue(all(item['type'] == 'TOOL' for item in items))
        self.assertIn('trial ended after denial', (self.root / 'result/report.md').read_text().lower())

    def test_posix_parent_path_inside_clone_and_symlink_escape(self):
        repo = self.root / 'repo'
        (repo / 'examples').mkdir(parents=True)
        (repo / 'hello.py').write_text('print(1)')
        (repo / 'link').symlink_to(self.root)
        self.assertEqual(policy('cd examples && python3 ../hello.py', root=repo), '')
        self.assertTrue(policy('cd examples && cat ../../outside', root=repo))
        self.assertTrue(policy('cat link/../outside', root=repo))

    def test_trusted_parent_paths_use_shell_cwd(self):
        for windows, command in ((False, 'cd examples && python3 ../hello.py'),
                                 (True, r'cd examples; py -3 ..\hello.py')):
            self.assertEqual(inspect(command, set(), windows=windows), '')
        self.assertTrue(inspect('cat ../outside', set()))

    def test_powershell_prompt_has_no_and_chaining_advice(self):
        with patch('readtrial.trial.execute', return_value=Result(0, '{"structured_output":{"command":"py -3 hello.py"}}', 0)) as execute:
            Agent('claude -p', Limits()).ask({'platform': 'Windows'}, 2, self.root)
        prompt = execute.call_args.kwargs['data'].decode()
        self.assertNotIn('cd subdir &&', prompt)
        self.assertIn('cd subdir;', prompt)
        self.assertNotIn('cd subdir &&', execute.call_args.args[0][execute.call_args.args[0].index('--system-prompt') + 1])

    def test_rewrite_row_has_actual_failure_code(self):
        records = []
        target = Target('ssh:example', Limits(), lambda *args: records.append(args))
        target.base = str(self.root)
        result = Result(7, 'failed', 0)
        with patch('readtrial.trusted.execute', return_value=Result(0, json.dumps(result.json()), 0)):
            TrustedSSH(target).command('. .venv/bin/activate')
        rewrite = next(row for row in records if row[0] == 'installation-rewrite')
        self.assertEqual(rewrite[2].exit_code, 7)

    def test_windows_captures_native_stderr_as_text(self):
        target = Target('ssh:example', Limits(), lambda *args: None)
        target.platform = 'Windows'
        script = TrustedSSH(target).windows_script('py -3 -m unittest -q', 10)
        import base64
        import re
        encoded = re.search(r'EncodedCommand ([A-Za-z0-9+/=]+)', script)[1]
        inner = base64.b64decode(encoded).decode('utf-16le')
        self.assertIn('RedirectStandardError=$true', inner)
        self.assertIn('RedirectStandardOutput=$true', inner)
        self.assertNotIn('*>', script)
        self.assertIn('$native.WorkingDirectory=', inner)
        self.assertIn('$info.WorkingDirectory=', script)

    def test_missing_ssh_ack_does_not_create_output(self):
        self.config['targets'] = ['ssh:example']
        with patch('readtrial.targets.Target.probe_platform', return_value='Linux'), patch('readtrial.trial.snapshot') as snapshot, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(run(self.config, self.root / 'out'), 2)
        self.assertFalse((self.root / 'out').exists())
        snapshot.assert_not_called()

    def test_omitted_finding_is_not_verified_fixed(self):
        changes = diff([finding()], [])
        self.assertEqual(changes['resolved'], [])
        self.assertEqual(changes['no_longer_raised'][0]['id'], 'venv')

    def test_success_timing_names_are_explicit(self):
        result = self.trial([{'command': 'python3 hello.py --help'}])
        self.assertIn('minutes_to_first_successful_command', result)
        self.assertIn('minutes_to_first_declared_check_success', result)
        self.assertNotIn('minutes_to_first_command_success', result)

    def test_wrong_directory_segment_does_not_count_as_root_command(self):
        entry = {'command': 'cd examples && python3 hello.py --name Ada', **Result(0, '', 0,
                 steps=[{'segment': 1, 'command': 'cd examples', 'exit_code': 0},
                        {'segment': 2, 'command': 'python3 hello.py --name Ada', 'exit_code': 0}]).json()}
        self.assertEqual(coverage(declared_steps(README, 'Linux'), [entry])['ran'], 0)

    def test_windows_path_guard_uses_quote_safe_payload(self):
        target = Target('ssh:example', Limits(), lambda *args: None)
        target.platform = 'Windows'
        guard = TrustedSSH(target).path_guard(r'cd examples; py -3 ..\hello.py')
        self.assertNotIn('["', guard)
        self.assertIn('b64decode', guard)

    def test_missing_path_resolver_is_runner_refusal(self):
        target = Target('ssh:example', Limits(), lambda *args: None)
        guard = TrustedSSH(target).path_guard('cd examples && python3 ../hello.py')
        self.assertIn('resolver unavailable or failed', guard)

    def test_platform_routes_ignore_alternative_blocks_and_join_continuations(self):
        doc = '## Quickstart\n### Linux\n```sh\n# comment\n$ python3 hello.py \\\n  --help\n```\n```sh\noptional\n```\n### Windows\n```powershell\nPS> py -3 hello.py --help\n```\n'
        self.assertEqual([s['command'] for s in declared_steps(doc, 'Linux')], ['python3 hello.py  --help'])
        self.assertEqual([s['command'] for s in declared_steps(doc, 'Windows')], ['py -3 hello.py --help'])

    def test_path_guard_executes_in_clone_and_refuses_physical_escape(self):
        repo = self.root / 'repo'
        (repo / 'examples').mkdir(parents=True)
        (repo / 'hello.py').write_text('print("inside")')
        (repo / 'link').symlink_to(self.root)
        target = Target('ssh:example', Limits(), lambda *args: None)
        guard = TrustedSSH(target)
        allowed = execute(['sh', '-c', guard.path_guard('cd examples && python3 ../hello.py') +
                           'cd examples && python3 ../hello.py'], cwd=str(repo))
        self.assertEqual(allowed.exit_code, 0, allowed.output)
        self.assertIn('inside', allowed.output)
        refused = execute(['sh', '-c', guard.path_guard('cat link/../outside') + 'echo should-not-run'], cwd=str(repo))
        self.assertEqual(refused.exit_code, 126, refused.output)
        self.assertIn('READTRIAL_PATH_REFUSAL:', refused.output)
        self.assertNotIn('should-not-run', refused.output)

    def test_plain_symlink_argument_is_refused(self):
        repo = self.root / 'repo'
        repo.mkdir()
        (repo / 'hello.py').symlink_to(self.root / 'outside.py')
        self.assertTrue(policy('python3 hello.py', root=repo))

    def test_shell_comments_cannot_declare_markdown_quickstart(self):
        doc = '```sh\n## Quickstart decoy\necho decoy\n```\n## Quickstart\n```sh\necho real\n```\n'
        self.assertEqual([s['command'] for s in declared_steps(doc, 'Linux')], ['echo real'])

    def test_unexecuted_compound_tail_never_counts_as_coverage(self):
        entry = {'command': 'false && python3 hello.py --help', **Result(1, '', 0,
                 steps=[{'segment': 1, 'command': 'false', 'exit_code': 1}]).json()}
        self.assertEqual(coverage(declared_steps(README, 'Linux'), [entry])['ran'], 0)


if __name__ == '__main__':
    unittest.main()

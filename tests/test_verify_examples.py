"""Offline subprocess agent decisions and read-only Factory result validation."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import tempfile
import unittest
from unittest.mock import patch

from readtrial.cli import main
from readtrial.process import Result, execute
from readtrial.verify_result import validate_result
from platforms import posix_only

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('standin_agent', ROOT / 'examples/standin_agent.py')
standin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standin)


class ScratchTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.readme = (ROOT / 'examples/agent_product/README.md').read_text()
        self.items = json.loads((ROOT / 'examples/standin-requirements.json').read_text())


class ExampleTests(ScratchTests):
    @posix_only
    def test_documented_honest_failure_is_proof_in_self_verification(self):
        project = self.directory / 'self-product'
        project.mkdir()
        for name in ('readtrial', 'examples'):
            shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT / 'README.md', project / 'README.md')
        for argv in (['git', 'init', '-q', str(project)],
                     ['git', '-C', str(project), 'add', 'readtrial', 'examples', 'README.md'],
                     ['git', '-C', str(project), '-c', 'user.name=Sample', '-c', 'user.email=sample',
                      '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Toy committed product']):
            result = execute(argv, tempdir=str(self.directory))
            self.assertEqual(result.exit_code, 0, result.output)
        out = self.directory / 'honest-proof'
        job = json.loads((ROOT / 'examples/honest-failure-job.json').read_text())
        job['evidence_dir'] = str(self.directory)
        job_path = self.directory / 'honest-job.json'
        job_path.write_text(json.dumps(job))
        argv = ['verify', str(project), '--requirements', str(ROOT / 'examples/honest-failure-requirements.json'),
                '--job', str(job_path), '--builder-model', 'sample-builder',
                '--agent-command', 'python3 examples/standin_agent.py', '--trusted-local', '--independent-agent',
                '--out', str(out)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)
        self.assertEqual(validate_result(out / 'verify.json', job), 2)
        report = json.loads((out / 'verify.json').read_text())
        self.assertEqual([row['status'] for row in report['checks']], ['verified', 'verified'])
        first = report['checks'][0]['step_results'][0]
        self.assertEqual((first['expected_exit_code'], first['exit_code']), (1, 1))
        self.assertTrue(first['expectation_matched'])
        self.assertIn('multiplication:', report['checks'][1]['observed'])

    def context(self, history=None):
        return {'README': self.readme, 'requirements': self.items,
                'recipe': 'not applicable: CLI', 'history': history or []}

    def test_standin_derives_command_from_readme_and_arbitrary_id(self):
        self.items[0]['id'] = 'another-id'
        context = self.context()
        context['README'] = self.readme.replace('add 2 3', 'add 4 7')
        self.assertEqual(standin.decide(context), {
            'requirement_id': 'another-id', 'command': 'python3 examples/agent_product/product.py add 4 7',
            'expected_exit_code': 0})

    def test_standin_declares_nonzero_before_execution_and_checks_output(self):
        self.items = [{'id': 'reject', 'text': 'reject exits with code 2 and output containing empty input.'}]
        self.readme = '- reject: `python3 product.py reject`\n'
        context = self.context()
        self.assertEqual(standin.decide(context)['expected_exit_code'], 2)
        record = {'requirement_id': 'reject', 'exit_code': 2, 'expected_exit_code': 2,
                  'output': 'empty input', 'denied': ''}
        self.assertEqual(standin.decide(self.context([record]))['status'], 'verified')
        for changes in ({'expected_exit_code': 0}, {'exit_code': 0}, {'output': 'different'},
                        {'denied': 'refused'}, {'timed_out': True}):
            with self.subTest(changes=changes):
                self.assertEqual(standin.decide(self.context([{**record, **changes}]))['status'], 'unverified')

    def test_standin_changes_verdict_with_output_and_requirement(self):
        row = {'requirement_id': 'addition', 'output': '{"result": 5}', 'exit_code': 0, 'denied': ''}
        self.assertEqual(standin.decide(self.context([row]))['status'], 'verified')
        self.items[0]['text'] = 'add returns a result of 9.'
        self.assertEqual(standin.decide(self.context([row]))['status'], 'unverified')
        row['output'] = '{"result": 9}'
        self.assertEqual(standin.decide(self.context([row]))['status'], 'verified')
        row['exit_code'] = 1
        self.assertEqual(standin.decide(self.context([row]))['status'], 'unverified')
        row.update(exit_code=0, output='not JSON')
        self.assertEqual(standin.decide(self.context([row]))['status'], 'unverified')

    def test_standin_uses_completed_history_and_reports_unsupported_requirements(self):
        history = [{'agent_action': {'requirement_id': 'addition', 'status': 'verified'}}]
        self.assertEqual(standin.decide(self.context(history))['requirement_id'], 'multiplication')
        self.items[1]['text'] = 'General judgement beyond the numeric vocabulary.'
        self.assertEqual(standin.decide(self.context(history))['status'], 'unverified')
        history.append({'agent_action': {'requirement_id': 'multiplication', 'status': 'unverified'}})
        self.assertTrue(standin.decide(self.context(history))['done'])

    @unittest.skipUnless(os.name == 'posix', 'local verification requires POSIX')
    def test_documented_agent_command_and_stale_recipe_through_cli(self):
        project = self.directory / 'project'
        project.mkdir()
        shutil.copytree(ROOT / 'examples', project / 'examples')
        (project / 'README.md').write_text(self.readme)

        def runner(argv, **options):
            if argv[0] == 'git' and 'clone' in argv:
                shutil.copytree(project, Path(argv[-1]))
                return Result(0, 'fresh copy', 0)
            return execute(argv, **options)

        job = json.loads((ROOT / 'examples/verify-job.json').read_text())
        job['evidence_dir'] = str(self.directory)
        job_path = self.directory / 'job.json'
        job_path.write_text(json.dumps(job))
        with patch('readtrial.verify.snapshot', return_value=(self.directory / 'bundle', 'b' * 40)), \
                patch('readtrial.verify.execute', side_effect=runner):
            for name in ('run', 'stale', 'passing'):
                out = self.directory / name
                items_path = ROOT / 'examples' / ('standin-passing-requirements.json' if name == 'passing'
                                                  else 'standin-requirements.json')
                selected_job = {**job, 'required_check_ids': ['addition']} if name == 'passing' else job
                job_path.write_text(json.dumps(selected_job))
                argv = ['verify', str(project), '--requirements', str(items_path),
                        '--job', str(job_path), '--builder-model', 'sample-builder',
                        '--agent-command', 'python3 examples/standin_agent.py', '--trusted-local',
                        '--independent-agent', '--out', str(out)]
                if name == 'stale':
                    argv += ['--recipe', str(ROOT / 'examples/stale-recipe.json'), '--command-seconds', '1']
                previous = Path.cwd()
                try:
                    # The starting directory has no script: resolve from the project.
                    os.chdir(self.directory)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main(argv), 0 if name == 'passing' else 1)
                finally:
                    os.chdir(previous)
                report = json.loads((out / 'verify.json').read_text())
                self.assertEqual(validate_result(out / 'verify.json', selected_job), 1 if name == 'passing' else 2)
                self.assertFalse(report['tool_problems'])
                self.assertFalse((out / '.state').exists())
                self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in out.glob('*.json')))
                if name == 'run':
                    self.assertEqual([c['status'] for c in report['checks']], ['verified', 'unverified'])
                    self.assertIn('recorded result 5', report['checks'][1]['observed'])
                    proof = json.loads((out / 'proof-002.json').read_text())
                    self.assertEqual(json.loads(proof['commands'][0]['output']), {'result': 5})
                elif name == 'stale':
                    self.assertTrue(all(c['status'] == 'unverified' for c in report['checks']))
                    proof = json.loads((out / 'proof-001.json').read_text())
                    self.assertTrue(proof['lifecycle']['recipe_reused'])
                    self.assertTrue(proof['lifecycle']['source_changed'])
                    self.assertNotEqual(proof['lifecycle']['ready_attempts'][-1]['exit_code'], 0)
                    self.assertEqual(proof['lifecycle']['cleanup']['exit_code'], 0)
                    self.assertFalse(json.loads((out / 'recipe.json').read_text())['validated'])
                    self.assertIn('readiness failed', report['checks'][0]['reason'])
                else:
                    self.assertEqual([c['status'] for c in report['checks']], ['verified'])

    def test_release_commands_match_documented_lines_and_need_no_shell_expansion(self):
        manifest = json.loads((ROOT / 'release-check.json').read_text())
        for command in manifest['quickstart'] + manifest['checks']:
            if 'file' in command:
                line = (ROOT / command['file']).read_text(encoding='utf-8').splitlines()[command['line'] - 1]
                self.assertEqual(shlex.split(line), command['argv'])
            for argument in command['argv']:
                self.assertNotIn('$', argument)
                self.assertNotIn('|', argument)
                self.assertNotIn('*', argument)


class ResultValidationTests(ScratchTests):
    def setUp(self):
        super().setUp()
        self.job = {'stage': 'verify', 'route': {'model': 'offline-standin'}, 'source_hash': 'a' * 64,
                    'required_check_ids': ['one'], 'evidence_dir': str(self.directory)}
        self.path = self.directory / 'verify.json'
        self.proof = self.directory / 'proof.json'
        self.proof.write_text('{}\n')
        self.report = {'run_id': 'b' * 32, 'model': 'offline-standin', 'source_hash': 'a' * 64,
                       'source_commit': 'c' * 40,
                       'recipe': 'not applicable: CLI', 'not_checked': ['none'], 'tool_problems': [],
                       'checks': [{'requirement_id': 'one', 'steps': ['documented command'],
                                   'observed': 'Observed output', 'status': 'verified', 'exit_code': 0,
                                   'artifact': str(self.proof),
                                   'artifact_sha256': hashlib.sha256(self.proof.read_bytes()).hexdigest()}]}
        self.report, proof = self.detailed()
        self.save_detailed(self.report, proof)

    def save(self, report=None):
        self.path.write_text(json.dumps(self.report if report is None else report))

    def detailed(self):
        from readtrial.verify import command_digest, step_summary
        row = {'step': 1, 'command': 'exit 2', 'expect_exit': [2], 'expected_exit_code': 2, 'exit_code': 2,
               'expectation_matched': True, 'refused_by_policy': False, 'superseded': False,
               'superseded_reason': '', 'timed_out': False, 'truncated': False, 'denied': '', 'output': '',
               'execution_error': '', 'execution_mode': 'shell', 'seconds': 0,
               'steps': [{'segment': 1, 'command': 'exit 2', 'exit_code': 2}]}
        check = self.report['checks'][0]
        check.update(steps=['exit 2'], exit_code=0, step_results=[step_summary(row)], proof_steps=[1])
        requirement = {'id': 'one', 'text': 'Exercise the product.'}
        binding = {key: self.report[key] for key in ('run_id', 'model', 'source_hash', 'source_commit', 'recipe')}
        binding.update(requirements=[requirement], command_set=[{'requirement_id': 'one',
                                                               'commands_sha256': [command_digest(row)]}])
        proof = {'binding': binding, 'requirement': requirement, 'commands': [row], 'proof_steps': [1],
                 'observation': {'requirement_id': 'one', 'status': 'verified', 'reason': '',
                                 'observed': check['observed'], 'proof_steps': [1]},
                 'lifecycle': {'recipe_reused': False, 'saved_source_hash': None, 'source_changed': False,
                               'ready_attempts': [], 'cleanup': None,
                               'launch': {'exit_code': None, 'output': '', 'truncated': False, 'stopped': True}}}
        return copy.deepcopy(self.report), proof

    def save_detailed(self, report, proof):
        self.proof.write_text(json.dumps(proof))
        report['checks'][0]['artifact_sha256'] = hashlib.sha256(self.proof.read_bytes()).hexdigest()
        self.save(report)

    def test_matching_nonzero_detail_is_valid_but_tampering_is_rejected(self):
        original, original_proof = self.detailed()
        self.save_detailed(original, original_proof)
        self.assertEqual(validate_result(self.path, self.job), 1)
        mutations = [lambda c, p: c.pop('proof_steps'),
                     lambda c, p: c.update(proof_steps=[True]),
                     lambda c, p: c.update(proof_steps=[2]),
                     lambda c, p: c.update(proof_steps=[1, 1]),
                     lambda c, p: c.update(proof_steps=[]),
                     lambda c, p: c.update(exit_code=2),
                     lambda c, p: c.update(steps=['different command']),
                     lambda c, p: c['step_results'][0].update(expected_exit_code=True),
                     lambda c, p: c['step_results'][0].update(expected_exit_code=256),
                     lambda c, p: c['step_results'][0].update(expectation_matched=False),
                     lambda c, p: c['step_results'][0].update(refused_by_policy=1),
                     lambda c, p: c['step_results'][0].update(superseded_reason='Unmarked replacement.'),
                     lambda c, p: p.update(proof_steps=[]),
                     lambda c, p: p['commands'][0].update(expected_exit_code=0),
                     lambda c, p: p['commands'][0].update(denied='Refused'),
                     lambda c, p: p['observation'].update(observed='different'),
                     lambda c, p: p['requirement'].update(id='other')]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                report, proof = copy.deepcopy(original), copy.deepcopy(original_proof)
                mutate(report['checks'][0], proof)
                self.save_detailed(report, proof)
                with self.assertRaises(ValueError):
                    validate_result(self.path, self.job)

    def test_consistent_but_unsupported_detail_is_rejected(self):
        from readtrial.verify import step_summary
        for changes in ({'exit_code': 0, 'expectation_matched': False},
                        {'exit_code': 126, 'expected_exit_code': 126, 'refused_by_policy': True, 'denied': 'Refused'},
                        {'superseded': True, 'superseded_reason': 'No replacement.'},
                        {'timed_out': True}, {'truncated': True}):
            with self.subTest(changes=changes):
                report, proof = self.detailed()
                row = proof['commands'][0]
                row.update(changes)
                report['checks'][0].update(step_results=[step_summary(row)])
                self.save_detailed(report, proof)
                with self.assertRaises(ValueError):
                    validate_result(self.path, self.job)

    def test_valid_verified_and_unverified_reports_and_relative_proof(self):
        self.save()
        self.assertEqual(validate_result(self.path, self.job), 1)
        self.report['checks'][0].update(status='unverified', reason='Actual mismatch', exit_code=2, proof_steps=[],
                                        artifact='proof.json')
        proof = json.loads(self.proof.read_text())
        proof.update(proof_steps=[])
        proof['observation'].update(status='unverified', reason='Actual mismatch', proof_steps=[])
        self.save_detailed(self.report, proof)
        self.assertEqual(validate_result(self.path, self.job), 1)

    def test_invalid_contract_fields_and_job_binding(self):
        mutations = [lambda r: r.pop('checks'), lambda r: r.update(model='other'),
                     lambda r: r.update(source_hash='b' * 64), lambda r: r.update(recipe='none'),
                     lambda r: r.update(not_checked=[]), lambda r: r.update(tool_problems=[{}]),
                     lambda r: r.update(checks=[]), lambda r: r['checks'].append(r['checks'][0].copy()),
                     lambda r: r['checks'][0].update(requirement_id='unknown'),
                     lambda r: r['checks'][0].pop('observed'), lambda r: r['checks'][0].update(steps=[]),
                     lambda r: r['checks'][0].update(exit_code=True),
                     lambda r: r['checks'][0].update(exit_code=1),
                     lambda r: r['checks'][0].update(status='invented'),
                     lambda r: r['checks'][0].update(status='unverified'),
                     lambda r: r['checks'][0].update(blocked_by='external'),
                     lambda r: r['checks'][0].update(artifact=None),
                     lambda r: r['checks'][0].update(artifact_sha256='b' * 64),
                     lambda r: r['checks'][0].update(artifact_sha256='invalid')]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                report = copy.deepcopy(self.report)
                mutate(report)
                self.save(report)
                with self.assertRaises(ValueError):
                    validate_result(self.path, self.job)
        self.save()
        for changes in ({'stage': 'build'}, {'route': {}}, {'source_hash': 'bad'},
                        {'required_check_ids': ['one', 'one']}, {'required_check_ids': []},
                        {'evidence_dir': str(self.directory / 'elsewhere')}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_result(self.path, {**self.job, **changes})

    def test_missing_tampered_and_escaping_proofs(self):
        self.save()
        self.proof.write_text('{"tampered": true}')
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            validate_result(self.path, self.job)
        self.proof.unlink()
        with self.assertRaisesRegex(ValueError, 'file is missing'):
            validate_result(self.path, self.job)
        self.report['checks'][0]['artifact'] = '../outside.json'
        self.save()
        with self.assertRaisesRegex(ValueError, 'outside result directory'):
            validate_result(self.path, self.job)
        self.report['checks'][0]['artifact'] = str(self.proof)
        self.proof.symlink_to(ROOT / 'README.md')
        self.save()
        with self.assertRaisesRegex(ValueError, 'outside result directory'):
            validate_result(self.path, self.job)

    def test_cli_returns_one_line_on_success_and_failure(self):
        job_path = self.directory / 'job.json'
        job_path.write_text(json.dumps(self.job))
        self.save()
        for expected in (0, 2):
            if expected:
                self.proof.write_text('changed')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(['validate-verify', str(self.path), '--job', str(job_path)])
            self.assertEqual(code, expected)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.path.write_text('broken\nJSON')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['validate-verify', str(self.path), '--job', str(job_path)]), 2)
        self.assertEqual(len(output.getvalue().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
